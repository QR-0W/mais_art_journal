"""普通绘图使用的单次 Images 协议，不复用旧客户端重试和域名分支。"""

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, List
from urllib.parse import urlsplit

import asyncio
import base64
import re

from PIL import Image
import httpx

from ..config.models import ModelConfig


@dataclass(frozen=True)
class 原生图片:
    index: int
    b64_json: str = ""
    url: str = ""
    error: str = ""


def 验证图片(data: bytes) -> str:
    if not data:
        raise ValueError("图片数据为空")
    with Image.open(BytesIO(data)) as image:
        mime = Image.MIME[image.format]
        image.verify()
    return mime


def 构造参考图(encoded: str) -> str:
    data = base64.b64decode(encoded, validate=True)
    return f"data:{验证图片(data)};base64,{encoded}"


def 构造生成请求(
    model: ModelConfig,
    prompt: str,
    count: int,
    references: List[str],
    size: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"model": model.model, "prompt": prompt}
    if model.format == "doubao":
        payload["sequential_image_generation"] = "disabled" if count == 1 else "auto"
        if count > 1:
            # max_images 是本次生成上限，模型实际返回数可以更少。
            payload["sequential_image_generation_options"] = {"max_images": count}
    else:
        payload["n"] = count
    image_size = model.default_size if model.fixed_size_enabled else size
    if image_size:
        payload["size"] = image_size
    if references:
        if model.image_input_mode not in ("json_image", "multipart_edit"):
            raise ValueError("所选模型未配置 JSON image 或 multipart 编辑协议，不能提交参考图")
        payload["image"] = (
            references[0] if model.format == "doubao" and model.image_input_mode == "json_image" and len(references) == 1
            else references
        )
    return payload


def 解析输出(payload: Dict[str, Any]) -> List[原生图片]:
    if payload.get("error") is not None:
        error = payload["error"]
        identifiers = {
            "code": error.get("code") if isinstance(error, dict) else None,
            "request_id": payload.get("request_id"),
        }
        # 只展示受限格式的定位标识，服务端 message 可能包含原请求或密钥。
        details = [
            f"{name}={value}" for name, value in identifiers.items()
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        ]
        suffix = f"（{', '.join(details)}）" if details else ""
        raise ValueError(f"图像服务返回业务错误{suffix}")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("图像接口未返回非空 data 集合，未取得可用输出集合")
    outputs = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            outputs.append(原生图片(i, error="返回项不是图像对象"))
        elif isinstance(item.get("b64_json"), str) and item["b64_json"]:
            outputs.append(原生图片(i, b64_json=item["b64_json"]))
        elif isinstance(item.get("url"), str) and item["url"]:
            outputs.append(原生图片(i, url=item["url"]))
        else:
            outputs.append(原生图片(i, error="返回项缺少 b64_json 或 url"))
    return outputs


async def 提交一次(
    client: httpx.AsyncClient,
    model: ModelConfig,
    payload: Dict[str, Any],
) -> List[原生图片]:
    base = model.base_url.rstrip("/")
    multipart = model.image_input_mode == "multipart_edit" and "image" in payload
    if urlsplit(base).scheme not in ("http", "https") or (base.endswith("/images/edits") and not multipart):
        raise ValueError("普通 Images 链路需要 HTTP(S) 服务根地址；images/edits 仅接受显式 multipart 参考图编辑")
    if multipart and base.endswith("/images/generations"):
        endpoint = base.removesuffix("/images/generations") + "/images/edits"
    else:
        suffix = "/images/edits" if multipart else "/images/generations"
        endpoint = base if base.endswith(suffix) else f"{base}{suffix}"
    key = model.api_key.strip()
    if not key:
        raise ValueError("所选模型没有配置 API 密钥")
    authorization = key if key.lower().startswith("bearer ") else f"Bearer {key}"
    if multipart:
        references = payload["image"]
        if not isinstance(references, list) or not references:
            raise ValueError("multipart 编辑需要非空的有序 DataURL 参考图列表")
        files = []
        field = "image" if len(references) == 1 else "image[]"
        # 上游已验证图片；这里严格还原全部 DataURL 后才发出唯一一次请求，不丢图。
        for i, reference in enumerate(references):
            match = re.fullmatch(r"data:(image/[A-Za-z0-9.+-]+);base64,(.+)", reference) if isinstance(reference, str) else None
            if match is None:
                raise ValueError(f"参考图 {i} 不是有效的图片 base64 DataURL")
            mime, encoded = match.groups()
            data = await asyncio.to_thread(base64.b64decode, encoded, validate=True)
            files.append((field, (f"reference-{i + 1}.{mime.removeprefix('image/')}", data, mime)))
        form = {name: str(value) for name, value in payload.items() if name != "image"}
        response = await client.post(
            endpoint, data=form, files=files, headers={"Authorization": authorization}, follow_redirects=False,
        )
    else:
        response = await client.post(
            endpoint, json=payload, headers={"Authorization": authorization}, follow_redirects=False,
        )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("图像接口响应不是 JSON 对象")
    return 解析输出(body)


async def 获取图片(client: httpx.AsyncClient, output: 原生图片) -> str:
    if output.error:
        raise ValueError(output.error)
    if output.b64_json:
        data = await asyncio.to_thread(base64.b64decode, output.b64_json, validate=True)
    else:
        if urlsplit(output.url).scheme not in ("http", "https"):
            raise ValueError("结果图片 URL 不是 HTTP(S) 地址")
        # 下载只使用图片 URL，不把生成接口的 Authorization 传给图片地址。
        response = await client.get(output.url, follow_redirects=True)
        response.raise_for_status()
        data = response.content
    await asyncio.to_thread(验证图片, data)
    return base64.b64encode(data).decode("ascii")


def 描述错误(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"图像服务返回 HTTP {error.response.status_code}"
    if isinstance(error, httpx.RequestError):
        return f"图像请求失败（{type(error).__name__}），请管理员检查连接与请求配置"
    return str(error)
