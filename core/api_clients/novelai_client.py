"""原生 NovelAI 单图客户端。

适配绘卷的单图契约，负责输入校验、有限读取和错误处理。
"""

from io import BytesIO
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import ValidationError
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import base64
import binascii
import json
import math
import re
import requests
import secrets
import zipfile

from ..config.novelai import NOVELAI_FIELD_NAMES, NovelAIConfig
from ..prompts.novelai_prompt import convert_novelai_weights
from .base_client import BaseApiClient, NonRetryableError, logger


MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# 25 MiB 图片转为 Base64 后约为 33.4 MiB，JSON 响应另留包装余量。
MAX_JSON_RESPONSE_BYTES = 36 * 1024 * 1024
MAX_SOURCE_PIXELS = 16 * 1024 * 1024
MAX_ZIP_ENTRIES = 16
_SAMPLERS = {
    "k_euler_ancestral", "k_euler", "k_dpmpp_2s_ancestral",
    "k_dpmpp_2m_sde", "k_dpmpp_2m", "k_dpmpp_sde",
}
_MODEL_VERSIONS = {
    "nai-diffusion-3": "v3",
    "nai-diffusion-furry-3": "v3",
    "nai-diffusion-4-full": "v4",
    "nai-diffusion-4-curated-preview": "v4",
    "nai-diffusion-4-5-full": "v4.5",
    "nai-diffusion-4-5-curated": "v4.5",
    "nai-diffusion-5-full": "v5",
    "nai-diffusion-5-curated": "v5",
}
_QUALITY_TAGS = {
    "nai-diffusion-3": "best quality, amazing quality, very aesthetic, absurdres",
    "nai-diffusion-furry-3": "{best quality}, {amazing quality}",
    "nai-diffusion-4-full": "no text, best quality, very aesthetic, absurdres",
    "nai-diffusion-4-curated-preview": "rating:general, best quality, very aesthetic, absurdres",
    "nai-diffusion-4-5-full": "very aesthetic, masterpiece, no text",
    "nai-diffusion-4-5-curated": "very aesthetic, masterpiece, no text, -0.8::feet::, rating:general",
    "nai-diffusion-5-full": "very aesthetic, masterpiece, no text",
    "nai-diffusion-5-curated": "very aesthetic, masterpiece, no text",
}


def _join_prompt(*parts: str) -> str:
    return ", ".join(part.strip(" \t\r\n,") for part in parts if part.strip(" \t\r\n,"))


def _add_quality_tags(prompt: str, model: str) -> str:
    """保留 V3 混合提示词末尾权重和 V4+ Text 区块的位置。"""
    suffix = _QUALITY_TAGS[model]

    def append(part: str) -> str:
        if suffix.casefold() in part.casefold():
            return part
        weight = re.search(r"(:-?\d+(?:\.\d+)?)\s*$", part)
        if weight is not None:
            return _join_prompt(part[:weight.start()], suffix) + weight.group(1)
        return _join_prompt(part, suffix)

    if _MODEL_VERSIONS[model] == "v3":
        return "|".join(append(part) for part in prompt.split("|"))
    text_section = re.search(r"\n(?=\s*Text:)", prompt, flags=re.IGNORECASE)
    if text_section is not None:
        return append(prompt[:text_section.start()]) + prompt[text_section.start():]
    return append(prompt)


def _number(value: Any, name: str, minimum: float, maximum: float, *, integer: bool = False) -> float:
    """对来自独立 API 字典的参数也执行约束，不依赖 WebUI 已经验证。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NonRetryableError(f"NovelAI {name} 必须是数值")
    if value < minimum or value > maximum or not math.isfinite(value):
        raise NonRetryableError(f"NovelAI {name} 须在 {minimum:g} 到 {maximum:g} 之间")
    if integer and not isinstance(value, int):
        raise NonRetryableError(f"NovelAI {name} 必须是整数")
    return value


def _text(config: Dict[str, Any], key: str, default: str = "") -> str:
    value = config.get(key, default)
    if not isinstance(value, str):
        raise NonRetryableError(f"NovelAI {key} 必须是字符串")
    return value


class NovelAIClient(BaseApiClient):
    """始终生成并返回一张图片，不在失败后改变尺寸、模型或生成方式。"""

    format_name = "novelai"

    async def generate_image(
        self, prompt: str, model_config: Dict[str, Any], size: str,
        strength: Optional[float] = None, input_image_base64: Optional[str] = None,
        max_retries: int = 2,
    ) -> Tuple[bool, str]:
        # 一次生成的连接重试复用相同种子与幂等键，调用方配置不被修改。
        prepared_config = dict(model_config)
        seed = prepared_config.get("seed", -1)
        if isinstance(seed, int) and not isinstance(seed, bool) and seed == -1:
            prepared_config["seed"] = secrets.randbelow(2**32)
        prepared_config["_novelai_idempotency_key"] = secrets.token_hex(16)
        return await super().generate_image(
            prompt, prepared_config, size, strength, input_image_base64, max_retries,
        )

    def _make_request(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        strength: Optional[float] = None,
        input_image_base64: Optional[str] = None,
    ) -> Tuple[bool, str]:
        settings = self._settings(model_config)
        payload = self._build_payload(prompt, model_config, size, settings, strength, input_image_base64)
        base_url = _text(model_config, "base_url", "https://image.novelai.net").strip().rstrip("/")
        try:
            parts = urlsplit(base_url)
        except ValueError as exc:
            raise NonRetryableError("NovelAI base_url 格式无效") from exc
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise NonRetryableError("NovelAI base_url 须为不含账号、查询参数或片段的 HTTP(S) API 地址")
        if settings.api_mode == "json":
            if parts.path.endswith("/v1/nai/generate-image"):
                endpoint = base_url
            elif parts.path.endswith("/v1"):
                endpoint = base_url + "/nai/generate-image"
            else:
                endpoint = base_url + "/v1/nai/generate-image"
        else:
            endpoint = base_url + "/ai/generate-image"
        token = re.sub(r"^(?i:bearer\s+)+", "", _text(model_config, "api_key").strip())
        if not token or token.casefold() == "bearer" or any(ord(character) < 33 or ord(character) > 126 for character in token):
            raise NonRetryableError("NovelAI API 密钥为空或含无效字符")
        request_args: Dict[str, Any] = {
            "url": endpoint,
            "headers": {
                "Authorization": "Bearer " + token,
                "Accept": "application/json" if settings.api_mode == "json" else "application/zip, image/png",
                "Content-Type": "application/json",
            },
            "json": payload,
            "timeout": (min(10, settings.request_timeout_seconds), settings.request_timeout_seconds),
            "stream": True,
            "allow_redirects": False,
        }
        if settings.api_mode == "json":
            request_args["headers"]["Idempotency-Key"] = model_config.get("_novelai_idempotency_key") or secrets.token_hex(16)
        proxy = self._get_proxy_config()
        if proxy is not None:
            request_args["proxies"] = {"http": proxy["http"], "https": proxy["https"]}
        logger.info("%s (NovelAI) 生成单张图片：model=%s action=%s size=%s", self.log_prefix, payload["model"], payload["action"], size)
        try:
            with requests.post(**request_args) as response:
                content = self._read_response(response, MAX_JSON_RESPONSE_BYTES if settings.api_mode == "json" else MAX_RESPONSE_BYTES)
                if response.status_code not in {200, 201}:
                    reason = self._error_message(content, token)
                    raise NonRetryableError(f"NovelAI 请求失败：HTTP {response.status_code}，{reason}")
                image_bytes = self._extract_image(content, response.headers.get("Content-Type", ""), token)
        except requests.ConnectTimeout:
            # 尚未建立连接，允许沿用 BaseApiClient 的显式重试次数。
            return False, "NovelAI 连接超时，尚未建立连接"
        except requests.Timeout as exc:
            raise NonRetryableError("NovelAI 响应超时，服务端是否已生成无法确认，本次不自动重试") from exc
        except requests.RequestException as exc:
            # 断线可能发生在服务端已接收请求之后，不能通过重发猜测结果。
            raise NonRetryableError(f"NovelAI 网络请求失败（{type(exc).__name__}），本次不自动重试") from exc
        return True, base64.b64encode(image_bytes).decode("ascii")

    @staticmethod
    def _settings(model_config: Dict[str, Any]) -> NovelAIConfig:
        try:
            # 兼容独立调用方的旧字典；正式模型配置已在升级入口转为直接字段。
            value = NovelAIConfig.model_validate(model_config.get("novelai", {})).model_dump()
            value.update({name: model_config[field] for name, field in NOVELAI_FIELD_NAMES.items() if field in model_config})
            return NovelAIConfig.model_validate(value)
        except ValidationError as exc:
            details = "; ".join(
                ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
                for error in exc.errors(include_input=False, include_url=False)
            )
            raise NonRetryableError(f"NovelAI 专属配置无效：{details}") from exc

    @classmethod
    def _build_payload(
        cls,
        prompt: str,
        model_config: Dict[str, Any],
        size: str,
        settings: NovelAIConfig,
        strength: Optional[float] = None,
        input_image_base64: Optional[str] = None,
    ) -> Dict[str, Any]:
        model = _text(model_config, "model").strip()
        if model not in _MODEL_VERSIONS:
            raise NonRetryableError(f"NovelAI 不支持模型 {model!r}，请配置已支持的 V3/V4/V4.5/V5 官方型号")
        version = _MODEL_VERSIONS[model]
        if not isinstance(prompt, str) or not prompt.strip():
            raise NonRetryableError("NovelAI 正向提示词不能为空")
        dimensions = re.fullmatch(r"([0-9]{2,4})[xX]([0-9]{2,4})", size.strip()) if isinstance(size, str) else None
        if dimensions is None:
            raise NonRetryableError("NovelAI 尺寸须为像素格式，例如 832x1216")
        width, height = (int(part) for part in dimensions.groups())
        if any(value < 64 or value > 2048 or value % 64 for value in (width, height)):
            raise NonRetryableError("NovelAI 宽高须为 64 到 2048 之间的 64 倍数")
        if settings.api_mode == "json" and width * height > 3_145_728:
            raise NonRetryableError("NovelAI JSON 接口总像素不得超过 3,145,728")
        is_img2img = input_image_base64 is not None
        if version == "v3":
            expected_mode = "furry" if model == "nai-diffusion-furry-3" else "anime"
            if settings.mode != expected_mode:
                raise NonRetryableError(f"NovelAI 模型 {model} 须使用 {expected_mode} 模式；请显式选择对应模型")
        elif version == "v4" and settings.mode == "background":
            raise NonRetryableError("NovelAI background 模式仅支持 V4.5/V5")
        if settings.smea_dynamic and not settings.smea:
            raise NonRetryableError("NovelAI 动态 SMEA 须同时开启 SMEA")
        if settings.smea and (version != "v3" or is_img2img):
            raise NonRetryableError("NovelAI SMEA 仅支持 V3 文生图")
        sampler = _text(model_config, "sampler", "k_euler_ancestral")
        if sampler not in _SAMPLERS:
            raise NonRetryableError(f"NovelAI 不支持采样器 {sampler!r}")
        noise_schedule = _text(model_config, "noise_schedule", "karras")
        schedules = {"karras", "native", "exponential", "polyexponential"} if version == "v3" else {"karras", "exponential", "polyexponential"}
        if version == "v5":
            schedules = {"karras"}
        if noise_schedule not in schedules:
            raise NonRetryableError(f"NovelAI {version} 不支持噪声调度 {noise_schedule!r}，可选：{', '.join(sorted(schedules))}")
        seed = int(_number(model_config.get("seed", -1), "seed", -1, 2**32 - 1, integer=True))
        if seed == -1:
            seed = secrets.randbelow(2**32)
        # 上层已完成 UC 合并；正向增强词只在本客户端追加一次。
        full_prompt = _join_prompt(_text(model_config, "artist"), prompt, _text(model_config, "custom_prompt_add"))
        if version != "v3" and settings.mode in {"furry", "background"}:
            dataset = "fur dataset" if settings.mode == "furry" else "background dataset"
            if not full_prompt.casefold().startswith(dataset):
                full_prompt = _join_prompt(dataset, full_prompt)
        if settings.quality_tags:
            full_prompt = _add_quality_tags(full_prompt, model)
        try:
            full_prompt = convert_novelai_weights(full_prompt, v3=version == "v3")
            negative_prompt = convert_novelai_weights(_text(model_config, "negative_prompt_add"), v3=version == "v3")
        except ValueError as exc:
            raise NonRetryableError(str(exc)) from exc
        parameters: Dict[str, Any] = {
            "params_version": 4, "width": width, "height": height,
            "n_samples": 1, "seed": seed, "sampler": sampler,
            "steps": int(_number(model_config.get("num_inference_steps", 28), "steps", 1, 50, integer=True)),
            "scale": _number(model_config.get("guidance_scale", 5.0), "scale", 0 if settings.api_mode == "json" else 0.1, 10 if settings.api_mode == "json" else 20),
            "cfg_rescale": _number(model_config.get("cfg", 0.0), "CFG Rescale", 0.0, 1.0),
            "noise_schedule": noise_schedule, "negative_prompt": negative_prompt,
            "legacy": False, "legacy_v3_extend": False, "dynamic_thresholding": False,
        }
        if version == "v3":
            if not is_img2img:
                parameters.update(sm=settings.smea, sm_dyn=settings.smea_dynamic)
        else:
            parameters.update(
                legacy_uc=False,
                v4_prompt={"caption": {"base_caption": full_prompt, "char_captions": []}, "use_coords": False, "use_order": True},
                v4_negative_prompt={"caption": {"base_caption": negative_prompt, "char_captions": []}},
            )
        if sampler == "k_euler_ancestral" and noise_schedule != "native":
            parameters.update(deliberate_euler_ancestral_bug=False, prefer_brownian=True)
        if is_img2img:
            parameters.update(
                image=cls._prepare_source(input_image_base64, width, height),
                strength=_number(settings.img2img_strength if strength is None else strength, "图生图强度", 0, 1),
                noise=settings.img2img_noise, extra_noise_seed=seed, add_original_image=False,
            )
        elif strength is not None:
            raise NonRetryableError("NovelAI strength 仅用于有源图的图生图请求")
        return {"input": full_prompt, "model": model, "action": "img2img" if is_img2img else "generate", "parameters": parameters}

    @classmethod
    def _prepare_source(cls, encoded: str, width: int, height: int) -> str:
        if not isinstance(encoded, str) or not encoded:
            raise NonRetryableError("NovelAI 参考图必须是非空的单张 Base64 图片")
        if encoded.startswith("data:"):
            prefix, separator, encoded = encoded.partition(",")
            if not separator or not re.fullmatch(r"data:image/[a-zA-Z0-9.+-]+;base64", prefix):
                raise NonRetryableError("NovelAI 参考图 Data URI 格式无效")
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise NonRetryableError("NovelAI 参考图超过 25 MiB 大小限制")
        try:
            source = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise NonRetryableError("NovelAI 参考图 Base64 编码无效") from exc
        cls._validate_image(source, "参考图")
        try:
            with Image.open(BytesIO(source)) as image:
                # 方向校正后保持比例居中裁切，确定性地适配本次已选择的尺寸。
                oriented = ImageOps.exif_transpose(image)
                if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    prepared = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    prepared = oriented.convert("RGB")
                prepared = ImageOps.fit(prepared, (width, height), method=Image.Resampling.LANCZOS)
                output = BytesIO()
                prepared.save(output, format="PNG")
                image_bytes = output.getvalue()
        except (OSError, ValueError) as exc:
            raise NonRetryableError(f"NovelAI 参考图尺寸处理失败：{exc}") from exc
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise NonRetryableError("NovelAI 处理后的参考图超过 25 MiB 大小限制")
        return base64.b64encode(image_bytes).decode("ascii")

    @staticmethod
    def _read_response(response: requests.Response, maximum: int = MAX_RESPONSE_BYTES) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise NonRetryableError("NovelAI 响应的 Content-Length 无效") from exc
            if declared_size < 0 or declared_size > maximum:
                raise NonRetryableError(f"NovelAI 响应超过 {maximum // (1024 * 1024)} MiB 大小限制或长度无效")
        chunks = BytesIO()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunks.tell() + len(chunk) > maximum:
                raise NonRetryableError(f"NovelAI 响应超过 {maximum // (1024 * 1024)} MiB 大小限制")
            chunks.write(chunk)
        return chunks.getvalue()

    @classmethod
    def _extract_image(cls, content: bytes, content_type: str, token: str = "") -> bytes:
        if not content:
            raise NonRetryableError("NovelAI 返回了空响应")
        if zipfile.is_zipfile(BytesIO(content)):
            try:
                with zipfile.ZipFile(BytesIO(content)) as archive:
                    members = archive.infolist()
                    if len(members) > MAX_ZIP_ENTRIES or sum(member.file_size for member in members) > MAX_RESPONSE_BYTES:
                        raise NonRetryableError("NovelAI ZIP 文件数量或解压总大小超过限制")
                    images = [member for member in members if not member.is_dir() and member.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                    if len(images) != 1:
                        raise NonRetryableError(f"NovelAI 单图请求应返回恰好一张图片，ZIP 中找到 {len(images)} 张")
                    member = images[0]
                    if member.file_size > MAX_IMAGE_BYTES or member.flag_bits & 1:
                        raise NonRetryableError("NovelAI ZIP 图片超过 25 MiB 限制或使用了加密")
                    if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise NonRetryableError("NovelAI ZIP 使用了不支持的压缩格式")
                    with archive.open(member) as image_file:
                        image_bytes = image_file.read(MAX_IMAGE_BYTES + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                raise NonRetryableError(f"NovelAI ZIP 解析失败：{exc}") from exc
        elif content_type.lower().split(";", 1)[0] in {"image/png", "image/jpeg", "image/webp"}:
            image_bytes = content
        elif "json" in content_type.lower():
            return cls._extract_json_image(content, token)
        else:
            raise NonRetryableError("NovelAI 响应不是原生 ZIP 或支持的图片格式")
        cls._validate_image(image_bytes, "生成图片")
        return image_bytes

    @classmethod
    def _extract_json_image(cls, content: bytes, token: str) -> bytes:
        """解析原生 NAI JSON 单图响应，拒绝错误、损坏或多图结果。"""
        try:
            body = json.loads(content)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise NonRetryableError("NovelAI JSON 响应格式无效") from exc
        if not isinstance(body, dict):
            raise NonRetryableError("NovelAI JSON 响应须为对象")
        if body.get("error") or body.get("success") is False or "images" not in body:
            raise NonRetryableError("NovelAI JSON 返回错误：" + cls._error_message(content, token))
        job = body.get("job")
        if job is not None and not isinstance(job, dict):
            raise NonRetryableError("NovelAI JSON job 字段须为对象")
        status = job.get("status") if isinstance(job, dict) else None
        if status is not None and not isinstance(status, str):
            raise NonRetryableError("NovelAI JSON job.status 须为字符串")
        normalized_status = status.strip().casefold() if isinstance(status, str) else ""
        # job 是附加任务信息，不要求与文档示例使用相同的成功状态名称。
        # 缺失/未知状态时仍须通过完整单图校验，显式失败及未完成状态不能投递。
        blocked_states = {
            "failed", "failure", "error", "cancelled", "canceled", "timeout", "timed_out",
            "queued", "pending", "running", "processing", "generating", "submitted",
        }
        safe_status = status.replace(token, "[已隐藏密钥]")[:80] if status and token else (status or "<missing>")[:80]
        safe_status = re.sub(r"[\r\n\x00-\x1f\x7f]", " ", safe_status)
        if normalized_status in blocked_states:
            raise NonRetryableError(f"NovelAI JSON 生成任务未成功：status={safe_status}，不自动重试")
        images = body["images"]
        if not isinstance(images, list) or len(images) != 1:
            raise NonRetryableError("NovelAI JSON 响应须包含恰好一张图片")
        encoded = images[0]
        if not isinstance(encoded, str) or not encoded:
            raise NonRetryableError("NovelAI JSON 图片须为非空 Base64 字符串")
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise NonRetryableError("NovelAI JSON 图片超过 25 MiB 大小限制")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise NonRetryableError("NovelAI JSON 图片 Base64 编码无效") from exc
        cls._validate_image(image_bytes, "JSON 生成图片")
        image_format = body.get("image_format")
        if not isinstance(image_format, str) or image_format not in {"png", "jpeg", "jpg", "webp"}:
            raise NonRetryableError("NovelAI JSON image_format 须为 png/jpeg/jpg/webp")
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format.lower() != ("jpeg" if image_format == "jpg" else image_format):
                raise NonRetryableError("NovelAI JSON image_format 与图片内容不一致")
        logger.info("(NovelAI JSON) 已校验单张图片：job.status=%s format=%s bytes=%d", safe_status, image_format, len(image_bytes))
        return image_bytes

    @staticmethod
    def _validate_image(content: bytes, label: str) -> None:
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise NonRetryableError(f"NovelAI {label}为空或超过 25 MiB 大小限制")
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise NonRetryableError(f"NovelAI {label}仅支持 PNG/JPEG/WebP")
                if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_SOURCE_PIXELS:
                    raise NonRetryableError(f"NovelAI {label}像素数量无效或超过 16 Mi 像素限制")
                # PNG/WebP 插件具有帧数属性；JPEG 解码器本身只支持单帧。
                if image.format != "JPEG" and image.n_frames != 1:
                    raise NonRetryableError(f"NovelAI {label}须为静态单帧图片")
                image.verify()
            # JPEG 的 verify 不解码像素，再加载一次以拒绝截断或损坏的数据。
            with Image.open(BytesIO(content)) as image:
                image.load()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
            raise NonRetryableError(f"NovelAI {label}内容无效：{exc}") from exc

    @staticmethod
    def _error_message(content: bytes, token: str = "") -> str:
        """只呈现有限的上游错误描述，不记录或回显认证密钥。"""
        message = "服务端未提供可识别的错误描述"
        try:
            value = json.loads(content[:8192])
        except (ValueError, UnicodeDecodeError, RecursionError):
            value = None
        if isinstance(value, dict):
            detail = value.get("error", value.get("message"))
            if isinstance(detail, dict):
                detail = detail.get("message")
            if isinstance(detail, str) and detail.strip():
                message = detail.strip()
        if token:
            message = message.replace(token, "[已隐藏密钥]")
        return message[:500]
