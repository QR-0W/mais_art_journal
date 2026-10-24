"""人设自拍共享入口；生成与投递分离，参考图不可被静默丢弃。"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from xml.etree.ElementTree import Element, SubElement, tostring

import asyncio
import base64

from ..api_clients.images_protocol import 描述错误, 构造参考图
from ..generation import 生成图片集合, 读取上下文参考图, 选择生成模型, 投递聊天图片
from ..state import runtime_state
from .schedule_provider import ActivityInfo, ApiScheduleProvider, LiveScheduleError

if TYPE_CHECKING:
    from ...plugin import MaisArtPlugin


拍摄要求 = {
    "standard": "自然的前置自拍构图，镜头外持机，画面中不要出现多余手机和手臂。",
    "mirror": "面对镜子拍摄，反射关系自然，只有一名角色。",
    "photo": "第三人称摄影构图，可以是全身或用户指定景别。",
}


def 组装自拍提示词(
    description: str, appearance: str, art_style: str, style: str,
    activity: Optional[ActivityInfo], negative: str = "",
) -> str:
    root = Element("selfie_request")
    SubElement(root, "identity").text = (
        "第一张参考图是角色身份依据，保持发型、发色、耳朵、五官和辨识特征。"
        "即使参考图是设定三视图，也只生成一个场景中的同一名角色，不复刻三视图排版。" + appearance
    )
    SubElement(root, "art_style").text = art_style
    SubElement(root, "schedule").text = activity.description if activity is not None else ""
    SubElement(root, "user_request").text = description
    SubElement(root, "camera").text = 拍摄要求[style]
    SubElement(root, "constraints").text = (
        "有日程时只使用日程场景，用户细节仅在相容时采纳；不能换到日程以外的场景。"
        "无日程时用户要求只作为创作场景，不冒充真实活动。拍摄角度和景别不改变角色身份。"
        "衣着得体，不输出文字、水印或角色设定版式。" + negative
    )
    return tostring(root, encoding="unicode", short_empty_elements=False)


def 读取人设参考图(plugin: "MaisArtPlugin") -> str:
    path = plugin.config.selfie.reference_image_path.strip()
    if not path:
        raise ValueError("尚未配置人设参考图，自拍未提交生成")
    file = Path(path)
    if not file.is_absolute():
        file = Path(plugin.plugin_dir) / file
    return 构造参考图(base64.b64encode(file.read_bytes()).decode("ascii"))


async def 生成自拍(
    plugin: "MaisArtPlugin", description: str = "", model_id: str = "", style: str = "",
    activity: Optional[ActivityInfo] = None, *, art_style: Optional[str] = None,
    extra_references: Optional[List[str]] = None, chat_id: str = "", size: str = "",
) -> Dict[str, Any]:
    selfie = plugin.config.selfie
    if not selfie.enabled:
        return {"success": False, "images": [], "message": "管理员尚未启用自拍"}
    selected_style = style or selfie.default_style
    if selected_style not in 拍摄要求:
        return {"success": False, "images": [], "message": "不支持的自拍拍摄方式"}
    references = [] if extra_references is None else extra_references
    try:
        model = 选择生成模型(plugin, model_id, 1, 1 + len(references), chat_id)
        persona = await asyncio.to_thread(读取人设参考图, plugin)
    except (ValueError, OSError) as error:
        return {"success": False, "images": [], "message": 描述错误(error)}
    prompt = 组装自拍提示词(
        description, selfie.prompt_prefix, selfie.art_style if art_style is None else art_style,
        selected_style, activity, selfie.negative_prompt,
    )
    return await 生成图片集合(plugin, model, prompt, 1, [persona, *references], size)


async def 执行手动自拍(plugin: "MaisArtPlugin", args: Dict[str, Any]) -> Dict[str, Any]:
    stream_id = args.get("stream_id", "")
    if not stream_id:
        return {"success": False, "message": "缺少真实聊天流，自拍未提交生成"}
    chat_id = args.get("chat_id") or stream_id
    if not runtime_state.is_plugin_enabled(chat_id, plugin.config.plugin.enabled):
        return {"success": False, "message": "当前聊天流已停用绘图"}
    if type(args.get("count", 1)) is not int or args.get("count", 1) != 1:
        return {"success": False, "message": "自拍每次只生成一张，不拆单补图"}
    if "strength" in args or args.get("free_hand_action"):
        return {"success": False, "message": "请将动作写入 description；新自拍不使用旧 strength/free_hand_action 参数"}
    if not isinstance(args.get("description"), str) or not args["description"].strip():
        return {"success": False, "message": "需要明确的自拍要求"}
    refs = args.get("references", [])
    if not isinstance(refs, list) or any(
        not isinstance(ref, dict) or not isinstance(ref.get("source_id"), str)
        or not ref["source_id"].strip() or type(ref.get("index", 0)) is not int or ref.get("index", 0) < 0
        for ref in refs
    ):
        return {"success": False, "message": "参考图需要有效的 source_id 和从0开始的整数 index"}
    try:
        选择生成模型(plugin, args.get("model_id", ""), 1, 1 + len(refs), chat_id)
        images = await 读取上下文参考图(plugin, stream_id, refs)
    except Exception as error:
        return {"success": False, "message": f"自拍素材读取失败：{描述错误(error)}；未提交生成。"}
    activity = None
    warning = ""
    if plugin.config.selfie.schedule_enabled:
        try:
            activity = await ApiScheduleProvider(plugin.ctx, chat_id).get_live_activity()
        except LiveScheduleError:
            warning = "日程 API 不可用，本次使用空日程段按用户创作要求生成，不将创作场景视作真实活动。"
    result = await 生成自拍(
        plugin, args["description"], args.get("model_id", ""), args.get("selfie_style", ""), activity,
        art_style=args.get("art_style"), extra_references=images, chat_id=chat_id, size=args.get("size", ""),
    )
    result = await 投递聊天图片(plugin, stream_id, result)
    if warning:
        result["warnings"] = [warning]
        result["message"] += f" {warning}"
    return result
