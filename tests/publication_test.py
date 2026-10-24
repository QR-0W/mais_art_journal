"""自拍发布与随机日程离线测试，独立 namespace，不加载旧插件初始化。

运行时指定 --rootdir=<本目录> --confcutdir=<本目录> --import-mode=importlib，
避免 pytest 收集插件根目录的旧 __init__.py。
"""

from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import asyncio
import json
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_mais_art_publication_tests"
for suffix, path in (
    ("", ROOT), (".core", ROOT / "core"), (".core.selfie", ROOT / "core/selfie"),
):
    module = ModuleType(PACKAGE + suffix)
    module.__path__ = [str(path)]
    sys.modules[module.__name__] = module

for name, function in (("service", "生成自拍"), ("caption_generator", "generate_caption")):
    module = ModuleType(PACKAGE + ".core.selfie." + name)
    setattr(module, function, AsyncMock())
    sys.modules[module.__name__] = module

auto = import_module(PACKAGE + ".core.selfie.auto_selfie_task")
schedule = import_module(PACKAGE + ".core.selfie.schedule_provider")
ZONE = ZoneInfo("Asia/Shanghai")
IMAGE = "ZmFrZS1pbWFnZQ=="


@pytest.fixture
def context(tmp_path, monkeypatch):
    plugin = SimpleNamespace(
        ctx=SimpleNamespace(
            paths=SimpleNamespace(data_dir=tmp_path),
            api=SimpleNamespace(call=AsyncMock(return_value={"result": True, "uncertain": False, "message": "ok"})),
        ),
        config=SimpleNamespace(
            selfie=SimpleNamespace(daily_count=4, window_start="07:00", window_end="23:00", time_zone="Asia/Shanghai"),
            basic=SimpleNamespace(llm_task_name="offline-utils"),
        ),
    )
    activity = schedule.ActivityInfo(schedule.ActivityType.STUDYING, "离线读书")
    provider = SimpleNamespace(get_live_activity=AsyncMock(return_value=activity))
    factory = Mock(return_value=provider)
    generation = AsyncMock(return_value={
        "success": True, "images": [{"index": 0, "base64": IMAGE}], "message": "ok", "failures": [],
        "requested_count": 1, "returned_count": 1, "available_count": 1,
    })
    caption = AsyncMock(return_value="今天安静读书。")
    monkeypatch.setattr(auto, "ApiScheduleProvider", factory)
    monkeypatch.setattr(auto, "生成自拍", generation)
    monkeypatch.setattr(auto, "generate_caption", caption)
    return SimpleNamespace(
        plugin=plugin, task=auto.AutoSelfieTask(plugin), activity=activity,
        provider=provider, factory=factory, generation=generation, caption=caption,
        publish=plugin.ctx.api.call,
    )


@pytest.mark.asyncio
async def test_one_generation_one_publication_without_preflight(context):
    result = await context.task._execute_selfie()
    assert result == context.task.last_result
    assert result["stage"] == "publication" and result["status"] == "success" and result["success"] is True
    context.factory.assert_called_once_with(context.plugin.ctx, chat_id="global")
    context.provider.get_live_activity.assert_awaited_once_with()
    context.generation.assert_awaited_once_with(context.plugin, activity=context.activity)
    context.caption.assert_awaited_once_with(context.plugin.ctx, context.activity, llm_task="offline-utils")
    context.publish.assert_awaited_once_with(
        "Rabbit-Jia-Er.MaiTrace.send_feed_api", message="今天安静读书。", images=[IMAGE],
    )
    assert IMAGE not in json.dumps(result) and "今天安静读书" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_no_activity_does_not_generate_or_publish(context):
    context.provider.get_live_activity.return_value = None
    result = await context.task._execute_selfie()
    assert result["stage"] == "activity" and result["status"] == "skipped"
    context.generation.assert_not_awaited()
    context.caption.assert_not_awaited()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_provider_never_uses_sqlite(monkeypatch):
    provider = schedule.ApiScheduleProvider(SimpleNamespace(api=SimpleNamespace(call=AsyncMock(return_value={"has_activity": False}))))
    monkeypatch.setattr(provider, "_get_sqlite_fallback", Mock(side_effect=AssertionError("SQLite forbidden")))
    assert await provider.get_live_activity() is None
    provider._get_sqlite_fallback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, [], {}, {"has_activity": "false"},
    {"has_activity": False, "error": "plugin_not_initialized"},
    {"has_activity": True, "activity": None}, {"has_activity": True, "activity": {}}])
async def test_live_provider_rejects_failure_as_distinct_from_no_activity(snapshot):
    provider = schedule.ApiScheduleProvider(SimpleNamespace(api=SimpleNamespace(call=AsyncMock(return_value=snapshot))))
    with pytest.raises(schedule.LiveScheduleError):
        await provider.get_live_activity()


@pytest.mark.asyncio
async def test_live_api_exception_is_safe_and_never_reads_sqlite(monkeypatch):
    api = AsyncMock(side_effect=PermissionError("secret-token-in-upstream-exception"))
    provider = schedule.ApiScheduleProvider(SimpleNamespace(api=SimpleNamespace(call=api)))
    monkeypatch.setattr(provider, "_get_sqlite_fallback", Mock(side_effect=AssertionError("SQLite forbidden")))
    with pytest.raises(schedule.LiveScheduleError, match="PermissionError") as error:
        await provider.get_live_activity()
    assert "secret-token" not in str(error.value)
    provider._get_sqlite_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_auto_schedule_api_failure_is_failed_not_skipped(context):
    context.provider.get_live_activity.side_effect = schedule.LiveScheduleError("实时日程 API 无法调用")
    result = await context.task._execute_selfie()
    assert result["stage"] == "activity" and result["status"] == "failed"
    assert "API" in result["message"]
    context.generation.assert_not_awaited()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", ["", "   "])
async def test_caption_failure_stops_publication(context, caption):
    context.caption.return_value = caption
    result = await context.task._execute_selfie()
    assert result["stage"] == "caption" and result["status"] == "failed"
    context.generation.assert_awaited_once()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", [
    {"success": False, "images": [], "message": "sensitive-generation-error"},
    {"success": False, "images": [{"base64": IMAGE}]},
    {"success": True, "images": []},
    {"success": True, "images": [{"base64": "https://offline.invalid/image"}], "available_count": 1},
    {"success": True, "images": [{"base64": "data:image/png;base64," + IMAGE}], "available_count": 1},
])
async def test_generation_failure_never_refills(context, generated):
    context.generation.return_value = generated
    result = await context.task._execute_selfie()
    assert result["stage"] == "generation" and result["status"] == "failed"
    assert "sensitive" not in json.dumps(result)
    context.generation.assert_awaited_once()
    context.caption.assert_not_awaited()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response,status", [
    ({"result": True, "uncertain": False}, "success"),
    ({"result": False, "uncertain": False}, "failed"),
    ({"result": False, "uncertain": True}, "unknown"),
    ({"result": True, "uncertain": True}, "unknown"),
    ({"result": 1}, "unknown"),
    ({"result": "true"}, "unknown"),
    ({"success": True}, "unknown"),
    (None, "unknown"),
])
async def test_publication_requires_explicit_true(context, response, status):
    context.publish.return_value = response
    result = await context.task._execute_selfie()
    assert result["status"] == status
    assert result["success"] is (status == "success")
    context.generation.assert_awaited_once()
    context.publish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("secret-token"), TimeoutError("secret-token")])
async def test_publication_exception_is_unknown_and_safe(context, error, caplog):
    context.publish.side_effect = error
    result = await context.task._execute_selfie()
    assert result["status"] == "unknown" and result["success"] is False
    assert result["exception_type"] == type(error).__name__
    assert "secret-token" not in json.dumps(result) + caplog.text
    context.publish.assert_awaited_once()
    context.generation.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["activity", "generation", "caption"])
async def test_upstream_exceptions_stop_before_publication(context, stage, caplog):
    call = {"activity": context.provider.get_live_activity, "generation": context.generation, "caption": context.caption}[stage]
    call.side_effect = RuntimeError("secret-token")
    result = await context.task._execute_selfie()
    assert result["stage"] == stage and result["status"] == "failed"
    assert "secret-token" not in json.dumps(result) + caplog.text
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_draws_once_in_each_equal_segment(context, monkeypatch):
    monkeypatch.setattr(auto.random, "random", Mock(side_effect=[0.1, 0.4, 0.6, 0.9]))
    now = datetime(2026, 10, 24, 7, tzinfo=ZONE)
    plan = await context.task._prepare_plan(now)
    assert plan["key"][0] == "2026-10-24"
    start = now.timestamp()
    segment = 4 * 3600
    for i, trigger in enumerate(plan["triggers"]):
        assert start + i * segment <= trigger < start + (i + 1) * segment
    assert plan["consumed"] == []
    assert auto.random.random.call_count == 4
    assert json.loads(context.task._plan_path.read_text()) == plan
    assert not context.task._plan_path.with_suffix(".tmp").exists()


@pytest.mark.asyncio
async def test_no_minimum_interval(context, monkeypatch):
    context.plugin.config.selfie.daily_count = 120
    context.plugin.config.selfie.window_end = "08:00"
    monkeypatch.setattr(auto.random, "random", Mock(return_value=0.5))
    plan = await context.task._prepare_plan(datetime(2026, 10, 24, 7, tzinfo=ZONE))
    assert len(plan["triggers"]) == 120
    assert plan["triggers"][1] - plan["triggers"][0] == 30


@pytest.mark.asyncio
async def test_cross_midnight_owned_by_window_start_day(context, monkeypatch):
    context.plugin.config.selfie.window_start = "22:00"
    context.plugin.config.selfie.window_end = "02:00"
    monkeypatch.setattr(auto.random, "random", Mock(return_value=0.5))
    now = datetime(2026, 10, 25, 0, 10, tzinfo=ZONE)
    start, end = context.task._window_bounds(now)
    assert start == datetime(2026, 10, 24, 22, tzinfo=ZONE)
    assert end == datetime(2026, 10, 25, 2, tzinfo=ZONE)
    plan = await context.task._prepare_plan(now)
    assert plan["key"][0] == "2026-10-24"
    assert plan["consumed"] == [0, 1]
    index, trigger = await context.task._next_trigger(now)
    assert index == 2
    assert datetime.fromtimestamp(trigger, ZONE) == datetime(2026, 10, 25, 0, 30, tzinfo=ZONE)


@pytest.mark.parametrize("zone,expected", [
    ("Asia/Shanghai", "2026-10-25"), ("America/New_York", "2026-10-24"), ("UTC", "2026-10-24"),
])
def test_window_uses_configured_zone_not_host(context, zone, expected):
    context.plugin.config.selfie.time_zone = zone
    context.plugin.config.selfie.window_start = "00:00"
    context.plugin.config.selfie.window_end = "23:59"
    start, _ = context.task._window_bounds(datetime(2026, 10, 24, 20, tzinfo=timezone.utc))
    assert start.date().isoformat() == expected
    assert str(start.tzinfo) == zone


@pytest.mark.asyncio
async def test_restart_reuses_draws_and_skips_missed_triggers(context, monkeypatch):
    draw = Mock(return_value=0.5)
    monkeypatch.setattr(auto.random, "random", draw)
    first = await context.task._prepare_plan(datetime(2026, 10, 24, 7, tzinfo=ZONE))
    original = first["triggers"][:]
    assert draw.call_count == 4
    first["consumed"].append(0)
    await asyncio.to_thread(context.task._write_plan)
    restarted = auto.AutoSelfieTask(context.plugin)
    now = datetime(2026, 10, 24, 15, tzinfo=ZONE)
    index, trigger = await restarted._next_trigger(now)
    assert restarted._plan["triggers"] == original
    assert restarted._plan["consumed"] == [0, 1]
    assert index == 2 and trigger == original[2]
    assert draw.call_count == 4
    context.generation.assert_not_awaited()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_after_window_waits_for_next_start_without_catchup(context, monkeypatch):
    monkeypatch.setattr(auto.random, "random", Mock(return_value=0.5))
    now = datetime(2026, 10, 24, 23, 30, tzinfo=ZONE)
    index, trigger = await context.task._next_trigger(now)
    assert index is None
    assert datetime.fromtimestamp(trigger, ZONE) == datetime(2026, 10, 25, 7, tzinfo=ZONE)
    assert context.task._plan["consumed"] == [0, 1, 2, 3]
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_process_missed_trigger_is_not_rescheduled(context, monkeypatch):
    monkeypatch.setattr(auto.random, "random", Mock(return_value=0.5))
    await context.task._prepare_plan(datetime(2026, 10, 24, 7, tzinfo=ZONE))
    index, trigger = await context.task._next_trigger(datetime(2026, 10, 24, 10, tzinfo=ZONE))
    assert index == 1
    assert context.task._plan["consumed"] == [0]
    assert datetime.fromtimestamp(trigger, ZONE).hour == 13
    assert auto.random.random.call_count == 4


@pytest.mark.asyncio
async def test_consumed_is_saved_before_execution(context, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    context.task._plan = {"consumed": [], "segment_ends": [now + 3600]}
    context.task.is_running = True
    monkeypatch.setattr(context.task, "_next_trigger", AsyncMock(return_value=(0, now - 1)))

    async def execute():
        saved = json.loads(context.task._plan_path.read_text())
        assert saved["consumed"] == [0]
        context.task.is_running = False

    monkeypatch.setattr(context.task, "_execute_selfie", AsyncMock(side_effect=execute))
    await context.task._selfie_loop()
    context.task._execute_selfie.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_cancels_and_awaits_inflight_publication(context, monkeypatch):
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def publish(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    context.publish.side_effect = publish
    monkeypatch.setattr(context.task, "_selfie_loop", context.task._execute_selfie)
    await context.task.start()
    running = context.task.task
    await context.task.start()
    assert context.task.task is running
    await asyncio.wait_for(entered.wait(), timeout=2)
    context.task.is_running = False
    await context.task.stop()
    assert running.done() and running.cancelled() and finished.is_set()
    assert context.task.task is None and context.task.is_running is False
    assert context.task.last_result["stage"] == "publication"
    assert context.task.last_result["status"] == "unknown"
    context.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_exception_is_consumed_and_logged_safely(context, monkeypatch, caplog):
    monkeypatch.setattr(context.task, "_selfie_loop", AsyncMock(side_effect=RuntimeError("secret-token")))
    await context.task.start()
    running = context.task.task
    with pytest.raises(RuntimeError):
        await running
    await asyncio.sleep(0)
    assert context.task.is_running is False
    assert context.task.last_result["stage"] == "schedule"
    assert context.task.last_result["exception_type"] == "RuntimeError"
    assert "secret-token" not in caplog.text
    await context.task.stop()


@pytest.mark.asyncio
async def test_no_deduplication_between_explicit_executions(context):
    await context.task._execute_selfie()
    await context.task._execute_selfie()
    assert context.generation.await_count == 2
    assert context.caption.await_count == 2
    assert context.publish.await_count == 2


@pytest.mark.asyncio
async def test_next_day_gets_new_draws(context, monkeypatch):
    monkeypatch.setattr(auto.random, "random", Mock(return_value=0.5))
    await context.task._prepare_plan(datetime(2026, 10, 24, 7, tzinfo=ZONE))
    plan = await context.task._prepare_plan(datetime(2026, 10, 25, 7, tzinfo=ZONE))
    assert plan["key"][0] == "2026-10-25"
    assert plan["consumed"] == []
    assert auto.random.random.call_count == 8
    assert json.loads(context.task._plan_path.read_text())["key"][0] == "2026-10-25"


@pytest.mark.asyncio
async def test_overslept_segment_is_consumed_without_execution(context, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    context.task._plan = {"consumed": [], "segment_ends": [now - 1]}
    context.task.is_running = True
    calls = 0

    async def next_trigger(current):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0, now - 10
        context.task.is_running = False
        return None, now - 1

    monkeypatch.setattr(context.task, "_next_trigger", next_trigger)
    monkeypatch.setattr(context.task, "_execute_selfie", AsyncMock())
    await context.task._selfie_loop()
    assert context.task._plan["consumed"] == [0]
    assert json.loads(context.task._plan_path.read_text())["consumed"] == [0]
    context.task._execute_selfie.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_unknown_is_not_reported_as_failure(context):
    context.generation.return_value = {
        "success": False, "uncertain": True, "images": [],
        "failures": [{"message": "sensitive-timeout"}], "available_count": 0,
    }
    result = await context.task._execute_selfie()
    assert result["stage"] == "generation" and result["status"] == "unknown"
    assert result["success"] is False and "sensitive" not in json.dumps(result)
    context.generation.assert_awaited_once()
    context.caption.assert_not_awaited()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("count,available", [(2, 2), (2, 1), (1, 2)])
async def test_extra_or_mismatched_outputs_are_rejected_without_refill(context, count, available):
    context.generation.return_value = {
        "success": True, "images": [{"index": i, "base64": IMAGE} for i in range(count)],
        "available_count": available, "failures": [],
    }
    result = await context.task._execute_selfie()
    assert result["stage"] == "generation" and result["status"] == "failed"
    assert "单张协议" in result["message"]
    context.generation.assert_awaited_once()
    context.caption.assert_not_awaited()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_items_prevent_success_even_with_one_image(context):
    context.generation.return_value["failures"] = [{"index": 1, "message": "sensitive-item-error"}]
    result = await context.task._execute_selfie()
    assert result["stage"] == "generation" and result["status"] == "failed"
    assert "失败项" in result["message"] and "sensitive" not in json.dumps(result)
    context.generation.assert_awaited_once()
    context.caption.assert_not_awaited()
    context.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_cancellation_preserves_unknown(context):
    started = asyncio.Event()
    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    context.generation.side_effect = waiting
    task = asyncio.create_task(context.task._execute_selfie())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.task.last_result["stage"] == "generation"
    assert context.task.last_result["status"] == "unknown"
    context.publish.assert_not_awaited()
