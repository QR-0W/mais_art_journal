"""绘卷聊天额度账本：事务预占、准确结算和待确认记录的人工处理。

主体来自调用方解析的真实 SDK 会话。本模块不推算 session_id，也不访问 QQ 或模型。
一个插件实例持有一个 QuotaStore；构造时无 I/O，SQLite 始终在专用线程中执行。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Literal, Optional, Tuple, TypeVar
from zoneinfo import ZoneInfo

import asyncio
import sqlite3
import uuid


QuotaPeriod = Literal["daily", "weekly", "monthly", "once"]
ReservationStatus = Literal["reserved", "consumed", "released", "unknown"]
SettlementOutcome = Literal["consumed", "released", "unknown"]
ResolutionOutcome = Literal["consumed", "released"]
T = TypeVar("T")
_SCHEMA_VERSION = 1


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    return value.strip()


@dataclass(frozen=True)
class QuotaSubject:
    platform: str
    target_type: Literal["group", "user"]
    target_id: str

    def __post_init__(self) -> None:
        for field, label in (("platform", "平台"), ("target_id", "群或用户 ID")):
            value = self.platform if field == "platform" else self.target_id
            if _text(value, label) != value or any(char.isspace() for char in value):
                raise ValueError(f"{label}不能包含空白字符")
        if self.target_type not in {"group", "user"}:
            raise ValueError("额度主体类型必须为 group 或 user")


@dataclass(frozen=True)
class QuotaPolicy:
    limit: int
    period: QuotaPeriod = "daily"
    timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 0 <= self.limit <= 1000000:
            raise ValueError("额度上限必须为 0 至 1000000 的整数")
        if self.period not in {"daily", "weekly", "monthly", "once"}:
            raise ValueError("额度周期必须为 daily、weekly、monthly 或 once")
        _text(self.timezone, "额度时区")


@dataclass(frozen=True)
class QuotaStatus:
    subject: QuotaSubject
    limit: int
    adjustment: int
    consumed: int
    reserved: int
    unknown: int
    period: QuotaPeriod
    period_key: str
    timezone: str
    reset_at: Optional[datetime]

    @property
    def balance(self) -> int:
        """保留管理员减额或降低配置后形成的实际负余额。"""
        return self.limit + self.adjustment - self.consumed - self.reserved

    @property
    def remaining(self) -> int:
        return max(0, self.balance)

    @property
    def used(self) -> int:
        return self.consumed + self.reserved


@dataclass(frozen=True)
class QuotaReservation:
    token: str
    subject: QuotaSubject
    status: ReservationStatus
    period: QuotaPeriod
    period_key: str
    timezone: str
    limit: int
    reset_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    actor: str
    reason: str
    amount: int = 1

    @property
    def reservation_id(self) -> str:
        return self.token

    @property
    def state(self) -> ReservationStatus:
        return self.status

    @property
    def policy(self) -> QuotaPolicy:
        return QuotaPolicy(self.limit, self.period, self.timezone)


class QuotaError(RuntimeError):
    """可向用户说明的额度账本错误。"""


class QuotaExceeded(QuotaError):
    def __init__(self, status: QuotaStatus) -> None:
        self.status = status
        self.quota_status = status
        super().__init__(f"当前周期生图额度不足，剩余 {status.remaining} 张")


class QuotaStateError(QuotaError):
    """预占不存在或结算会破坏已确认的状态。"""


class QuotaSubjectMismatch(QuotaStateError):
    """禁止通过其他聊天的预占编号进行管理。"""


def _subject(subject: QuotaSubject) -> Tuple[str, str, str]:
    return subject.platform, subject.target_type, subject.target_id


def _period(policy: QuotaPolicy, now: datetime) -> Tuple[str, Optional[datetime]]:
    try:
        local = now.astimezone(ZoneInfo(policy.timezone))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"无效的额度时区：{policy.timezone}") from exc
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if policy.period == "daily":
        return f"daily:{policy.timezone}:{local.date().isoformat()}", midnight + timedelta(days=1)
    if policy.period == "weekly":
        monday = midnight - timedelta(days=midnight.weekday())
        return f"weekly:{policy.timezone}:{monday.date().isoformat()}", monday + timedelta(days=7)
    if policy.period == "monthly":
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        return f"monthly:{policy.timezone}:{local.year:04d}-{local.month:02d}", midnight.replace(
            year=year, month=month, day=1
        )
    return "once", None


class QuotaStore:
    """SQLite 的唯一所有者；调用方负责在插件卸载时 await close()。"""

    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock = asyncio.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._connection: Optional[sqlite3.Connection] = None
        self._path: Optional[Path] = None

    @property
    def loaded(self) -> bool:
        return self._connection is not None

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("额度时钟必须返回带时区的 datetime")
        return now.astimezone(timezone.utc)

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise QuotaStateError("额度账本尚未加载或已经关闭")
        return self._connection

    @staticmethod
    async def _finish_future(future: asyncio.Future[T]) -> T:
        cancelled = False
        # SQLite 事务不能在线程外中断。即使调用方多次取消，也必须等事务完成再解锁或关库。
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                cancelled = True
                if future.cancelled():
                    raise
            except BaseException as exc:
                if cancelled:
                    raise asyncio.CancelledError() from exc
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def _submit(self, operation: Callable[[], T]) -> T:
        if self._executor is None:
            raise QuotaStateError("额度账本尚未加载或已经关闭")
        future = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        return await self._finish_future(future)

    async def _run(self, operation: Callable[[], T]) -> T:
        async with self._lock:
            return await self._submit(operation)

    async def load(self, path: Path) -> None:
        """加载数据库文件；上次进程未结算的预占保留占用并转为待人工核对。"""
        async with self._lock:
            if self._executor is not None:
                if self._path == path:
                    return
                raise QuotaStateError("额度账本已经加载，切换数据库前必须关闭")
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="art-quota")
            self._executor = executor
            try:
                await self._submit(lambda: self._open_sync(path))
                self._path = path
            except BaseException:
                await self._dispose(executor)
                raise

    def _open_sync(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=10)
        self._connection = connection
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, _SCHEMA_VERSION):
            raise QuotaStateError(f"额度数据库版本不兼容：{version}，当前支持 {_SCHEMA_VERSION}")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS quota_reservations(
                token TEXT PRIMARY KEY, platform TEXT NOT NULL, target_type TEXT NOT NULL,
                target_id TEXT NOT NULL, period TEXT NOT NULL, period_key TEXT NOT NULL,
                timezone TEXT NOT NULL, quota_limit INTEGER NOT NULL, reset_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('reserved','consumed','released','unknown')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS quota_reservation_period
                ON quota_reservations(platform,target_type,target_id,period_key,status);
            CREATE TABLE IF NOT EXISTS quota_adjustments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, target_type TEXT NOT NULL,
                target_id TEXT NOT NULL, period_key TEXT NOT NULL, amount INTEGER NOT NULL,
                actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS quota_adjustment_period
                ON quota_adjustments(platform,target_type,target_id,period_key);
            CREATE TABLE IF NOT EXISTS quota_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, platform TEXT NOT NULL,
                target_type TEXT NOT NULL, target_id TEXT NOT NULL, period_key TEXT NOT NULL,
                action TEXT NOT NULL, amount INTEGER NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                previous_status TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
        with self._transaction() as connection:
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            now = self._now().isoformat()
            for row in connection.execute("SELECT * FROM quota_reservations WHERE status='reserved'").fetchall():
                reason = "插件重新加载，原预占的投递结果待人工核对"
                connection.execute(
                    "UPDATE quota_reservations SET status='unknown',updated_at=?,actor='system',reason=? WHERE token=?",
                    (now, reason, row["token"]),
                )
                self._audit_sync(
                    row["token"],
                    QuotaSubject(row["platform"], row["target_type"], row["target_id"]),
                    row["period_key"],
                    "recover",
                    0,
                    "system",
                    reason,
                    "reserved",
                    "unknown",
                    now,
                )

    def _close_sync(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    async def close(self) -> None:
        async with self._lock:
            executor = self._executor
            if executor is None:
                return
            await self._dispose(executor)

    async def _dispose(self, executor: ThreadPoolExecutor) -> None:
        # 加载失败后的清理同样可能再次被取消，连接关闭后仍须重置状态并等待线程退出。
        try:
            await self._submit(self._close_sync)
        finally:
            self._executor = None
            self._path = None
            shutdown = asyncio.get_running_loop().run_in_executor(None, executor.shutdown)
            await self._finish_future(shutdown)

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._db()
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _audit_sync(
        self,
        token: str,
        subject: QuotaSubject,
        period_key: str,
        action: str,
        amount: int,
        actor: str,
        reason: str,
        previous: str,
        status: str,
        now: str,
    ) -> None:
        self._db().execute(
            "INSERT INTO quota_audit(token,platform,target_type,target_id,period_key,action,amount,actor,reason,previous_status,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, *_subject(subject), period_key, action, amount, actor, reason, previous, status, now),
        )

    def _status_sync(
        self, subject: QuotaSubject, policy: QuotaPolicy, period_key: str, reset_at: Optional[datetime]
    ) -> QuotaStatus:
        params = (*_subject(subject), period_key)
        # reserved 包含 unknown；所有占用都只按预占所属周期计算，不依赖结算当天。
        usage = (
            self._db()
            .execute(
                """SELECT
            COALESCE(SUM(status='consumed'),0),
            COALESCE(SUM(status IN ('reserved','unknown')),0),
            COALESCE(SUM(status='unknown'),0)
            FROM quota_reservations WHERE platform=? AND target_type=? AND target_id=? AND period_key=?""",
                params,
            )
            .fetchone()
        )
        adjustment = (
            self._db()
            .execute(
                "SELECT COALESCE(SUM(amount),0) FROM quota_adjustments WHERE platform=? AND target_type=? AND target_id=? AND period_key=?",
                params,
            )
            .fetchone()[0]
        )
        return QuotaStatus(
            subject,
            policy.limit,
            int(adjustment),
            int(usage[0]),
            int(usage[1]),
            int(usage[2]),
            policy.period,
            period_key,
            policy.timezone,
            reset_at,
        )

    async def status(self, subject: QuotaSubject, policy: QuotaPolicy) -> QuotaStatus:
        def read() -> QuotaStatus:
            period_key, reset_at = _period(policy, self._now())
            with self._transaction(write=False):
                return self._status_sync(subject, policy, period_key, reset_at)

        return await self._run(read)

    @staticmethod
    def _reservation(row: sqlite3.Row) -> QuotaReservation:
        zone = ZoneInfo(row["timezone"])
        return QuotaReservation(
            token=row["token"],
            subject=QuotaSubject(row["platform"], row["target_type"], row["target_id"]),
            status=row["status"],
            period=row["period"],
            period_key=row["period_key"],
            timezone=row["timezone"],
            limit=int(row["quota_limit"]),
            reset_at=datetime.fromisoformat(row["reset_at"]).astimezone(zone) if row["reset_at"] is not None else None,
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(zone),
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(zone),
            actor=row["actor"],
            reason=row["reason"],
        )

    async def reserve(self, subject: QuotaSubject, policy: QuotaPolicy) -> QuotaReservation:
        """每次恰好预占一张；检查与写入持有同一个 SQLite 写事务。"""

        def write() -> QuotaReservation:
            with self._transaction() as connection:
                # 等待其他连接写入时可能跨过周期边界，获得写锁后再确定本次归属。
                now = self._now()
                period_key, reset_at = _period(policy, now)
                status = self._status_sync(subject, policy, period_key, reset_at)
                if status.remaining < 1:
                    raise QuotaExceeded(status)
                token, stamp = uuid.uuid4().hex, now.isoformat()
                connection.execute(
                    """INSERT INTO quota_reservations
                    (token,platform,target_type,target_id,period,period_key,timezone,quota_limit,reset_at,status,created_at,updated_at,actor,reason)
                    VALUES(?,?,?,?,?,?,?,?,?,'reserved',?,?,'pipeline','生图请求预占一张')""",
                    (
                        token,
                        *_subject(subject),
                        policy.period,
                        period_key,
                        policy.timezone,
                        policy.limit,
                        reset_at.isoformat() if reset_at is not None else None,
                        stamp,
                        stamp,
                    ),
                )
                self._audit_sync(
                    token, subject, period_key, "reserve", -1, "pipeline", "生图请求预占一张", "", "reserved", stamp
                )
                return self._reservation(
                    connection.execute("SELECT * FROM quota_reservations WHERE token=?", (token,)).fetchone()
                )

        return await self._run(write)

    async def settle(self, token: str, outcome: SettlementOutcome, *, reason: str = "") -> QuotaReservation:
        """管线结算；待确认状态不能自动退款，成功记录也不能被后续失败覆盖。"""
        if outcome not in {"consumed", "released", "unknown"}:
            raise ValueError("额度结算结果必须为 consumed、released 或 unknown")
        reasons = {
            "consumed": "已确认实际发送成功",
            "released": "已确认发送失败，退回预占",
            "unknown": "投递结果尚未确认，保留预占",
        }
        return await self._run(
            lambda: self._settle_sync(
                token,
                outcome,
                subject=None,
                actor="pipeline",
                reason=reason.strip() if reason.strip() else reasons[outcome],
                manual=False,
            )
        )

    def _settle_sync(
        self,
        token: str,
        outcome: SettlementOutcome,
        *,
        subject: Optional[QuotaSubject],
        actor: str,
        reason: str,
        manual: bool,
    ) -> QuotaReservation:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM quota_reservations WHERE token=?", (token,)).fetchone()
            if row is None:
                raise QuotaStateError("未找到额度预占记录")
            reservation = self._reservation(row)
            if subject is not None and reservation.subject != subject:
                raise QuotaSubjectMismatch("该额度预占不属于当前聊天")
            previous = reservation.status
            if previous == outcome:
                return reservation
            if previous in {"consumed", "released"}:
                raise QuotaStateError("额度预占已经完成结算，不能更改已确认结果")
            if manual and previous != "unknown":
                raise QuotaStateError("该预占仍在处理中，只能人工结算待确认记录")
            if previous == "unknown" and outcome == "released" and not manual:
                raise QuotaStateError("投递结果待确认，不能自动退款；请人工核对后结算")
            stamp = self._now().isoformat()
            connection.execute(
                "UPDATE quota_reservations SET status=?,updated_at=?,actor=?,reason=? WHERE token=?",
                (outcome, stamp, actor, reason, token),
            )
            self._audit_sync(
                token,
                reservation.subject,
                reservation.period_key,
                "resolve" if manual else "settle",
                1 if outcome == "released" else 0,
                actor,
                reason,
                previous,
                outcome,
                stamp,
            )
            return self._reservation(
                connection.execute("SELECT * FROM quota_reservations WHERE token=?", (token,)).fetchone()
            )

    async def adjust(
        self, subject: QuotaSubject, policy: QuotaPolicy, amount: int, *, actor: str, reason: str
    ) -> QuotaStatus:
        """管理员只调整调用时的当前周期，保留操作者、理由和负余额，不改历史预占。"""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
            raise ValueError("额度调整量必须为非零整数")
        actor, reason = _text(actor, "额度调整操作者"), _text(reason, "额度调整理由")

        def write() -> QuotaStatus:
            with self._transaction() as connection:
                now = self._now()
                period_key, reset_at = _period(policy, now)
                connection.execute(
                    "INSERT INTO quota_adjustments(platform,target_type,target_id,period_key,amount,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (*_subject(subject), period_key, amount, actor, reason, now.isoformat()),
                )
                self._audit_sync("", subject, period_key, "adjust", amount, actor, reason, "", "", now.isoformat())
                return self._status_sync(subject, policy, period_key, reset_at)

        return await self._run(write)

    async def list_pending(self, subject: QuotaSubject) -> List[QuotaReservation]:
        """列出本聊天跨周期保留的 unknown；正在生成的 reserved 不交给管理员退款。"""
        return await self._run(
            lambda: [
                self._reservation(row)
                for row in self._db()
                .execute(
                    "SELECT * FROM quota_reservations WHERE platform=? AND target_type=? AND target_id=? AND status='unknown' ORDER BY created_at,token",
                    _subject(subject),
                )
                .fetchall()
            ]
        )

    async def resolve(
        self, subject: QuotaSubject, token: str, outcome: ResolutionOutcome, *, actor: str, reason: str
    ) -> QuotaReservation:
        if outcome not in {"consumed", "released"}:
            raise ValueError("人工结算结果必须为 consumed 或 released")
        actor, reason = _text(actor, "人工结算操作者"), _text(reason, "人工结算理由")
        return await self._run(
            lambda: self._settle_sync(token, outcome, subject=subject, actor=actor, reason=reason, manual=True)
        )
