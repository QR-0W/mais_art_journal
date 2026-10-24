"""共享单次生成／取图能力，聊天投递与自动发布分别由调用方负责。"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

import asyncio

import httpx

from .api_clients.images_protocol import 描述错误, 构造参考图, 构造生成请求, 获取图片, 提交一次
from .config.models import ModelConfig
from .state import runtime_state

if TYPE_CHECKING:
    from ..plugin import MaisArtPlugin


def 选择生成模型(
    plugin: "MaisArtPlugin", model_id: str = "", count: int = 1,
    reference_count: int = 0, chat_id: str = "",
) -> ModelConfig:
    config = plugin.config
    if not config.plugin.enabled:
        raise ValueError("管理员尚未启用绘图插件")
    selected = model_id or config.basic.default_generation_model
    if not selected:
        raise ValueError("请管理员在 WebUI 设置全局默认模型，或本次明确指定模型")
    matches = [model for model in config.models.items if model.id == selected]
    if len(matches) != 1:
        raise ValueError(f"模型 {selected} 不存在或 ID 重复，不改选其他模型")
    model = matches[0]
    if chat_id and not runtime_state.is_model_enabled(chat_id, model.id):
        raise ValueError(f"当前聊天流已禁用模型 {model.id}，不改选其他模型")
    if model.format not in ("openai", "doubao"):
        raise ValueError("当前集合链路仅支持 OpenAI 兼容 Images 或火山原生 Images 协议")
    if not model.model.strip():
        raise ValueError("所选模型缺少提交给图像服务的模型标识")
    if type(count) is not int or count < 1:
        raise ValueError("输出数量必须是正整数")
    limit = min(config.basic.max_output_images, model.max_output_images)
    if count > limit:
        raise ValueError(f"请求 {count} 张超过本次上限 {limit}，不截小、不拆单")
    if reference_count and (
        not model.support_img2img
        or model.image_input_mode not in ("json_image", "multipart_edit")
        or (model.format == "doubao" and model.image_input_mode != "json_image")
        or reference_count > model.max_reference_images
    ):
        raise ValueError("所选模型的编辑方式或参考图数量不支持本次请求，不丢图、不改选")
    return model


def 校验生成输入(plugin: "MaisArtPlugin", args: Dict[str, Any]) -> ModelConfig:
    stream_id = args.get("stream_id", "")
    if not stream_id:
        raise ValueError("缺少真实聊天流，无法发送生成结果")
    chat_id = args.get("chat_id") or stream_id
    config = plugin.config
    if not config.basic.enable_unified_generation:
        raise ValueError("管理员尚未启用普通绘图")
    if not runtime_state.is_plugin_enabled(chat_id, config.plugin.enabled):
        raise ValueError("当前聊天流已停用绘图")
    description = args.get("description", "")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("需要明确的图片描述")
    operation = args.get("operation")
    references = args.get("references", [])
    if operation not in ("generate", "edit"):
        raise ValueError("需要明确选择 generate 或 edit")
    if not isinstance(references, list):
        raise ValueError("references 必须是有序图片引用列表")
    if operation == "edit" and not references:
        raise ValueError("无法确定编辑所需图片，本次结束，不转为文生图")
    if operation == "generate" and references:
        raise ValueError("带参考图的请求应使用 edit，不能忽略参考图")
    for ref in references:
        if not isinstance(ref, dict) or not isinstance(ref.get("source_id"), str) or not ref["source_id"].strip():
            raise ValueError("每张参考图都需要明确的 source_id")
        index = ref.get("index", 0)
        if type(index) is not int or index < 0:
            raise ValueError("图片 index 必须是从 0 开始的整数")
    if "strength" in args:
        raise ValueError("当前 Images 链路尚无已核实的 strength 参数契约")
    return 选择生成模型(
        plugin, args.get("model_id", ""), args.get("count", 1), len(references), chat_id,
    )


async def 生成图片集合(
    plugin: "MaisArtPlugin", model: ModelConfig, prompt: str, count: int = 1,
    references: Optional[List[str]] = None, size: str = "",
) -> Dict[str, Any]:
    """只生成和获取已有图片，不创建聊天流、不发送聊天或说说。images 为内部数据。"""
    reference_data = [] if references is None else references
    payload = 构造生成请求(model, prompt, count, reference_data, size)
    proxy = plugin.config.proxy
    async with httpx.AsyncClient(
        timeout=proxy.timeout,
        proxy=proxy.url if proxy.enabled else None,
        trust_env=False,
    ) as client:
        try:
            outputs = await 提交一次(client, model, payload)
        except httpx.TimeoutException:
            return {"success": False, "images": [], "uncertain": True,
                    "message": "生成调用超时，服务端结果未知；本次不自动重新提交。"}
        except (httpx.HTTPError, ValueError) as error:
            return {"success": False, "images": [],
                    "message": f"未取得图像输出：{描述错误(error)}；本次不自动重新提交。"}
        images: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        for output in outputs:
            try:
                image = await 获取图片(client, output)
                images.append({"index": output.index, "base64": image})
            except Exception as error:
                failures.append({"index": output.index, "stage": "acquire", "error": 描述错误(error)})
    return {
        "success": bool(images), "model_id": model.id, "requested_count": count,
        "returned_count": len(outputs), "available_count": len(images),
        "images": images, "failures": failures,
        "message": "图片已取得" if images else "接口返回项中没有可用图片，本次不补充生成。",
    }


async def 读取上下文参考图(plugin: "MaisArtPlugin", stream_id: str, references: List[Dict[str, Any]]) -> List[str]:
    images: List[str] = []
    for ref in references:
        result = await plugin.ctx.maisaka.context.resolve_image(
            stream_id, ref["source_id"], index=ref.get("index", 0),
        )
        if not result.get("success"):
            raise ValueError(f"无法读取参考图 {ref['source_id']}：{result.get('error', '未取得图片')}。"
                             "本次结束，不搜索历史或改用其他图片。")
        encoded = result["image"]["binary_data_base64"]
        images.append(await asyncio.to_thread(构造参考图, encoded))
    return images


async def 投递聊天图片(plugin: "MaisArtPlugin", stream_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    images = result.pop("images", [])
    if not result["success"]:
        return result
    sent_count = 0
    for image in images:
        try:
            sent = await plugin.ctx.send.image(
                image["base64"], stream_id, sync_to_maisaka_history=True,
                maisaka_source_kind="plugin_send_image",
            )
            if sent is not True:
                raise RuntimeError("发送图片未确认成功")
            sent_count += 1
        except Exception as error:
            result["failures"].append({"index": image["index"], "stage": "send", "error": 描述错误(error)})
    result["failures"].sort(key=lambda item: item["index"])
    count = result["requested_count"]
    message = f"请求 {count} 张，接口返回 {result['returned_count']} 项，取得 {result['available_count']} 张，成功发送 {sent_count} 张。"
    if result["failures"] or sent_count < count:
        message += "已有可用结果已逐项处理，不补充生成。"
    result.update(success=sent_count > 0, sent_count=sent_count, message=message)
    return result


async def 执行普通生成(plugin: "MaisArtPlugin", args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        model = 校验生成输入(plugin, args)
    except ValueError as error:
        return {"success": False, "message": str(error)}
    try:
        references = await 读取上下文参考图(plugin, args["stream_id"], args.get("references", []))
    except Exception as error:
        return {"success": False, "message": f"参考图读取失败：{描述错误(error)}；未提交生成。"}
    result = await 生成图片集合(
        plugin, model, args["description"].strip(), args.get("count", 1), references, args.get("size", ""),
    )
    return await 投递聊天图片(plugin, args["stream_id"], result)
