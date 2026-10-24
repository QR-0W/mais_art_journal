"""按当地日历窗口随机执行自拍，只使用实时日程 API，不补排或重试。

每个等分时间段抽取一次触发时间，计划和已消费项保存在一个 JSON 文件中。
执行顺序为实时活动、生成一张自拍、生成配文、调用一次 MaiTrace 发布 API。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import asyncio
import json
import logging
import random

from .caption_generator import generate_caption
from .schedule_provider import ApiScheduleProvider, LiveScheduleError
from .service import 生成自拍

if TYPE_CHECKING:
    from ...plugin import MaisArtPlugin

logger = logging.getLogger("plugin.mais_art_journal.auto_selfie")


class AutoSelfieTask:
    """自动自拍后台任务，阶段结果不包含图片、配文或外部异常原文。"""

    def __init__(self, plugin: "MaisArtPlugin"):
        self.plugin = plugin
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self.last_result: Dict[str, Any] = {}
        self._plan: Optional[Dict[str, Any]] = None

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._selfie_loop())
        self.task.add_done_callback(self._task_done)
        logger.info("自动自拍任务已启动")

    async def stop(self) -> None:
        self.is_running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("自动自拍任务结束异常: %s", type(exc).__name__)
            finally:
                self.task = None
        logger.info("自动自拍任务已停止")

    def _task_done(self, task: asyncio.Task) -> None:
        self.is_running = False
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._result("schedule", "failed", "自动自拍调度已停止", exc)
            logger.error("自动自拍调度异常: %s", type(exc).__name__)

    def _result(
        self, stage: str, status: str, message: str, exc: Optional[Exception] = None,
    ) -> Dict[str, Any]:
        result = {
            "stage": stage, "status": status,
            "success": status == "success", "message": message,
        }
        if exc is not None:
            result["exception_type"] = type(exc).__name__
            logger.warning("自动自拍阶段 %s 异常: %s", stage, type(exc).__name__)
        self.last_result = result
        return result

    def _window_bounds(self, now: datetime) -> Tuple[datetime, datetime]:
        config = self.plugin.config.selfie
        zone = ZoneInfo(config.time_zone)
        local = now.astimezone(zone)
        start_time = time.fromisoformat(config.window_start)
        end_time = time.fromisoformat(config.window_end)
        day = local.date()
        start = datetime.combine(day, start_time, zone)
        if local < start:
            day -= timedelta(days=1)
            start = datetime.combine(day, start_time, zone)
        end_day = day + timedelta(days=1) if end_time <= start_time else day
        return start, datetime.combine(end_day, end_time, zone)

    @property
    def _plan_path(self) -> Path:
        return Path(self.plugin.ctx.paths.data_dir) / "auto_selfie_plan.json"

    def _read_plan(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._plan_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _write_plan(self) -> None:
        path = self._plan_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._plan, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    async def _prepare_plan(self, now: datetime) -> Dict[str, Any]:
        start, end = self._window_bounds(now)
        config = self.plugin.config.selfie
        count = config.daily_count
        if count < 1:
            raise ValueError("daily_count must be positive")
        key = [start.date().isoformat(), config.window_start, config.window_end, config.time_zone, count]
        if self._plan is not None and self._plan["key"] == key:
            return self._plan
        saved = await asyncio.to_thread(self._read_plan)
        if saved is not None and saved["key"] == key:
            self._plan = saved
        else:
            segment = (end.timestamp() - start.timestamp()) / count
            triggers = [start.timestamp() + (i + random.random()) * segment for i in range(count)]
            self._plan = {
                "key": key, "triggers": triggers, "consumed": [],
                "segment_ends": [start.timestamp() + (i + 1) * segment for i in range(count)],
            }
        # 启动或恢复时，过去的触发点只标记为已消费，不重抽、不补做。
        consumed = self._plan["consumed"]
        for i, trigger in enumerate(self._plan["triggers"]):
            if trigger <= now.timestamp() and i not in consumed:
                consumed.append(i)
        await asyncio.to_thread(self._write_plan)
        return self._plan

    async def _next_trigger(self, now: datetime) -> Tuple[Optional[int], float]:
        plan = await self._prepare_plan(now)
        changed = False
        for i, trigger in enumerate(plan["triggers"]):
            if i in plan["consumed"]:
                continue
            if trigger <= now.timestamp():
                plan["consumed"].append(i)
                changed = True
                continue
            if changed:
                await asyncio.to_thread(self._write_plan)
            return i, trigger
        if changed:
            await asyncio.to_thread(self._write_plan)
        start, _ = self._window_bounds(now)
        next_start = datetime.combine(start.date() + timedelta(days=1), start.timetz())
        return None, next_start.timestamp()

    async def _selfie_loop(self) -> None:
        while self.is_running:
            index, trigger = await self._next_trigger(datetime.now(ZoneInfo(self.plugin.config.selfie.time_zone)))
            await asyncio.sleep(max(0, trigger - datetime.now().timestamp()))
            if index is None:
                continue
            now = datetime.now().timestamp()
            if now < trigger:
                continue
            self._plan["consumed"].append(index)
            await asyncio.to_thread(self._write_plan)
            if now >= self._plan["segment_ends"][index]:
                continue
            await self._execute_selfie()

    async def _execute_selfie(self) -> Dict[str, Any]:
        stage = "activity"
        try:
            self._result(stage, "running", "正在获取实时活动")
            provider = ApiScheduleProvider(self.plugin.ctx, chat_id="global")
            activity = await provider.get_live_activity()
            if activity is None:
                return self._result(stage, "skipped", "没有实时活动，跳过本次自拍")

            stage = "generation"
            self._result(stage, "running", "正在生成自拍")
            generated = await 生成自拍(self.plugin, activity=activity)
            if generated.get("uncertain") is True:
                return self._result(stage, "unknown", "自拍生成结果未知，未发布且不重试")
            if generated.get("failures"):
                return self._result(stage, "failed", "自拍生成包含失败项，未发布")
            if generated.get("success") is not True:
                return self._result(stage, "failed", "自拍生成失败，未发布")
            images = generated.get("images", [])
            if len(images) != 1 or generated.get("available_count") != 1:
                return self._result(stage, "failed", "自拍图片数量违反单张协议，未发布")
            image = images[0]["base64"]
            if not isinstance(image, str) or not image or image.startswith(("data:", "http://", "https://")):
                return self._result(stage, "failed", "自拍未返回纯 base64 图片，未发布")

            stage = "caption"
            self._result(stage, "running", "正在生成配文")
            caption = await generate_caption(
                self.plugin.ctx, activity, llm_task=self.plugin.config.basic.llm_task_name or "utils",
            )
            if not caption or not caption.strip():
                return self._result(stage, "failed", "配文生成失败，未发布")

            stage = "publication"
            self._result(stage, "running", "正在提交发布")
            result = await self.plugin.ctx.api.call(
                "Rabbit-Jia-Er.MaiTrace.send_feed_api", message=caption, images=[image],
            )
            if not isinstance(result, dict) or result.get("uncertain") is True:
                return self._result(stage, "unknown", "发布结果未知，不重试")
            if result.get("result") is True:
                return self._result(stage, "success", "自拍已确认发布")
            if result.get("result") is False:
                return self._result(stage, "failed", "自拍发布失败，不重试")
            return self._result(stage, "unknown", "发布结果未知，不重试")
        except asyncio.CancelledError:
            status = "unknown" if stage in ("generation", "publication") else "cancelled"
            if stage == "publication":
                message = "发布结果未知，不重试"
            elif stage == "generation":
                message = "生成等待已取消；若已提交，服务端结果未知，不重提交"
            else:
                message = "自拍流程已取消"
            self._result(stage, status, message)
            raise
        except LiveScheduleError as exc:
            return self._result("activity", "failed", str(exc))
        except Exception as exc:
            status = "unknown" if stage == "publication" else "failed"
            return self._result(stage, status, "发布结果未知，不重试" if status == "unknown" else "自拍流程已终止，未发布", exc)


__all__ = ["AutoSelfieTask"]
