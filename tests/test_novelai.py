"""原生 NovelAI 离线契约：请求、单图、错误边界及实际自拍提示词。"""

from collections import deque
from io import BytesIO
from PIL import Image
from types import SimpleNamespace
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

import asyncio
import base64
import json
import pytest
import re
import requests
import zipfile

from mais_art_journal.core.api_clients import get_client_class
from mais_art_journal.core.api_clients import novelai_client as nai
from mais_art_journal.core.api_clients.base_client import NonRetryableError
from mais_art_journal.core.api_clients.client_context import ClientContext, ProxyConfig
from mais_art_journal.core.config.novelai import NovelAIConfig
from mais_art_journal.core.pipeline.request import GenerationRequest
from mais_art_journal.core.pipeline.steps.build_selfie_prompt import BuildSelfiePrompt
from mais_art_journal.core.pipeline.steps.merge_negative_prompt import MergeNegativePrompt
from mais_art_journal.core.prompts import selfie_prompt_builder
from mais_art_journal.core.prompts.novelai_prompt import convert_novelai_weights


def image_bytes(size: Tuple[int, int] = (64, 64), image_format: str = "PNG") -> bytes:
    output = BytesIO()
    with Image.new("RGB", size, "navy") as image:
        image.save(output, format=image_format)
    return output.getvalue()


def zip_images(*images: bytes, extras: Optional[Dict[str, bytes]] = None) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, content in enumerate(images):
            archive.writestr(f"image_{index}.png", content)
        for name, content in (extras or {}).items():
            archive.writestr(name, content)
    return output.getvalue()


class Response:
    """只在内存提供响应流，记录是否提前停止和释放。"""

    def __init__(
        self, body: bytes, *, status: int = 200,
        content_type: str = "application/zip", headers: Optional[Dict[str, str]] = None,
        chunks: Optional[List[bytes]] = None,
    ) -> None:
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.chunks = chunks
        self.closed = False
        self.read_count = 0

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def iter_content(self, *, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        for chunk in self.chunks if self.chunks is not None else [self.body]:
            self.read_count += 1
            yield chunk


class Transport:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.results: Deque[Any] = deque()
        self.output_image = image_bytes()

    def post(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        result = self.results.popleft() if self.results else Response(zip_images(self.output_image))
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Transport:
    value = Transport()
    monkeypatch.setattr(nai.requests, "post", value.post)
    return value


@pytest.fixture
def client() -> nai.NovelAIClient:
    return nai.NovelAIClient(ClientContext(log_prefix="[NAI 离线契约]"))


def config(**changes: Any) -> Dict[str, Any]:
    return {
        "format": "novelai", "model": "nai-diffusion-4-5-full",
        "base_url": "https://offline.invalid", "api_key": "Bearer offline-secret",
        "sampler": "k_euler_ancestral", "noise_schedule": "karras",
        "num_inference_steps": 28, "guidance_scale": 5.0, "seed": 123,
        "cfg": 0.3, "custom_prompt_add": "soft lighting",
        "negative_prompt_add": "watermark", "artist": "artist:offline",
        "novelai": {"quality_tags": False}, **changes,
    }


def generate(client: nai.NovelAIClient, settings: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Tuple[bool, str]:
    return asyncio.run(client.generate_image(
        prompt="a quiet forest", model_config=settings if settings is not None else config(),
        size=kwargs.pop("size", "64x64"), **kwargs,
    ))


@pytest.mark.parametrize("model,mode", [
    ("nai-diffusion-3", "anime"), ("nai-diffusion-furry-3", "furry"),
    ("nai-diffusion-4-full", "anime"), ("nai-diffusion-4-curated-preview", "anime"),
    ("nai-diffusion-4-5-full", "anime"), ("nai-diffusion-4-5-curated", "anime"),
    ("nai-diffusion-5-full", "anime"), ("nai-diffusion-5-curated", "anime"),
])
@pytest.mark.parametrize("with_source", [False, True])
def test_supported_models_keep_single_image_contract(
    client: nai.NovelAIClient, transport: Transport, model: str, mode: str, with_source: bool,
) -> None:
    settings = config(model=model, novelai={"mode": mode, "quality_tags": False})
    source = base64.b64encode(image_bytes((64, 128))).decode("ascii") if with_source else None
    ok, result = generate(client, settings, input_image_base64=source)
    assert ok and base64.b64decode(result, validate=True) == transport.output_image
    assert get_client_class("novelai") is nai.NovelAIClient
    assert len(transport.calls) == 1
    request = transport.calls[0]
    assert request["url"] == "https://offline.invalid/ai/generate-image"
    assert request["headers"]["Authorization"] == "Bearer offline-secret"
    assert request["allow_redirects"] is False and request["stream"] is True
    body = request["json"]
    parameters = body["parameters"]
    assert body["model"] == model
    assert body["action"] == ("img2img" if with_source else "generate")
    assert parameters["n_samples"] == 1 and parameters["seed"] == 123
    assert parameters["steps"] == 28 and parameters["scale"] == 5 and parameters["cfg_rescale"] == 0.3
    assert parameters["negative_prompt"] == "watermark"
    assert body["input"] == "artist:offline, a quiet forest, soft lighting"
    assert "uc" not in parameters and "ucPreset" not in parameters and "qualityToggle" not in parameters
    if model in {"nai-diffusion-3", "nai-diffusion-furry-3"}:
        assert "v4_prompt" not in parameters
    else:
        assert parameters["v4_prompt"]["caption"]["base_caption"] == body["input"]
        assert parameters["v4_negative_prompt"]["caption"]["base_caption"] == "watermark"
        assert "sm" not in parameters and "sm_dyn" not in parameters
    if with_source:
        assert parameters["strength"] == 0.6 and parameters["noise"] == 0.1
        assert parameters["extra_noise_seed"] == 123 and parameters["add_original_image"] is False
        with Image.open(BytesIO(base64.b64decode(parameters["image"], validate=True))) as image:
            assert image.size == (64, 64) and image.format == "PNG" and image.mode == "RGB"
    else:
        assert "image" not in parameters and "strength" not in parameters


@pytest.mark.parametrize("token", ["offline-secret", "Bearer offline-secret", "bEaReR offline-secret", "Bearer Bearer offline-secret"])
def test_bearer_prefix_is_applied_once(client: nai.NovelAIClient, transport: Transport, token: str) -> None:
    assert generate(client, config(api_key=token))[0]
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer offline-secret"


@pytest.mark.parametrize("changes,expected", [
    ({"model": "nai-diffusion-5-unimplemented"}, "不支持模型"),
    ({"num_inference_steps": 51}, "steps"),
    ({"num_inference_steps": 28.5}, "整数"),
    ({"guidance_scale": 0.0}, "scale"),
    ({"guidance_scale": float("nan")}, "scale"),
    ({"cfg": 1.2}, "CFG Rescale"),
    ({"seed": 2**32}, "seed"),
    ({"seed": True}, "seed"),
    ({"sampler": "unknown"}, "采样器"),
    ({"model": "nai-diffusion-5-full", "noise_schedule": "native"}, "噪声调度"),
    ({"model": "nai-diffusion-4-full", "noise_schedule": "native"}, "噪声调度"),
    ({"novelai": {"mode": "background"}, "model": "nai-diffusion-4-full"}, "background"),
    ({"novelai": {"mode": "furry"}, "model": "nai-diffusion-3"}, "显式选择"),
    ({"novelai": {"smea": True}}, "SMEA"),
    ({"novelai": {"smea_dynamic": True}}, "SMEA"),
    ({"novelai": {"n_samples": 2}}, "n_samples"),
    ({"novelai": {"extra_parameters": {"n_samples": 20}}}, "extra_parameters"),
    ({"novelai": {"img2img_noise": 1.1}}, "img2img_noise"),
    ({"novelai": {"request_timeout_seconds": 200}}, "request_timeout_seconds"),
    ({"api_key": "Bearer "}, "密钥"),
    ({"api_key": "secret\r\nHeader:value"}, "密钥"),
    ({"base_url": "file:///offline"}, "base_url"),
    ({"base_url": "https://secret@offline.invalid"}, "base_url"),
    ({"base_url": "https://offline.invalid?key=secret"}, "base_url"),
])
def test_invalid_request_is_rejected_before_http(
    client: nai.NovelAIClient, transport: Transport, changes: Dict[str, Any], expected: str,
) -> None:
    ok, error = generate(client, config(**changes), max_retries=2)
    assert not ok and expected in error
    assert transport.calls == []


@pytest.mark.parametrize("size", ["16:9", "2K", "63x64", "1025x1024", "4096x4096", "-64x64", ""])
def test_invalid_size_does_not_fall_back(client: nai.NovelAIClient, transport: Transport, size: str) -> None:
    ok, error = generate(client, size=size)
    assert not ok and "NovelAI" in error and transport.calls == []


@pytest.mark.parametrize("source", ["", "not base64!", "data:image/png,AAAA", "data:application/json;base64,e30=", "bm90IGFuIGltYWdl"])
def test_invalid_source_never_becomes_text_to_image(client: nai.NovelAIClient, transport: Transport, source: str) -> None:
    ok, error = generate(client, input_image_base64=source)
    assert not ok and "参考图" in error and transport.calls == []


def test_source_data_uri_strength_and_noise_are_used(client: nai.NovelAIClient, transport: Transport) -> None:
    source = "data:image/jpeg;base64," + base64.b64encode(image_bytes((128, 64), "JPEG")).decode("ascii")
    settings = config(novelai={"img2img_strength": 0.8, "img2img_noise": 0.25, "quality_tags": False})
    assert generate(client, settings, input_image_base64=source, strength=0.0)[0]
    parameters = transport.calls[0]["json"]["parameters"]
    assert parameters["strength"] == 0.0 and parameters["noise"] == 0.25


def test_smea_is_v3_text_only(client: nai.NovelAIClient, transport: Transport) -> None:
    settings = config(model="nai-diffusion-3", noise_schedule="native", novelai={"smea": True, "smea_dynamic": True})
    assert generate(client, settings)[0]
    parameters = transport.calls[0]["json"]["parameters"]
    assert parameters["sm"] is True and parameters["sm_dyn"] is True
    assert "prefer_brownian" not in parameters
    ok, error = generate(client, settings, input_image_base64=base64.b64encode(image_bytes()).decode("ascii"))
    assert not ok and "SMEA" in error and len(transport.calls) == 1


@pytest.mark.parametrize("model,mode,tag", [
    ("nai-diffusion-4-full", "furry", "fur dataset"),
    ("nai-diffusion-4-5-full", "background", "background dataset"),
    ("nai-diffusion-5-full", "background", "background dataset"),
])
def test_dataset_modes_keep_the_configured_model(
    client: nai.NovelAIClient, transport: Transport, model: str, mode: str, tag: str,
) -> None:
    assert generate(client, config(model=model, novelai={"mode": mode, "quality_tags": False}))[0]
    body = transport.calls[0]["json"]
    assert body["model"] == model and body["input"].startswith(tag + ", ")


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_http_failure_does_not_retry_or_change_size(
    client: nai.NovelAIClient, transport: Transport, status: int,
) -> None:
    response = Response(json.dumps({"error": {"message": "拒绝 offline-secret"}}).encode(), status=status, content_type="application/json")
    transport.results.append(response)
    ok, error = generate(client, input_image_base64=base64.b64encode(image_bytes((128, 64))).decode("ascii"))
    assert not ok and f"HTTP {status}" in error
    assert "offline-secret" not in error and "已隐藏密钥" in error
    assert len(transport.calls) == 1 and response.closed
    assert transport.calls[0]["json"]["parameters"]["width"] == 64


def test_json_error_at_http_200_is_not_an_image(client: nai.NovelAIClient, transport: Transport) -> None:
    transport.results.append(Response(b'{"message":"offline-secret denied"}', content_type="application/json"))
    ok, error = generate(client)
    assert not ok and "JSON" in error and "offline-secret" not in error
    assert len(transport.calls) == 1


def test_config_error_does_not_echo_unknown_values(client: nai.NovelAIClient, transport: Transport) -> None:
    ok, error = generate(client, config(novelai={"unknown_option": "private-test-value"}))
    assert not ok and "unknown_option" in error and "private-test-value" not in error
    assert transport.calls == []


@pytest.mark.parametrize("failure", [requests.ReadTimeout("offline"), requests.ConnectionError("offline")])
def test_unknown_network_result_is_not_retried(client: nai.NovelAIClient, transport: Transport, failure: Exception) -> None:
    transport.results.append(failure)
    ok, error = generate(client)
    assert not ok and "不自动重试" in error and len(transport.calls) == 1


def test_only_connect_timeout_uses_existing_retry_budget(client: nai.NovelAIClient, transport: Transport) -> None:
    transport.results.append(requests.ConnectTimeout("offline"))
    assert generate(client, max_retries=1)[0]
    assert len(transport.calls) == 2
    assert transport.calls[0]["json"] == transport.calls[1]["json"]


@pytest.mark.parametrize("headers", [{"Content-Length": "-1"}, {"Content-Length": "bad"}, {"Content-Length": str(nai.MAX_RESPONSE_BYTES + 1)}])
def test_bad_content_length_is_rejected_without_reading(
    client: nai.NovelAIClient, transport: Transport, headers: Dict[str, str],
) -> None:
    response = Response(b"unused", headers=headers)
    transport.results.append(response)
    assert not generate(client)[0]
    assert response.closed and response.read_count == 0 and len(transport.calls) == 1


def test_streaming_limit_stops_reading_and_closes_response(
    client: nai.NovelAIClient, transport: Transport, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nai, "MAX_RESPONSE_BYTES", 128)
    response = Response(b"", chunks=[b"a" * 100, b"b" * 100, b"c" * 100])
    transport.results.append(response)
    ok, error = generate(client)
    assert not ok and "大小限制" in error
    assert response.read_count == 2 and response.closed and len(transport.calls) == 1


@pytest.mark.parametrize("content,expected", [
    (zip_images(), "找到 0 张"),
    (zip_images(image_bytes(), image_bytes()), "找到 2 张"),
    (zip_images(b"not a PNG"), "内容无效"),
    (b"", "空响应"),
    (b"PK broken zip", "不是原生 ZIP"),
])
def test_output_must_be_exactly_one_valid_image(
    client: nai.NovelAIClient, transport: Transport, content: bytes, expected: str,
) -> None:
    transport.results.append(Response(content))
    ok, error = generate(client)
    assert not ok and expected in error and len(transport.calls) == 1


def test_zip_checks_uncompressed_size_and_entry_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nai, "MAX_IMAGE_BYTES", 1024)
    with pytest.raises(NonRetryableError, match="ZIP 图片超过"):
        nai.NovelAIClient._extract_image(zip_images(b"x" * 2048), "application/zip")
    with pytest.raises(NonRetryableError, match="文件数量"):
        nai.NovelAIClient._extract_image(zip_images(image_bytes(), extras={f"{index}.txt": b"x" for index in range(16)}), "application/zip")
    monkeypatch.setattr(nai, "MAX_RESPONSE_BYTES", 512)
    with pytest.raises(NonRetryableError, match="解压总大小"):
        nai.NovelAIClient._extract_image(zip_images(image_bytes(), extras={"large.txt": b"x" * 512}), "application/zip")


def test_damaged_zip_crc_is_not_retried(client: nai.NovelAIClient, transport: Transport) -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("image.png", image_bytes())
        info = archive.getinfo("image.png")
        payload_offset = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
    corrupted = bytearray(output.getvalue())
    corrupted[payload_offset] ^= 1
    transport.results.append(Response(bytes(corrupted)))
    ok, error = generate(client)
    assert not ok and "ZIP 解析失败" in error and len(transport.calls) == 1


@pytest.mark.parametrize("image_format,content_type", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_direct_image_response_is_verified(
    client: nai.NovelAIClient, transport: Transport, image_format: str, content_type: str,
) -> None:
    data = image_bytes(image_format=image_format)
    transport.results.append(Response(data, content_type=content_type))
    ok, encoded = generate(client)
    assert ok and base64.b64decode(encoded, validate=True) == data
    transport.results.append(Response(data[:30], content_type=content_type))
    assert not generate(client)[0]
    assert len(transport.calls) == 2


def test_animated_source_and_oversized_pixels_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    output = BytesIO()
    with Image.new("RGB", (64, 64), "red") as first, Image.new("RGB", (64, 64), "blue") as second:
        first.save(output, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(NonRetryableError, match="静态单帧"):
        nai.NovelAIClient._prepare_source(base64.b64encode(output.getvalue()).decode("ascii"), 64, 64)
    monkeypatch.setattr(nai, "MAX_SOURCE_PIXELS", 100)
    with pytest.raises(NonRetryableError, match="像素数量"):
        nai.NovelAIClient._validate_image(image_bytes(), "参考图")


def test_proxy_and_timeout_are_forwarded(transport: Transport) -> None:
    proxied = nai.NovelAIClient(ClientContext(proxy=ProxyConfig(True, "http://127.0.0.1:9999")))
    assert generate(proxied, config(novelai={"request_timeout_seconds": 45}))[0]
    assert transport.calls[0]["proxies"] == {"http": "http://127.0.0.1:9999", "https": "http://127.0.0.1:9999"}
    assert transport.calls[0]["timeout"] == (10, 45)


def test_quality_tags_preserve_native_prompt_structure(client: nai.NovelAIClient) -> None:
    settings = config(model="nai-diffusion-3", artist="", custom_prompt_add="", novelai={"quality_tags": True})
    payload = client._build_payload("forest:0.7|ocean:0.3", settings, "64x64", NovelAIConfig())
    assert payload["input"].split("|")[0].endswith(":0.7") and payload["input"].endswith(":0.3")
    assert payload["input"].count("best quality") == 2
    v4 = config(artist="", custom_prompt_add="")
    payload = client._build_payload('sign\nText: "Welcome"', v4, "64x64", NovelAIConfig())
    assert payload["input"].endswith('no text\nText: "Welcome"')


def test_native_weights_are_preserved_and_sd_weights_are_converted() -> None:
    assert convert_novelai_weights("{{silver hair}}, [hat]", v3=True) == "{{silver hair}}, [hat]"
    assert convert_novelai_weights("1.4::silver hair::, -0.8::feet::", v3=False) == "1.4::silver hair::, -0.8::feet::"
    assert convert_novelai_weights("(silver hair:1.5), (hat:0.5)", v3=False) == "1.5::silver hair::, 0.5::hat::"
    assert convert_novelai_weights("(silver hair:1.05), (hat:0.9523809524)", v3=True) == "{silver hair}, [hat]"
    with pytest.raises(ValueError, match="零或负数"):
        convert_novelai_weights("(hat:0)", v3=True)


@pytest.mark.parametrize("model", ["nai-diffusion-3", "nai-diffusion-4-5-full"])
def test_real_selfie_prompt_and_uc_reach_nai_with_native_weights(
    client: nai.NovelAIClient, transport: Transport, model: str,
) -> None:
    assert selfie_prompt_builder.uses_weighted_tags("novelai")
    assert selfie_prompt_builder.supports_reference_strength("novelai")
    selfie = asyncio.run(selfie_prompt_builder.build(
        ctx=SimpleNamespace(), selfie_style="photo", description="reading under a tree",
        free_hand_action="holding a book", bot_appearance="silver hair, blue eyes",
        base_negative="watermark", run_scene_optimizer=False, api_format="novelai",
    ))
    settings = config(model=model, negative_prompt_add=selfie.negative_prompt)
    ok, _ = asyncio.run(client.generate_image(prompt=selfie.prompt, model_config=settings, size="64x64"))
    assert ok and len(transport.calls) == 1
    body = transport.calls[0]["json"]
    assert "silver hair" in body["input"] and "reading under a tree" in body["input"]
    assert body["input"].count("soft lighting") == 1 and body["input"].count("artist:offline") == 1
    assert not re.search(r"\([^()]+:\d+(?:\.\d+)?\)", body["input"])
    assert not re.search(r"\([^()]+:\d+(?:\.\d+)?\)", body["parameters"]["negative_prompt"])
    assert "Create a photograph" not in body["input"]
    assert "watermark" in body["parameters"]["negative_prompt"]
    assert ("{" if model == "nai-diffusion-3" else "::") in body["input"]


@pytest.mark.parametrize("explicit_strength,expected", [(None, 0.72), (0.2, 0.2)])
def test_selfie_pipeline_preserves_reference_strength_precedence(
    client: nai.NovelAIClient, transport: Transport, explicit_strength: Optional[float], expected: float,
) -> None:
    plugin = SimpleNamespace(
        ctx=SimpleNamespace(),
        config=SimpleNamespace(
            selfie=SimpleNamespace(enabled=True, activity_rules=[], prompt_prefix="silver hair", negative_prompt="watermark"),
            basic=SimpleNamespace(prompt_optimizer_enabled=False, llm_task_name="offline", llm_timeout_seconds=10),
        ),
    )
    request = GenerationRequest(
        description="reading under a tree", is_selfie=True, selfie_style="photo", activity_type="relaxing",
        free_hand_action="holding a book", api_format="novelai", final_size="64x64",
        input_image_base64=base64.b64encode(image_bytes()).decode("ascii"), strength=explicit_strength,
        resolved_model_config=config(selfie_reference_strength=0.72, novelai={"img2img_strength": 0.9}),
    )

    async def run() -> Tuple[bool, str]:
        context = SimpleNamespace(plugin=plugin)
        assert await BuildSelfiePrompt().run(request, context) is None
        assert await MergeNegativePrompt().run(request, context) is None
        return await client.generate_image(
            prompt=request.description, model_config=request.resolved_model_config, size=request.final_size,
            input_image_base64=request.input_image_base64, strength=request.strength,
        )

    assert asyncio.run(run())[0]
    assert request.strength == expected
    parameters = transport.calls[0]["json"]["parameters"]
    assert parameters["strength"] == expected and "watermark" in parameters["negative_prompt"]
    assert len(transport.calls) == 1
