"""麦麦绘卷 SDK 2.x 插件入口（Pipeline 架构）

组件：
- @Tool draw_picture: 普通文/图生图和人设自拍走单次集合链路，手动只回聊天
- @Command dr: 单一入口，通过 CommandDispatcher 分发到 @subcommand 注册的 handlers
- @API generate_image: 对外暴露的生图接口，供其他插件 ctx.api.call 调用
- @API list_image_models: 对外暴露的模型清单接口

生命周期：
- on_load: 初始化 dispatcher + 加载 action pipeline + 启动自动自拍
- on_unload: 停止自动自拍
- on_config_update: 版本变更时备份配置 + 同步自动自拍开关
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from maibot_sdk import API, Command, MaiBotPlugin, Tool

from .core.config.models import MaisArtConfig

if TYPE_CHECKING:
    from .core.commands.dispatcher import CommandDispatcher
    from .core.pipeline import Pipeline
    from .core.selfie.auto_selfie_task import AutoSelfieTask

logger = logging.getLogger("plugin.mais_art_journal")


class MaisArtPlugin(MaiBotPlugin):
    """麦麦绘卷（Claude MAInet）智能多模型图片生成插件"""

    config_model = MaisArtConfig
    config_reload_subscriptions = ()  # 不订阅 bot/model 全局广播

    # 智能生图 Tool 描述（2026-08-23 精简：原 1058 字符过长，导致 MaiBot deferred-tools
    # system-reminder 拼成一行后格式异常、挤占上下文。见 GitHub issue#35。）
    # 描述保持精简；引用、数量与自拍本次画风放在对应参数，不重复系统提示。
    _DRAW_TOOL_DESCRIPTION = (
        "智能图片生成：根据描述生成图片（文生图）或基于现有图片进行修改（图生图）。"
        "仅在用户明确要求画图、生成图片、自拍、或对已有图片修改/换风格时使用。"
        "不要响应发给其他机器人的画图命令（如 /nai、/sd、/mj），不要替别人执行画图请求。"
    )

    _DRAW_TOOL_PARAMETERS = {
        "description": {
            "type": "string",
            "description": "图片描述文本（中文或英文均可），例如用户说'画一只小猫'则填写'一只小猫'。必填。",
            "required": True,
        },
        "operation": {
            "type": "string",
            "enum": ["generate", "edit"],
            "description": "普通请求必填：generate 为无参考图生成；edit 为基于已选参考图编辑。selfie_mode=true 时由人设图确定编辑输入。",
        },
        "references": {
            "type": "array",
            "description": "edit 所需的全部参考图，按提示词使用顺序排列，只从本轮可见内容选择；定位不明时说明失败，不猜图或查更早历史。",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "可见图片所属的消息编号或工具图片引用"},
                    "index": {"type": "integer", "minimum": 0, "default": 0, "description": "该消息内图片序号，从 0 开始"},
                },
                "required": ["source_id"],
                "additionalProperties": False,
            },
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "default": 1,
            "description": "普通绘图本次请求的张数，默认 1；原生单次生成，不因不足再次调用补图。",
        },
        "model_id": {
            "type": "string",
            "description": "可选的已配置模型 ID。未指定时只用管理员全局默认，普通绘图和自拍一致，不读取聊天流模型偏好、不因能力不足改选。",
        },
        "size": {
            "type": "string",
            "description": "本次图片尺寸。未填时仅在模型启用固定尺寸时传配置值，否则由服务决定；自拍同样适用。",
        },
        "selfie_mode": {
            "type": "boolean",
            "description": "可选，默认 false。自拍使用管理员人设图与当前日程，仅生成一张并回原聊天；不发布空间。",
        },
        "selfie_style": {
            "type": "string",
            "description": "自拍风格：standard（前置自拍）/ mirror（对镜自拍）/ photo（第三人称照片）。仅 selfie_mode=true 时生效。",
            "enum": ["standard", "mirror", "photo"],
        },
        "art_style": {
            "type": "string",
            "description": "仅当用户明确指定本次自拍画风时填写，临时覆盖默认画风，不改变人设身份图。",
        },
    }

    def __init__(self):
        super().__init__()
        self._plugin_dir: str = os.path.dirname(os.path.abspath(__file__))
        self._dispatcher: Optional["CommandDispatcher"] = None
        self._action_pipeline: Optional["Pipeline"] = None
        self._auto_selfie_task: Optional["AutoSelfieTask"] = None
        self._manual_test_task: Optional[asyncio.Task] = None
        self._test_executor: Optional["AutoSelfieTask"] = None
        self._manual_test_result: Dict[str, Any] = {"stage": "idle", "status": "idle", "message": "尚未运行单次测试"}

    # ── 公共属性 ──────────────────────────────────

    @property
    def plugin_dir(self) -> str:
        """插件目录绝对路径"""
        return self._plugin_dir

    def get_config(self, key: str, default: Any = None) -> Any:
        """嵌套 dot-path 配置读取（保留给 runtime_state / model_registry 等历史接口）"""
        config = self.get_plugin_config_data()
        current: Any = config
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current if current is not None else default

    # ── 生命周期 ──────────────────────────────────

    async def on_load(self) -> None:
        from .core.commands.dispatcher import CommandDispatcher
        from .core.pipeline import build_action_pipeline

        self._dispatcher = CommandDispatcher(self)
        self._action_pipeline = build_action_pipeline()

        if self.config.selfie.auto_enabled:
            from .core.selfie.auto_selfie_task import AutoSelfieTask
            task = AutoSelfieTask(self)
            await task.start()  # 成功后才赋值
            self._auto_selfie_task = task

        logger.info(f"麦麦绘卷已加载 (v{self.config.plugin.config_version}, Pipeline 架构)")

    async def on_unload(self) -> None:
        if self._manual_test_task is not None:
            self._manual_test_task.cancel()
            try:
                await self._manual_test_task
            except asyncio.CancelledError:
                pass
            self._manual_test_task = None
        if self._auto_selfie_task is not None:
            try:
                await self._auto_selfie_task.stop()
            except Exception as e:
                logger.warning(f"自动自拍任务停止失败: {e}")
        logger.info("麦麦绘卷已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return

        # 触发版本备份
        from .core.config.backup import backup_config_if_version_changed
        expected_version = self.config.plugin.config_version
        current_version = ""
        try:
            if isinstance(config_data, dict):
                plugin_section = config_data.get("plugin")
                if isinstance(plugin_section, dict):
                    current_version = str(plugin_section.get("config_version", ""))
        except Exception:
            current_version = ""
        backup_config_if_version_changed(
            self._plugin_dir, "config.toml", expected_version, current_version,
        )

        # 自动自拍随配置同步
        try:
            if self._auto_selfie_task is not None:
                await self._auto_selfie_task.stop()
                self._auto_selfie_task = None
            if self.config.selfie.auto_enabled:
                if self._manual_test_task is not None and not self._manual_test_task.done():
                    self._manual_test_task.cancel()
                    try:
                        await self._manual_test_task
                    except asyncio.CancelledError:
                        pass
                from .core.selfie.auto_selfie_task import AutoSelfieTask
                self._auto_selfie_task = AutoSelfieTask(self)
                await self._auto_selfie_task.start()
        except Exception as e:
            logger.warning(f"配置更新时调整自动自拍任务失败: {e}")

    # ── Tool: 智能生图 ──────────────────────────

    @Tool(
        "draw_picture",
        description=_DRAW_TOOL_DESCRIPTION,
        parameters=_DRAW_TOOL_PARAMETERS,
        timeout_ms=600000,
    )
    async def handle_draw_picture(self, **kwargs: Any) -> Union[Dict[str, Any], Tuple[bool, str]]:
        """智能生图入口，host 注入真实 stream_id 等上下文。

        普通请求和人设自拍复用单次生成层；自动发布独立触发。
        """
        selfie_mode = kwargs.get("selfie_mode", False)
        if not isinstance(selfie_mode, bool):
            return {"success": False, "message": "selfie_mode 必须是布尔值"}
        if not selfie_mode:
            from .core.generation import 执行普通生成

            return await 执行普通生成(self, kwargs)
        from .core.selfie.service import 执行手动自拍

        return await 执行手动自拍(self, kwargs)

    @API("selfie_test_status", description="管理员单次自拍测试的最近阶段，不返回密钥或图片数据")
    async def api_selfie_test_status(self) -> Dict[str, Any]:
        if self._test_executor is not None and self._test_executor.last_result:
            return dict(self._test_executor.last_result)
        return dict(self._manual_test_result)

    @API("start_selfie_preview", description="启动一次当前日程人设自拍预览，仅保存本地，可能产生生图费用")
    async def api_start_selfie_preview(self) -> Dict[str, Any]:
        return self._start_selfie_test(False)

    @API("start_selfie_publication", description="启动一次真实日程自拍并发布QQ空间，可能产生费用与公开可见内容")
    async def api_start_selfie_publication(self) -> Dict[str, Any]:
        return self._start_selfie_test(True)

    @API("start_preview_publication", description="管理员发布已核对摘要的本地预览图，不重新生图；只尝试一次发布")
    async def api_start_preview_publication(self, expected_sha256: str = "", message: str = "") -> Dict[str, Any]:
        import re

        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            return {"success": False, "message": "需要已确认预览图的 SHA256"}
        if not isinstance(message, str) or not message.strip():
            return {"success": False, "message": "需要已确认的发布正文"}
        if self._manual_test_task is not None and not self._manual_test_task.done():
            return {"success": False, "message": "已有单次测试运行中，请查询状态"}
        if self._auto_selfie_task is not None and self._auto_selfie_task.is_running:
            return {"success": False, "message": "请先关闭自动调度，再发布已确认预览"}
        self._test_executor = None
        self._manual_test_result = {"stage": "queued", "status": "running", "message": "正在核对已确认预览，不重新生图"}
        self._manual_test_task = asyncio.create_task(self._run_preview_publication(expected_sha256, message))
        return dict(self._manual_test_result)

    async def _run_preview_publication(self, expected_sha256: str, message: str) -> None:
        from pathlib import Path
        import base64
        import hashlib
        import re

        stage = "preview"
        try:
            def read_approved_preview() -> str:
                directory = Path(self.ctx.paths.data_dir) / "selfie_test"
                for file in directory.glob("preview-0.*"):
                    if file.is_file() and not file.is_symlink():
                        binary = file.read_bytes()
                        if hashlib.sha256(binary).hexdigest() == expected_sha256:
                            return base64.b64encode(binary).decode("ascii")
                raise ValueError("未找到摘要一致的预览图，未发布，不重新生成或替换")

            image = await asyncio.to_thread(read_approved_preview)
            stage = "publication"
            self._manual_test_result = {"stage": stage, "status": "running", "message": "正在发布已确认图文，不重新生图"}
            result = await self.ctx.api.call("Rabbit-Jia-Er.MaiTrace.send_feed_api", message=message, images=[image])
            if isinstance(result, dict) and result.get("uncertain") is not True and result.get("result") is True:
                self._manual_test_result = {"stage": stage, "status": "success", "success": True, "message": "已确认图文发布成功"}
                identifier = re.search(r"\btid=([A-Za-z0-9_-]+)", str(result.get("message", "")))
                if identifier:
                    self._manual_test_result["publication_id"] = identifier.group(1)
            else:
                status = "failed" if isinstance(result, dict) and result.get("result") is False and result.get("uncertain") is not True else "unknown"
                self._manual_test_result = {"stage": stage, "status": status, "success": False, "message": "未确认发布成功，不重试"}
        except asyncio.CancelledError:
            self._manual_test_result = {"stage": stage, "status": "unknown" if stage == "publication" else "cancelled", "success": False, "message": "测试等待取消，不重新提交"}
            raise
        except Exception as error:
            self._manual_test_result = {"stage": stage, "status": "unknown" if stage == "publication" else "failed", "success": False, "message": "未确认发布成功，不重试", "exception_type": type(error).__name__}

    def _start_selfie_test(self, publish: bool) -> Dict[str, Any]:
        if self._manual_test_task is not None and not self._manual_test_task.done():
            return {"success": False, "message": "已有单次测试运行中，请查询状态，不要重复提交"}
        if self._auto_selfie_task is not None and self._auto_selfie_task.is_running:
            return {"success": False, "message": "自动调度正在运行，请先关闭后再做手动测试"}
        self._test_executor = None
        self._manual_test_result = {"stage": "queued", "status": "running", "message": "已启动单次测试", "publish": publish}
        self._manual_test_task = asyncio.create_task(self._run_selfie_test(publish))
        return dict(self._manual_test_result)

    async def _run_selfie_test(self, publish: bool) -> None:
        from pathlib import Path
        import base64

        from .core.selfie.auto_selfie_task import AutoSelfieTask
        from .core.selfie.schedule_provider import ApiScheduleProvider, LiveScheduleError
        from .core.selfie.service import 生成自拍

        try:
            if publish:
                self._test_executor = AutoSelfieTask(self)
                self._manual_test_result = await self._test_executor._execute_selfie()
                return
            self._manual_test_result = {"stage": "activity", "status": "running", "message": "正在获取当前真实日程"}
            activity = await ApiScheduleProvider(self.ctx).get_live_activity()
            if activity is None:
                self._manual_test_result = {"stage": "activity", "status": "skipped", "message": "没有当前真实活动，不生成、不发布"}
                return
            self._manual_test_result = {"stage": "generation", "status": "running", "message": "正在生成本地自拍预览"}
            result = await 生成自拍(self, activity=activity)
            images = result.pop("images", [])
            if not result["success"]:
                self._manual_test_result = {"stage": "generation", "status": "unknown" if result.get("uncertain") else "failed", "message": result["message"]}
                return
            directory = Path(self.ctx.paths.data_dir) / "selfie_test"
            def save_images() -> List[str]:
                from io import BytesIO
                from PIL import Image

                directory.mkdir(parents=True, exist_ok=True)
                files = []
                for image in images:
                    binary = base64.b64decode(image["base64"], validate=True)
                    with Image.open(BytesIO(binary)) as picture:
                        extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}.get(picture.format, ".img")
                    file = directory / f"preview-{image['index']}{extension}"
                    file.write_bytes(binary)
                    files.append(str(file))
                return files
            files = await asyncio.to_thread(save_images)
            self._manual_test_result = {"stage": "preview", "status": "success", "message": "仅保存本地，没有发布空间", "files": files}
        except asyncio.CancelledError:
            if self._test_executor is not None:
                self._manual_test_result = dict(self._test_executor.last_result)
            elif self._manual_test_result.get("stage") == "generation":
                self._manual_test_result = {"stage": "generation", "status": "unknown", "message": "生成等待已取消；若已提交，服务端结果未知，不重提交"}
            else:
                self._manual_test_result = {"stage": "cancelled", "status": "cancelled", "message": "测试已取消，没有自动补做"}
            raise
        except LiveScheduleError as error:
            self._manual_test_result = {"stage": "activity", "status": "failed", "message": str(error)}
        except Exception as error:
            self._manual_test_result = {"stage": "test", "status": "failed", "message": "单次测试中止", "exception_type": type(error).__name__}

    # ── API: 对外暴露给其他插件调用 ───────────────────

    @API(
        "generate_image",
        description=(
            "生成图片（文生图 / 图生图，自动识别）并返回 base64，不发送到聊天流。"
            "其他插件可通过 ctx.api.call('1021143806.mais_art_journal.generate_image', "
            "prompt=..., model_id=..., input_image_base64=..., ...) 复用本插件配置的全部模型。"
        ),
        version="1",
        public=True,
    )
    async def api_generate_image(
        self,
        prompt: str = "",
        model_id: str = "",
        size: str = "",
        strength: Optional[float] = None,
        input_image_base64: Optional[str] = None,
        negative_prompt: str = "",
        selfie_mode: bool = False,
        selfie_style: str = "standard",
        free_hand_action: str = "",
        use_cache: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """对外 API：生成图片，返回 base64

        Args:
            prompt: 图片描述（必填，中英文均可；启用 prompt_optimizer 时会自动优化为英文 SD 提示词）
            model_id: 模型 ID（如 model1、model2），留空时按文生图/图生图场景自动选择默认模型
            size: 图片尺寸，如 "1024x1024"；留空由 LLM 自选或用模型默认
            strength: 图生图强度 0.1-1.0；仅 input_image_base64 非空时生效，默认 0.7
            input_image_base64: 图生图输入图（纯 base64 字符串，不含 data:image/... 前缀）
            negative_prompt: 额外负面提示词（合并到模型自身的 negative_prompt_add）
            selfie_mode: 是否启用自拍模式（默认 false）
            selfie_style: 自拍风格 standard/mirror/photo（仅 selfie_mode=true 生效）
            free_hand_action: 自由手部动作（英文，仅 selfie_mode=true 生效）
            use_cache: 是否参与缓存读写（默认 false，每次都重新生成）

        Returns:
            Dict[str, Any]: {
                "success": bool,                # 是否成功
                "image_base64": str,            # 成功时为生成图的 base64（不含前缀）
                "model_id": str,                # 实际使用的模型 ID
                "size": str,                    # 实际使用的尺寸
                "is_img2img": bool,             # 是否走的图生图分支
                "error": str,                   # 失败时的错误描述
            }

        Examples:
            ::

                result = await self.ctx.api.call(
                    "1021143806.mais_art_journal.generate_image",
                    prompt="一只在月光下打哈欠的银发狐妖",
                    model_id="model1",
                    size="1024x1024",
                )
                if result["success"]:
                    image_bytes = base64.b64decode(result["image_base64"])
        """
        del kwargs  # 预留扩展，当前未使用

        if not prompt or not str(prompt).strip():
            return {
                "success": False,
                "error": "prompt 不能为空",
                "image_base64": "",
                "model_id": "",
                "size": "",
                "is_img2img": False,
            }

        # 参数标准化
        strength_f: Optional[float] = None
        if strength is not None:
            try:
                strength_f = float(strength)
                if not (0.1 <= strength_f <= 1.0):
                    strength_f = 0.7
            except (ValueError, TypeError):
                strength_f = 0.7

        style = str(selfie_style or "standard").strip().lower()
        if style not in ("standard", "mirror", "photo"):
            style = "standard"

        # 构造 pipeline 请求（source=standalone：跳过 chat-stream 级的模型禁用检查）
        from .core.pipeline import (
            GenerationRequest,
            build_action_pipeline,
            make_pipeline_context,
        )

        req = GenerationRequest(
            description=str(prompt).strip(),
            model_id=str(model_id or "").strip(),
            size=str(size or "").strip(),
            strength=strength_f,
            input_image_base64=input_image_base64 or None,
            extra_negative_prompt=str(negative_prompt or "").strip() or None,
            is_selfie=bool(selfie_mode),
            selfie_style=style,  # type: ignore[arg-type]
            free_hand_action=str(free_hand_action or "").strip(),
            send_image=False,
            update_cache=bool(use_cache),
            schedule_recall=False,
            debug_info=False,
            silent_img2img_fallback=True,
            stream_id="",
            chat_id="",
            log_prefix="[API]",
            source="standalone",
        )

        pipeline = self._action_pipeline if self._action_pipeline is not None else build_action_pipeline()
        pipeline_ctx = make_pipeline_context(self, req)
        out = await pipeline.run(req, pipeline_ctx)

        return {
            "success": bool(out.success),
            "image_base64": out.resolved_image_data or "",
            "model_id": req.model_id,
            "size": req.final_size or req.size,
            "is_img2img": bool(req.is_img2img),
            "error": out.error or "",
        }

    @API(
        "list_image_models",
        description="列出当前插件已配置的图片生成模型（id / 名称 / API 格式 / 是否支持图生图）",
        version="1",
        public=True,
    )
    async def api_list_image_models(self, **kwargs: Any) -> Dict[str, Any]:
        """对外 API：列出可用模型

        Returns:
            Dict[str, Any]: {
                "success": bool,
                "default_model": str,           # basic.default_txt2img_model（兼容旧调用方）
                "default_txt2img_model": str,   # basic.default_txt2img_model
                "default_img2img_model": str,   # basic.default_img2img_model（留空时与文生图相同）
                "models": list[dict],           # [{"id", "name", "format", "model", "support_img2img"}]
            }
        """
        del kwargs
        try:
            from .core.config import (
                list_models,
                resolve_img2img_model,
                resolve_txt2img_model,
            )
            items = list_models(self)
            models = [
                {
                    "id": mid,
                    "name": cfg.get("name", ""),
                    "format": cfg.get("format", ""),
                    "model": cfg.get("model", ""),
                    "support_img2img": bool(cfg.get("support_img2img", True)),
                    "default_size": cfg.get("default_size", ""),
                }
                for mid, cfg in items.items()
            ]
            return {
                "success": True,
                "default_model": resolve_txt2img_model(self),
                "default_txt2img_model": resolve_txt2img_model(self),
                "default_img2img_model": resolve_img2img_model(self),
                "models": models,
            }
        except Exception as exc:
            logger.error(f"api_list_image_models 失败: {exc}", exc_info=True)
            return {"success": False, "error": str(exc), "models": []}

    # ── Command: /dr 全套（集中分发） ───────────────

    @Command(
        "dr",
        description="麦麦绘卷命令套件（生图 / 模型管理 / 风格管理 / 自拍开关）。命令前缀可在「基础配置.命令前缀」修改后重启 MaiBot 生效",
        pattern=r"^\s*(?:\[(?=[^\]]*(?:图片|image|表情|emoji))(?:[^\[\]]|\[[^\]]*\])*\]\s*)*(?:.*，说：\s*)?/dr(?:\s+(?P<rest>.+))?$",
    )
    async def handle_dr(self, **kwargs: Any):
        """命令入口：实际 pattern 由 get_components() 按 config.basic.command_prefix 动态注入

        装饰器里的 pattern 仅是默认占位（/dr）。get_components() 返回 host 之前会按
        当前 config.basic.command_prefix 重写 metadata.command_pattern 为精确前缀，
        避免抢占其他插件的命令（如 /mute on）。修改前缀后**重启 MaiBot** 即生效。
        """
        if self._dispatcher is None:
            logger.error("CommandDispatcher 尚未初始化")
            return False, "插件未就绪", True

        matched = kwargs.get("matched_groups") or {}
        message = kwargs.get("message")

        # host 平铺进来的上下文字段（component_query._build_command_executor 注入）
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = str(kwargs.get("user_id") or "")
        group_id = str(kwargs.get("group_id") or "")

        # 昵称/群名仅在 message dict 里携带，按需取
        user_nickname, group_name = _extract_display_names(message)

        # 引用拼接误触发检测：取用户自己实际输入的文本，跳过 reply / image 等段
        configured_prefix = (self.config.basic.command_prefix or "/dr").strip()
        if not configured_prefix.startswith("/"):
            configured_prefix = "/" + configured_prefix
        if _looks_like_quoted_command(message, configured_prefix):
            logger.info("检测到引用了别人的命令消息且用户自身未发命令，透传")
            return False, "quoted command from others", False

        rest = (matched.get("rest") or "").strip()
        return await self._dispatcher.dispatch(
            stream_id, message, rest,
            user_id=user_id,
            group_id=group_id,
            user_nickname=user_nickname,
            group_name=group_name,
        )

    # ── get_components 覆写：动态注入用户配置的精确命令前缀 ──

    def get_components(self) -> list[dict[str, Any]]:
        """注入 basic.command_prefix 到 dr command 的 pattern，避免宽匹配抢占其他插件"""
        components = super().get_components()

        # 使用 _plugin_config_data 而不是 self.config，避免配置未注入时的 RuntimeError
        try:
            basic_section = self._plugin_config_data.get("basic", {})
            if isinstance(basic_section, dict):
                prefix = basic_section.get("command_prefix", "/dr")
            else:
                prefix = "/dr"
        except Exception:
            prefix = "/dr"

        prefix = str(prefix or "/dr").strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        escaped = re.escape(prefix)
        # ^ 锚反"别的 bot 帮助文本"误触；放行前导空白和"含图片/表情"的方括号占位段
        # （覆盖附图/附 emoji/引用 这几种 processed_plain_text 形态）。
        bracket_media = (
            r"\[(?=[^\]]*(?:图片|image|表情|emoji))"
            r"(?:[^\[\]]|\[[^\]]*\])*\]"
        )
        new_pattern = (
            rf"^\s*(?:{bracket_media}\s*)*"
            rf"(?:.*，说：\s*)?{escaped}(?:\s+(?P<rest>.+))?$"
        )

        for comp in components:
            if (
                comp.get("name") == "dr"
                and str(comp.get("type", "")).upper() == "COMMAND"
            ):
                metadata = comp.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    comp["metadata"] = metadata
                metadata["command_pattern"] = new_pattern
                logger.info(f"麦麦绘卷命令前缀生效: {prefix!r} (pattern={new_pattern!r})")
                break

        return components


def create_plugin() -> MaisArtPlugin:
    """SDK Runner 创建插件实例的入口"""
    return MaisArtPlugin()


def _extract_user_text_segments(message: Any) -> tuple[str, bool]:
    """从消息对象提取"用户自身实际输入的纯文本"

    跳过 ReplyComponent / ImageComponent / EmojiComponent / AtComponent 等非用户文本段，
    仅拼接 TextComponent。用于区分"用户自己发的命令"和"引用别人的命令消息后拼接出来的文本"。

    支持三种消息形态：
    1. SDK 2.x 的 RPC 序列化 dict：``message["raw_message"]`` 是 list[dict]，
       每段 ``{"type": "text"|"image"|"reply"|...}``。
    2. SessionMessage（同进程内）：``message.raw_message.components`` 列表。
    3. 老 message_segment 形态：``message.message_segment`` 是 Seg 树。

    Returns:
        (user_text, has_reply): 用户净输入文本 + 该消息是否含引用段
    """
    if message is None:
        return "", False

    parts: list[str] = []
    has_reply = False

    # 路径 1：dict 形态（SDK 2.x command 入口）
    if isinstance(message, dict):
        raw = message.get("raw_message")
        if isinstance(raw, list):
            for seg in raw:
                if not isinstance(seg, dict):
                    continue
                stype = str(seg.get("type") or "").lower()
                if stype == "reply":
                    has_reply = True
                    continue
                if stype == "text":
                    text = seg.get("data")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts).strip(), has_reply
        # raw_message 不是 list（罕见）→ 兜底走 reply_to 标记
        if message.get("reply_to"):
            has_reply = True
        return "", has_reply

    # 路径 2：SessionMessage / MaiMessage —— message.raw_message.components
    raw_msg = getattr(message, "raw_message", None)
    components = getattr(raw_msg, "components", None) if raw_msg is not None else None
    if isinstance(components, (list, tuple)) and components:
        for comp in components:
            cls_name = type(comp).__name__
            if cls_name == "ReplyComponent":
                has_reply = True
                continue
            if cls_name == "TextComponent":
                text = getattr(comp, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                continue
            # 其他组件（Image/Emoji/At/Voice/Forward）忽略
        return "".join(parts).strip(), has_reply

    # 路径 3：老 Seg 树 —— message.message_segment
    seg = getattr(message, "message_segment", None)
    if seg is not None:
        def _walk(s: Any) -> None:
            nonlocal has_reply
            if s is None:
                return
            stype = getattr(s, "type", None)
            sdata = getattr(s, "data", None)
            if stype == "reply":
                has_reply = True
                return
            if stype == "text" and isinstance(sdata, str):
                parts.append(sdata)
                return
            if stype == "seglist" and isinstance(sdata, (list, tuple)):
                for child in sdata:
                    _walk(child)

        _walk(seg)
        return "".join(parts).strip(), has_reply

    # 路径 4：兜底 —— message.reply_to 非空表示这是引用消息
    reply_to = getattr(message, "reply_to", None)
    if reply_to:
        has_reply = True
    return "", has_reply


def _extract_display_names(message: Any) -> tuple[str, str]:
    """从消息对象提取 (user_nickname, group_name)，仅用于日志前缀。

    优先 SDK 2.x dict 形态，其次老对象形态；取不到返回空串。
    """
    if message is None:
        return "", ""

    if isinstance(message, dict):
        msg_info = message.get("message_info") or {}
        if not isinstance(msg_info, dict):
            return "", ""
        user_info = msg_info.get("user_info") or {}
        group_info = msg_info.get("group_info") or {}
        nick = ""
        gname = ""
        if isinstance(user_info, dict):
            nick = str(user_info.get("user_nickname") or user_info.get("user_cardname") or "")
        if isinstance(group_info, dict):
            gname = str(group_info.get("group_name") or "")
        return nick, gname

    msg_info = getattr(message, "message_info", None)
    if msg_info is None:
        return "", ""
    user_info = getattr(msg_info, "user_info", None)
    group_info = getattr(msg_info, "group_info", None)
    nick = ""
    gname = ""
    if user_info is not None:
        nick = str(getattr(user_info, "user_nickname", "") or getattr(user_info, "user_cardname", "") or "")
    if group_info is not None:
        gname = str(getattr(group_info, "group_name", "") or "")
    return nick, gname


def _looks_like_quoted_command(message: Any, configured_prefix: str) -> bool:
    """判断 processed_plain_text 的命令命中是不是"引用别人命令消息"误触发

    判定：消息含 reply 段 + 用户自己的纯文本不以 /<prefix> 开头 → 误触发。
    若没有 reply 段（用户自己发的命令），返回 False。
    若有 reply 段但用户自己也输入了 /<prefix> ...（如对图发 /dv cartoon），返回 False。
    """
    user_text, has_reply = _extract_user_text_segments(message)
    if not has_reply:
        return False
    # 用户自己的输入为空 或 不以前缀开头 → 误触发
    prefix_with_slash = configured_prefix if configured_prefix.startswith("/") else "/" + configured_prefix
    if not user_text:
        return True
    lower_text = user_text.lstrip().lower()
    lower_prefix = prefix_with_slash.lower()
    if not (lower_text == lower_prefix or lower_text.startswith(lower_prefix + " ")):
        return True
    return False
