"""自动自拍：按 MaiTrace 日程决定发空间、一个允许群或跳过。

一次周期只有一次发布尝试；超时或取消后不重试不确定的远端发布结果。
任务对象在配置热更新时保留冷却时间，不会立即重复同一轮自拍。
"""

from typing import TYPE_CHECKING, Optional

import asyncio
import base64
import logging
import random
import time

from ..utils.time_utils import is_in_time_range
from .caption_generator import generate_caption
from .proactive_context import activity_arguments, activity_block_reason, group_allowed, proactive_model, read_group_context, recheck_activity
from .publication_decision import decide_publication
from .schedule_provider import get_schedule_provider

if TYPE_CHECKING:
    from ...plugin import MaisArtPlugin

logger = logging.getLogger("plugin.mais_art_journal.auto_selfie")


class AutoSelfieTask:
    """带安静时段、活动限制、冷却和失败退避的自动自拍任务。"""

    def __init__(self, plugin: "MaisArtPlugin") -> None:
        self.plugin = plugin
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self._last_attempt_at: Optional[float] = None
        self._consecutive_failures = 0

    async def start(self) -> None:
        if self.is_running:
            return
        if not self.plugin.config.selfie.caption_enabled and not self.plugin.config.selfie.caption_text.strip():
            raise ValueError("关闭自动配文后必须配置非空的固定配文")
        self.is_running = True
        self.task = asyncio.create_task(self._selfie_loop(), name="mais-art:auto-selfie")
        logger.info("自动自拍任务已启动")

    async def stop(self) -> None:
        self.is_running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("自动自拍任务已停止")

    def _is_quiet_hours(self) -> bool:
        cfg = self.plugin.config.selfie
        return cfg.quiet_hours_start != cfg.quiet_hours_end and is_in_time_range(cfg.quiet_hours_start, cfg.quiet_hours_end)

    def _allowed(self) -> bool:
        cfg = self.plugin.config.selfie
        return self.plugin.config.plugin.enabled and cfg.enabled and cfg.auto_enabled and not self._is_quiet_hours()

    async def _selfie_loop(self) -> None:
        await asyncio.sleep(30)  # 启动延迟，等待其他插件完成 API 注册。
        while self.is_running:
            try:
                cfg = self.plugin.config.selfie
                interval = cfg.check_interval_minutes * 60 * (2 ** min(self._consecutive_failures // 3, 4))
                elapsed = time.monotonic() - self._last_attempt_at if self._last_attempt_at is not None else interval
                if elapsed >= interval:
                    async with asyncio.timeout(cfg.cycle_timeout_seconds):
                        await self._execute_selfie()
                    self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._consecutive_failures += 1
                logger.exception("自动自拍周期失败，本轮不会重试发布")
            await asyncio.sleep(30)

    async def _execute_selfie(self) -> None:
        cfg = self.plugin.config.selfie
        if not self._allowed():
            return
        if cfg.probability <= 0 or not (cfg.qzone_enabled or cfg.group_enabled):
            return
        # 判断游标持久化；失败、跳过和重启都不会立即重新判断本轮。
        self._last_attempt_at = time.monotonic()
        if not await self.plugin.proactive_state.claim_check("auto_selfie", cfg.check_interval_minutes * 60):
            return
        provider = get_schedule_provider(self.plugin.ctx, include_life=True)
        activity = await provider.get_current_activity()
        block = activity_block_reason(self.plugin, activity, automatic=True, before_generation=True)
        if block:
            logger.info("跳过自动自拍：%s", block)
            return
        assert activity is not None
        store = self.plugin.proactive_state
        qzone_allowed = cfg.qzone_enabled and store.available("qzone", cfg.interval_minutes * 60, source="auto_selfie", source_cooldown=cfg.interval_minutes * 60)
        groups = {}
        model_id = proactive_model(self.plugin, is_selfie=True, automatic=True)
        if cfg.group_enabled:
            for group_id in dict.fromkeys(self.plugin.config.group_image.group_ids):
                try:
                    context = await read_group_context(self.plugin, group_id)
                    if context is None or not context.messages:
                        continue
                    if not store.available(f"group:{context.session_id}", self.plugin.config.group_image.cooldown_minutes * 60, source="auto_selfie", source_cooldown=cfg.interval_minutes * 60):
                        continue
                    if activity.activity_type in self.plugin.config.group_image.disabled_activity_types:
                        continue
                    if not self.plugin.runtime_state.is_model_enabled(context.session_id, model_id):
                        continue
                    groups[f"group_{len(groups) + 1}"] = context
                except Exception:
                    logger.exception("读取自动自拍候选群 %s 失败，本轮排除该群", group_id)
        if not qzone_allowed and not groups:
            return
        decision = await decide_publication(self.plugin, activity, groups, qzone_allowed)
        logger.info("日程自拍决定：%s，理由：%s", decision["destination"], decision["reason"])
        if decision["destination"] == "skip" or random.random() >= cfg.probability:
            return
        if not self._allowed() or activity_block_reason(self.plugin, activity, automatic=True, before_generation=True):
            return
        group = groups.get(decision["destination"])
        target = f"group:{group.session_id}" if group is not None else "qzone"
        token = await store.reserve(
            source="auto_selfie", target=target,
            cooldown_seconds=self.plugin.config.group_image.cooldown_minutes * 60 if group is not None else cfg.interval_minutes * 60,
            source_cooldown=cfg.interval_minutes * 60, activity_key=activity.publication_key(),
            activity=activity.description, location=activity.location, prompt=decision["prompt"],
            reason=decision["reason"], is_selfie=True, messages=group.messages if group is not None else None,
        )
        if token is None:
            return
        outcome, reason = "failed", "自拍生成或发布失败"
        try:
            result = await self.plugin.api_generate_image(
                prompt=decision["prompt"], model_id=model_id,
                selfie_mode=True, selfie_style=cfg.default_style, use_cache=False,
                outfit=activity.outfit, recent_selfie_styles=store.recent_styles(),
                include_photo_plan=True,
                **activity_arguments(activity),
            )
            if not result.get("success") or not result.get("image_base64"):
                raise RuntimeError(f"自动自拍生图失败: {result.get('error', '没有图片')}")
            if not await store.remember_image(token, result["image_base64"], selfie_style=result.get("selfie_style", ""), outfit=result.get("outfit", "")):
                outcome, reason = "skipped", "生成结果与近期图片重复"
                return
            if cfg.caption_enabled:
                caption = await generate_caption(
                    self.plugin.ctx, activity, llm_task=self.plugin.config.basic.llm_task_name,
                    timeout_seconds=self.plugin.config.basic.llm_timeout_seconds,
                    destination="group" if group is not None else "qzone",
                    group_context=group.messages if group is not None else None,
                    outfit=result.get("outfit", activity.outfit),
                    photo_plan=result.get("photo_plan"),
                )
                if not caption:
                    raise RuntimeError("自动自拍配文生成失败，本轮不发布")
            else:
                caption = cfg.caption_text.strip()
                if not caption:
                    raise ValueError("自动自拍固定配文不能为空")
            if not self._allowed():
                outcome, reason = "skipped", "发布前配置已关闭"
                return
            block = await recheck_activity(self.plugin, activity, provider, automatic=True, group=group is not None)
            if block:
                outcome, reason = "skipped", f"发布前跳过：{block}"
                return
            if not self._allowed():
                outcome, reason = "skipped", "重查日程期间配置已关闭"
                return
            if group is not None:
                if not self.plugin.config.selfie.group_enabled or not group_allowed(self.plugin, group.group_id, group.session_id) or group.platform != self.plugin.config.group_image.platform or not self.plugin.runtime_state.is_model_enabled(group.session_id, model_id):
                    outcome, reason = "skipped", "发布前目标群或模型已禁用"
                    return
                outcome = "unknown"
                await self._publish_to_group(result["image_base64"], caption, group.session_id)
            else:
                if not self.plugin.config.selfie.qzone_enabled:
                    outcome, reason = "skipped", "发布前空间渠道已关闭"
                    return
                outcome = "unknown"
                await self._publish_to_qzone(result["image_base64"], caption)
            outcome, reason = "sent", decision["reason"]
        except BaseException as exc:
            reason = str(exc) or "本轮被取消"
            raise
        finally:
            await store.finish(token, outcome, reason)

    async def _publish_to_group(self, image_data: str, caption: str, session_id: str) -> None:
        """一次 SDK 图文消息把图片与配文一起发送，避免半次发布后重发。"""
        sent = await self.plugin.ctx.send.hybrid(
            [{"type": "image", "content": image_data}, {"type": "text", "content": caption}],
            session_id, timeout_ms=30000,
        )
        if sent is not True:
            raise RuntimeError("自拍群消息未确认发送成功，本轮不重发")
        logger.info("自拍已发到候选群 %s", session_id)

    async def _publish_to_qzone(self, image_data: str, caption: str) -> None:
        """send_feed_api 只发布已有图片，绝不要求 MaiTrace 再生成配图。"""
        if not caption.strip():
            raise ValueError("发布自拍需要非空正文")
        if not base64.b64decode(image_data, validate=True):
            raise ValueError("自拍图片不能为空")
        try:
            result = await self.plugin.ctx.call_capability(
                "api.call", timeout_ms=185000,
                api_name="Rabbit-Jia-Er.MaiTrace.send_feed_api", version="1",
                args={"message": caption, "images": [image_data]},
            )
        except asyncio.CancelledError:
            logger.warning("自拍发布请求被取消，远端投递状态待核对，本轮不重发")
            raise
        except TimeoutError as exc:
            raise TimeoutError("自拍发布请求超时，远端投递状态待核对，本轮不重发") from exc
        except Exception as exc:
            # Host 的 RPC 超时/断连以运行时异常传递；保留完整原因，不假定远端未发布。
            raise RuntimeError(f"自拍发布调用未确认完成，投递状态待核对，本轮不重发: {exc}") from exc
        if isinstance(result, dict) and result.get("uncertain") is True:
            raise RuntimeError(f"MaiTrace 自拍发布结果待核对，本轮不重发: {result.get('message', '')}")
        if not isinstance(result, dict) or result.get("result") is not True:
            raise RuntimeError(f"MaiTrace 直发自拍失败: {result}")
        logger.info("自拍已通过 MaiTrace 发布到空间")
