"""Ark 原生与显式 multipart Images 离线测试，不读实际配置或发送真实 HTTP。"""

from email.parser import BytesParser
from importlib import import_module
from io import BytesIO
from pathlib import Path
from types import ModuleType

import base64
import json
import sys

from PIL import Image
import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_mais_art_ark_protocol_tests"
for suffix, path in (
    ("", ROOT),
    (".core", ROOT / "core"),
    (".core.config", ROOT / "core/config"),
    (".core.api_clients", ROOT / "core/api_clients"),
):
    module = ModuleType(PACKAGE + suffix)
    module.__path__ = [str(path)]
    sys.modules[module.__name__] = module

models = import_module(PACKAGE + ".core.config.models")
protocol = import_module(PACKAGE + ".core.api_clients.images_protocol")


def 图片(color):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


PNG_A = 图片("red")
PNG_B = 图片("blue")
REF_A = protocol.构造参考图(PNG_A)
REF_B = protocol.构造参考图(PNG_B)
WIRES = ("doubao-seedream-5-0-260128", "doubao-seedream-5-0-pro-260628")


@pytest.fixture
def model():
    return models.ModelConfig(
        id="offline-ark", model=WIRES[0], format="doubao",
        base_url="https://ark-api.invalid/api/v3", api_key="offline-fake-key",
        image_input_mode="json_image",
    )


def 假客户端(handler, calls):
    def route(request):
        calls.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(route))


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("references", [[], [REF_A], [REF_B, REF_A, REF_B]])
async def test_ark_native_json_contract_one_post(model, wire, count, references):
    model.model = wire
    payload = protocol.构造生成请求(model, "本次明确描述", count, references, "")
    expected = {
        "model": wire, "prompt": "本次明确描述",
        "sequential_image_generation": "disabled" if count == 1 else "auto",
    }
    if count > 1:
        expected["sequential_image_generation_options"] = {"max_images": count}
    if references:
        expected["image"] = references[0] if len(references) == 1 else references
    calls = []

    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://ark-api.invalid/api/v3/images/generations"
        assert request.headers["Authorization"] == "Bearer offline-fake-key"
        assert json.loads(request.content) == expected
        return httpx.Response(200, json={"data": [{"b64_json": PNG_A}]})

    async with 假客户端(handler, calls) as client:
        outputs = await protocol.提交一次(client, model, payload)
    # max_images 不保证返回数量；只交付真实返回项，不再次请求凑数。
    assert outputs == [protocol.原生图片(0, b64_json=PNG_A)]
    assert len(calls) == 1 and "n" not in payload


@pytest.mark.parametrize("references", [[REF_A], [REF_B, REF_A]])
@pytest.mark.parametrize("base_url", ["https://ark.cn-beijing.volces.com/api/v3", "https://openai.invalid/v1"])
def test_openai_contract_is_unchanged_even_for_ark_host_or_model(model, references, base_url):
    model.format = "openai"
    model.base_url = base_url
    assert protocol.构造生成请求(model, "原描述", 3, references, "") == {
        "model": WIRES[0], "prompt": "原描述", "n": 3, "image": references,
    }


@pytest.mark.parametrize("api_format", ["doubao", "openai"])
def test_reference_input_requires_explicit_json_image(model, api_format):
    model.format = api_format
    model.image_input_mode = "disabled"
    with pytest.raises(ValueError, match="未配置 JSON image"):
        protocol.构造生成请求(model, "不丢弃参考图", 1, [REF_A], "")
    assert "image" not in protocol.构造生成请求(model, "纯文生图", 1, [], "")


@pytest.mark.parametrize("api_format", ["doubao", "openai"])
@pytest.mark.parametrize("fixed_size,size,expected", [
    (False, "", None),
    (False, "requested-canvas", "requested-canvas"),
    (True, "requested-canvas", "configured-canvas"),
])
def test_size_is_only_explicit_configuration_without_model_guesses(model, api_format, fixed_size, size, expected):
    model.format = api_format
    model.fixed_size_enabled = fixed_size
    model.default_size = "configured-canvas"
    payload = protocol.构造生成请求(model, "原描述", 1, [], size)
    assert payload.get("size") == expected
    assert ("size" in payload) is (expected is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", [
    "https://ark-api.invalid/api/v3/",
    "https://ark-api.invalid/api/v3/images/generations",
    "https://ark-api.invalid/api/v3/images/generations/",
])
async def test_endpoint_and_bearer_normalization_are_preserved(model, base_url):
    model.base_url = base_url
    model.api_key = "  Bearer offline-fake-key  "
    calls = []

    def handler(request):
        assert request.url.path == "/api/v3/images/generations"
        assert request.headers["Authorization"] == "Bearer offline-fake-key"
        return httpx.Response(200, json={"data": [{"url": "https://cdn.invalid/image"}]})

    async with 假客户端(handler, calls) as client:
        assert await protocol.提交一次(client, model, {}) == [protocol.原生图片(0, url="https://cdn.invalid/image")]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_entire_response_preserves_original_positions_without_truncation(model):
    calls = []
    body = {"error": None, "data": [
        {"url": "https://cdn.invalid/first"}, 42,
        {"b64_json": PNG_A, "url": "https://cdn.invalid/not-downloaded"}, {},
        {"b64_json": PNG_B},
    ]}
    async with 假客户端(lambda request: httpx.Response(200, json=body), calls) as client:
        outputs = await protocol.提交一次(client, model, protocol.构造生成请求(model, "全集", 1, [], ""))
    assert [output.index for output in outputs] == list(range(5))
    assert outputs[0].url == "https://cdn.invalid/first"
    assert outputs[1].error == "返回项不是图像对象"
    assert outputs[2] == protocol.原生图片(2, b64_json=PNG_A)
    assert outputs[3].error == "返回项缺少 b64_json 或 url"
    assert outputs[4] == protocol.原生图片(4, b64_json=PNG_B)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], [{"b64_json": PNG_A}]])
async def test_top_level_business_error_wins_over_data_and_is_safe(model, data):
    calls = []
    sensitive = "Authorization: Bearer offline-secret; prompt=private-request"
    body = {
        "error": {"code": "InvalidParameter.ContentRisk", "message": sensitive},
        "request_id": "offline-request_123:abc", "data": data,
    }
    async with 假客户端(lambda request: httpx.Response(200, json=body), calls) as client:
        with pytest.raises(ValueError, match="业务错误") as caught:
            await protocol.提交一次(client, model, {})
    description = protocol.描述错误(caught.value)
    assert "code=InvalidParameter.ContentRisk" in description
    assert "request_id=offline-request_123:abc" in description
    assert sensitive not in description and "offline-secret" not in description
    assert "private-request" not in description and len(calls) == 1


@pytest.mark.parametrize("error", [{}, [], False, 0, "", "Authorization: Bearer offline-secret"])
def test_non_null_top_level_error_is_not_hidden_by_valid_data(error):
    with pytest.raises(ValueError, match="业务错误") as caught:
        protocol.解析输出({"error": error, "data": [{"b64_json": PNG_A}]})
    assert "offline-secret" not in protocol.描述错误(caught.value)


@pytest.mark.parametrize("unsafe", [
    "Authorization: Bearer offline-secret", "private-request\nrequest", "中文原请求", "x" * 129,
    {"secret": "offline-secret"}, ["offline-secret"], 123,
])
def test_business_error_identifiers_reject_freeform_or_non_string_values(unsafe):
    with pytest.raises(ValueError) as caught:
        protocol.解析输出({"error": {"code": unsafe}, "request_id": unsafe, "data": [{"b64_json": PNG_A}]})
    assert protocol.描述错误(caught.value) == "图像服务返回业务错误"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 400, 401, 500])
async def test_http_failure_is_not_retried_redirected_or_echoed(model, status):
    calls = []
    response = httpx.Response(status, headers={"Location": "https://other.invalid"}, json={
        "error": {"message": "Authorization: Bearer offline-secret"},
        "data": [{"b64_json": PNG_A}],
    })
    async with 假客户端(lambda request: response, calls) as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await protocol.提交一次(client, model, {})
    assert protocol.描述错误(caught.value) == f"图像服务返回 HTTP {status}"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], "private-request"])
async def test_json_response_must_be_object_without_echo(model, body):
    calls = []
    response = httpx.Response(200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    async with 假客户端(lambda request: response, calls) as client:
        with pytest.raises(ValueError, match="响应不是 JSON 对象"):
            await protocol.提交一次(client, model, {})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_json_is_not_retried_or_echoed(model):
    calls = []
    async with 假客户端(lambda request: httpx.Response(200, content=b"{private-request"), calls) as client:
        with pytest.raises(json.JSONDecodeError) as caught:
            await protocol.提交一次(client, model, {})
    assert "private-request" not in protocol.描述错误(caught.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"data": []}, {"data": "invalid"}, {"images": [PNG_A]}])
async def test_unknown_output_collection_fails_without_retry(model, body):
    calls = []
    async with 假客户端(lambda request: httpx.Response(200, json=body), calls) as client:
        with pytest.raises(ValueError, match="非空 data 集合"):
            await protocol.提交一次(client, model, {})
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.LocalProtocolError])
async def test_request_error_keeps_s1_secret_redaction_without_retry(model, error_type):
    calls = []

    def handler(request):
        raise error_type("Illegal header value b'Bearer offline-secret'; private-request", request=request)

    async with 假客户端(handler, calls) as client:
        with pytest.raises(error_type) as caught:
            await protocol.提交一次(client, model, {})
    description = protocol.描述错误(caught.value)
    assert error_type.__name__ in description
    assert "offline-secret" not in description and "private-request" not in description
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_downloads_keep_all_valid_outputs_and_do_not_forward_authorization(model):
    calls = []

    def handler(request):
        if request.method == "POST":
            assert "Authorization" in request.headers
            return httpx.Response(200, json={"data": [{"b64_json": PNG_A}, {"url": "https://cdn.invalid/start"}]})
        assert "Authorization" not in request.headers
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://other-cdn.invalid/final"})
        return httpx.Response(200, content=base64.b64decode(PNG_B))

    async with 假客户端(handler, calls) as client:
        outputs = await protocol.提交一次(client, model, protocol.构造生成请求(model, "两项", 3, [], ""))
        images = [await protocol.获取图片(client, output) for output in outputs]
    assert images == [PNG_A, PNG_B]
    assert [request.method for request in calls] == ["POST", "GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    protocol.原生图片(0, error="返回项缺少 b64_json 或 url"),
    protocol.原生图片(0, b64_json="broken"),
    protocol.原生图片(0, b64_json=base64.b64encode(b"not an image").decode()),
    protocol.原生图片(0, url="file:///not-an-image"),
])
async def test_invalid_output_is_not_downloaded_or_silently_replaced(output):
    calls = []
    async with 假客户端(lambda request: pytest.fail("invalid output attempted HTTP"), calls) as client:
        with pytest.raises((ValueError, OSError)):
            await protocol.获取图片(client, output)
    assert calls == []


def test_namespace_does_not_import_legacy_core_or_other_tests():
    assert PACKAGE + ".core.generation" not in sys.modules
    assert PACKAGE + ".core.state" not in sys.modules
    assert PACKAGE + ".core.config.manager" not in sys.modules
    assert protocol.__package__ == PACKAGE + ".core.api_clients"
    assert not hasattr(sys.modules[PACKAGE + ".core"], "__file__")
    assert protocol.构造参考图(PNG_A) == f"data:image/png;base64,{PNG_A}"
    assert protocol.构造参考图(PNG_B) == f"data:image/png;base64,{PNG_B}"
    with pytest.raises(ValueError):
        protocol.构造参考图("broken")
    with pytest.raises(OSError):
        protocol.构造参考图(base64.b64encode(b"not an image").decode())
    with pytest.raises(ValueError, match="图片数据为空"):
        protocol.构造参考图("")
    assert list(protocol.原生图片.__dataclass_fields__) == ["index", "b64_json", "url", "error"]
    assert PACKAGE != "_mais_art_s1_tests"


@pytest.fixture
def multipart_model():
    return models.ModelConfig(
        id="offline-multipart", model="gpt-image-2.5", format="openai",
        base_url="https://edit-api.invalid/v1", api_key="offline-fake-key",
        image_input_mode="multipart_edit",
    )


def 解析表单(request):
    message = BytesParser().parsebytes(
        b"Content-Type: " + request.headers["Content-Type"].encode("ascii") + b"\r\n\r\n" + request.content
    )
    fields = {}
    images = []
    for part in message.get_payload():
        name = part.get_param("name", header="content-disposition")
        if part.get_filename() is None:
            fields[name] = part.get_payload(decode=True).decode("utf-8")
        else:
            images.append((name, part.get_content_type(), part.get_payload(decode=True)))
    return fields, images


@pytest.mark.asyncio
@pytest.mark.parametrize("references", [[REF_A], [REF_B, REF_A, REF_B]])
@pytest.mark.parametrize("base_url", [
    "https://edit-api.invalid/v1/",
    "https://edit-api.invalid/v1/images/generations",
    "https://edit-api.invalid/v1/images/edits/",
])
@pytest.mark.parametrize("count", [1, 3])
async def test_multipart_edit_uses_one_ordered_file_submission(multipart_model, references, base_url, count):
    multipart_model.base_url = base_url
    payload = protocol.构造生成请求(multipart_model, "保留人设，改成下雨天", count, references, "requested-canvas")
    assert payload["image"] == references
    calls = []

    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://edit-api.invalid/v1/images/edits"
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert request.headers["Authorization"] == "Bearer offline-fake-key"
        fields, images = 解析表单(request)
        assert fields == {
            "model": "gpt-image-2.5", "prompt": "保留人设，改成下雨天", "n": str(count), "size": "requested-canvas",
        }
        field = "image" if len(references) == 1 else "image[]"
        assert images == [
            (field, "image/png", base64.b64decode(reference.split(",", 1)[1])) for reference in references
        ]
        return httpx.Response(200, json={"data": [{"b64_json": PNG_A}]})

    async with 假客户端(handler, calls) as client:
        assert await protocol.提交一次(client, multipart_model, payload) == [protocol.原生图片(0, b64_json=PNG_A)]
    assert len(calls) == 1 and payload["image"] == references


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["https://edit-api.invalid/v1", "https://edit-api.invalid/v1/images/generations/"])
async def test_multipart_mode_text_generation_still_uses_json_generations(multipart_model, base_url):
    multipart_model.base_url = base_url
    payload = protocol.构造生成请求(multipart_model, "只文生图", 1, [], "")
    calls = []

    def handler(request):
        assert request.url.path == "/v1/images/generations"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {"model": "gpt-image-2.5", "prompt": "只文生图", "n": 1}
        return httpx.Response(200, json={"data": [{"b64_json": PNG_A}]})

    async with 假客户端(handler, calls) as client:
        await protocol.提交一次(client, multipart_model, payload)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_openai_json_image_edit_never_switches_to_multipart(multipart_model):
    multipart_model.image_input_mode = "json_image"
    payload = protocol.构造生成请求(multipart_model, "显式 JSON 扩展", 2, [REF_B, REF_A], "")
    calls = []

    def handler(request):
        assert request.url.path == "/v1/images/generations"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {
            "model": "gpt-image-2.5", "prompt": "显式 JSON 扩展", "n": 2, "image": [REF_B, REF_A],
        }
        return httpx.Response(200, json={"data": [{"b64_json": PNG_A}]})

    async with 假客户端(handler, calls) as client:
        await protocol.提交一次(client, multipart_model, payload)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,references", [
    ("multipart_edit", []), ("json_image", []), ("disabled", []), ("json_image", [REF_A]),
])
async def test_edits_base_rejects_every_non_multipart_operation_before_post(multipart_model, mode, references):
    multipart_model.base_url = "https://edit-api.invalid/v1/images/edits"
    multipart_model.image_input_mode = mode
    payload = protocol.构造生成请求(multipart_model, "不改变协议", 1, references, "")
    calls = []
    async with 假客户端(lambda request: pytest.fail("incompatible endpoint submitted"), calls) as client:
        with pytest.raises(ValueError, match="images/edits"):
            await protocol.提交一次(client, multipart_model, payload)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("references", [
    [], None, REF_A,
    [REF_A, "https://private-request.invalid/image.png"],
    [REF_A, "data:image/png;base64,offline-secret!"],
    [REF_A, "data:text/plain;base64,cHJpdmF0ZS1yZXF1ZXN0"],
    [REF_A, "data:image/png;base64,"],
    [REF_A, "data:image/png;base64,AAAA\n"],
    [REF_A, 123],
])
async def test_invalid_multipart_reference_fails_before_any_post_without_echo(multipart_model, references):
    calls = []
    payload = {"model": multipart_model.model, "prompt": "private-request", "n": 1, "image": references}
    async with 假客户端(lambda request: pytest.fail("invalid reference submitted"), calls) as client:
        with pytest.raises(ValueError) as caught:
            await protocol.提交一次(client, multipart_model, payload)
    description = protocol.描述错误(caught.value)
    assert "private-request" not in description and "offline-secret" not in description
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "protocol", "http", "business", "json"])
async def test_multipart_errors_keep_safe_single_submission(multipart_model, failure):
    calls = []

    def handler(request):
        assert request.url.path == "/v1/images/edits"
        if failure == "timeout":
            raise httpx.ReadTimeout("private-request offline-secret", request=request)
        if failure == "protocol":
            raise httpx.LocalProtocolError("Bearer offline-secret", request=request)
        if failure == "http":
            return httpx.Response(500, text="private-request offline-secret")
        if failure == "business":
            return httpx.Response(200, json={
                "error": {"code": "ImageEditFailed", "message": "private-request offline-secret"},
                "data": [{"b64_json": PNG_A}],
            })
        return httpx.Response(200, content=b"{private-request offline-secret")

    payload = protocol.构造生成请求(multipart_model, "private-request", 1, [REF_A], "")
    async with 假客户端(handler, calls) as client:
        with pytest.raises((ValueError, httpx.HTTPError)) as caught:
            await protocol.提交一次(client, multipart_model, payload)
    description = protocol.描述错误(caught.value)
    assert "private-request" not in description and "offline-secret" not in description
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_multipart_result_download_does_not_receive_generation_authorization(multipart_model):
    calls = []

    def handler(request):
        if request.method == "POST":
            assert request.url.path == "/v1/images/edits"
            return httpx.Response(200, json={"data": [{"url": "https://cdn.invalid/final"}]})
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=base64.b64decode(PNG_B))

    payload = protocol.构造生成请求(multipart_model, "有图编辑", 1, [REF_A], "")
    async with 假客户端(handler, calls) as client:
        outputs = await protocol.提交一次(client, multipart_model, payload)
        assert await protocol.获取图片(client, outputs[0]) == PNG_B
    assert [request.method for request in calls] == ["POST", "GET"]
