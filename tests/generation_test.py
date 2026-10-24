"""S1 离线验收：假 SDK 和 httpx MockTransport，不读实际配置或发送消息。"""

from importlib import import_module
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import base64
import json
import sys

from PIL import Image
import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_mais_art_s1_tests"
for suffix, path in (("", ROOT), (".core", ROOT / "core"),
                     (".core.config", ROOT / "core/config"), (".core.api_clients", ROOT / "core/api_clients")):
    module = ModuleType(PACKAGE + suffix)
    module.__path__ = [str(path)]
    sys.modules[module.__name__] = module

state = ModuleType(PACKAGE + ".core.state")
state.runtime_state = SimpleNamespace(
    is_plugin_enabled=lambda stream, enabled: enabled,
    is_model_enabled=lambda stream, model_id: True,
)
sys.modules[state.__name__] = state
config_module = import_module(PACKAGE + ".core.config.models")
protocol = import_module(PACKAGE + ".core.api_clients.images_protocol")
generation = import_module(PACKAGE + ".core.generation")
entry = import_module(PACKAGE + ".plugin")
REAL_CLIENT = httpx.AsyncClient


def 图片(color):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


PNG_A = 图片("red")
PNG_B = 图片("blue")
REFS = [{"source_id": "visible-a", "index": 0}, {"source_id": "visible-b", "index": 1}]


@pytest.fixture
def plugin():
    config = config_module.MaisArtConfig()
    config.plugin.enabled = True
    config.basic.default_generation_model = "default"
    config.basic.default_txt2img_model = "legacy-other"
    config.basic.max_output_images = 6
    config.models.items = [config_module.ModelConfig(
        id="default", model="wire-model", base_url="https://image-api.invalid/v1", api_key="offline-key",
        image_input_mode="json_image", max_reference_images=3, max_output_images=6,
    ), config_module.ModelConfig(
        id="explicit", model="wire-explicit", base_url="https://another.invalid/v1", api_key="offline-key",
        max_output_images=6,
    )]
    resolve = AsyncMock(side_effect=[
        {"success": True, "image": {"binary_data_base64": PNG_A}},
        {"success": True, "image": {"binary_data_base64": PNG_B}},
    ])
    return SimpleNamespace(config=config, ctx=SimpleNamespace(
        maisaka=SimpleNamespace(context=SimpleNamespace(resolve_image=resolve)),
        send=SimpleNamespace(image=AsyncMock(return_value=True)),
    ))


def 装载HTTP(monkeypatch, handler):
    calls = []

    async def route(request):
        calls.append(request)
        return handler(request)

    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(route), **kwargs)

    monkeypatch.setattr(generation.httpx, "AsyncClient", factory)
    return calls


def 请求(**kwargs):
    return {"description": "本次明确描述", "operation": "generate", "stream_id": "offline-existing-stream", **kwargs}


@pytest.mark.asyncio
async def test_default_single_generation_no_legacy_style(plugin, monkeypatch):
    calls = 装载HTTP(monkeypatch, lambda req: httpx.Response(200, json={"data": [{"b64_json": PNG_A}]}))
    result = await generation.执行普通生成(plugin, 请求())
    body = json.loads(calls[0].content)
    assert body == {"model": "wire-model", "prompt": "本次明确描述", "n": 1}
    assert len(calls) == 1 and calls[0].method == "POST"
    assert result["available_count"] == result["sent_count"] == 1
    assert plugin.ctx.maisaka.context.resolve_image.await_count == 0
    assert plugin.ctx.send.image.await_args.kwargs["sync_to_maisaka_history"] is True


@pytest.mark.asyncio
async def test_reference_order_and_one_native_submission(plugin, monkeypatch):
    calls = 装载HTTP(monkeypatch, lambda req: httpx.Response(200, json={"data": [{"b64_json": PNG_A}] * 3}))
    result = await generation.执行普通生成(plugin, 请求(operation="edit", references=REFS, count=3))
    body = json.loads(calls[0].content)
    assert body["image"] == [f"data:image/png;base64,{PNG_A}", f"data:image/png;base64,{PNG_B}"]
    assert body["n"] == 3 and len(calls) == 1
    assert [call.kwargs["index"] for call in plugin.ctx.maisaka.context.resolve_image.await_args_list] == [0, 1]
    assert result["returned_count"] == result["sent_count"] == 3


@pytest.mark.asyncio
async def test_explicit_model_overrides_global_only(plugin, monkeypatch):
    calls = 装载HTTP(monkeypatch, lambda req: httpx.Response(200, json={"data": [{"b64_json": PNG_A}]}))
    await generation.执行普通生成(plugin, 请求(model_id="explicit"))
    assert json.loads(calls[0].content)["model"] == "wire-explicit"


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"operation": "edit"}, {"operation": "generate", "references": REFS}, {"operation": "unknown"},
    {"count": 0}, {"count": True}, {"count": "3"}, {"count": 7},
    {"model_id": "missing"}, {"stream_id": ""}, {"description": ""},
    {"operation": "edit", "references": REFS * 2},
    {"operation": "edit", "references": [{"source_id": "", "index": 0}]},
    {"operation": "edit", "references": [{"source_id": "a", "index": -1}]},
    {"strength": 0.7},
])
async def test_invalid_input_never_submits(plugin, monkeypatch, overrides):
    calls = 装载HTTP(monkeypatch, lambda req: pytest.fail("invalid request submitted"))
    result = await generation.执行普通生成(plugin, 请求(**overrides))
    assert not result["success"] and result["message"]
    assert calls == [] and plugin.ctx.maisaka.context.resolve_image.await_count == 0


@pytest.mark.asyncio
async def test_global_and_model_caps_both_apply(plugin, monkeypatch):
    calls = 装载HTTP(monkeypatch, lambda req: pytest.fail("over-limit submitted"))
    plugin.config.basic.max_output_images = 2
    assert not (await generation.执行普通生成(plugin, 请求(count=3)))["success"]
    plugin.config.basic.max_output_images = 6
    plugin.config.models.items[0].max_output_images = 2
    assert not (await generation.执行普通生成(plugin, 请求(count=3)))["success"]
    assert calls == []


@pytest.mark.asyncio
async def test_missing_global_does_not_guess_legacy_model(plugin, monkeypatch):
    calls = 装载HTTP(monkeypatch, lambda req: pytest.fail("missing default submitted"))
    plugin.config.basic.default_generation_model = ""
    result = await generation.执行普通生成(plugin, 请求())
    assert not result["success"] and "全局默认" in result["message"] and not calls


@pytest.mark.asyncio
async def test_unreadable_reference_does_not_drop_or_search(plugin, monkeypatch):
    calls = 装载HTTP(monkeypatch, lambda req: pytest.fail("missing reference submitted"))
    plugin.ctx.maisaka.context.resolve_image.side_effect = [
        {"success": True, "image": {"binary_data_base64": PNG_A}}, {"success": False, "error": "not retained"},
    ]
    result = await generation.执行普通生成(plugin, 请求(operation="edit", references=REFS))
    assert not result["success"] and "不搜索历史" in result["message"]
    assert not calls and plugin.ctx.send.image.await_count == 0


@pytest.mark.asyncio
async def test_partial_acquire_and_send_keep_original_positions(plugin, monkeypatch):
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"data": [
                {"b64_json": PNG_A}, {"url": "https://cdn.invalid/bad"}, {"url": "https://cdn.invalid/good"},
            ]})
        assert "Authorization" not in req.headers
        return httpx.Response(404) if req.url.path == "/bad" else httpx.Response(200, content=base64.b64decode(PNG_B))

    calls = 装载HTTP(monkeypatch, handler)
    plugin.ctx.send.image.side_effect = [False, True]
    result = await generation.执行普通生成(plugin, 请求(count=3))
    assert result["success"] and result["available_count"] == 2 and result["sent_count"] == 1
    assert [(x["index"], x["stage"]) for x in result["failures"]] == [(0, "send"), (1, "acquire")]
    assert [r.method for r in calls] == ["POST", "GET", "GET"]
    assert plugin.ctx.send.image.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [{"success": False}, {"sent": False}, 1, "ok"])
async def test_truthy_non_boolean_receipt_is_not_sent(plugin, monkeypatch, receipt):
    calls = 装载HTTP(monkeypatch, lambda req: httpx.Response(200, json={"data": [{"b64_json": PNG_A}]}))
    plugin.ctx.send.image.return_value = receipt
    result = await generation.执行普通生成(plugin, 请求())
    assert result["sent_count"] == 0 and result["success"] is False
    assert result["failures"][0]["stage"] == "send"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"data": []}, {"images": ["not-a-known-contract"]}, {"data": "invalid"}])
async def test_unknown_response_never_regenerates(plugin, monkeypatch, response):
    calls = 装载HTTP(monkeypatch, lambda req: httpx.Response(200, json=response))
    result = await generation.执行普通生成(plugin, 请求())
    assert not result["success"] and len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_reports_unknown_without_retry(plugin, monkeypatch):
    def handler(req):
        raise httpx.ReadTimeout("offline timeout", request=req)

    calls = 装载HTTP(monkeypatch, handler)
    result = await generation.执行普通生成(plugin, 请求())
    assert not result["success"] and "结果未知" in result["message"] and len(calls) == 1


@pytest.mark.asyncio
async def test_bad_item_does_not_hide_later_output(plugin, monkeypatch):
    calls = 装载HTTP(monkeypatch, lambda req: httpx.Response(200, json={"data": [
        {"b64_json": "broken"}, 42, {"b64_json": PNG_B},
    ]}))
    result = await generation.执行普通生成(plugin, 请求(count=3))
    assert result["success"] and result["sent_count"] == 1
    assert [item["index"] for item in result["failures"]] == [0, 1] and len(calls) == 1


@pytest.mark.asyncio
async def test_tool_routes_ordinary_away_from_old_pipeline(plugin, monkeypatch):
    result = {"success": True, "message": "offline"}
    handler = AsyncMock(return_value=result)
    monkeypatch.setattr(generation, "执行普通生成", handler)
    assert await entry.MaisArtPlugin.handle_draw_picture(plugin, **请求()) == result
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_selfie_routes_to_persona_service_without_legacy_pipeline(monkeypatch):
    module = ModuleType(PACKAGE + ".core.selfie.service")
    module.执行手动自拍 = AsyncMock(return_value={"success": True, "message": "自拍完成"})
    monkeypatch.setitem(sys.modules, module.__name__, module)
    plugin = SimpleNamespace()
    result = await entry.MaisArtPlugin.handle_draw_picture(plugin, selfie_mode=True, stream_id="offline-existing")
    assert result == {"success": True, "message": "自拍完成"}
    module.执行手动自拍.assert_awaited_once()


@pytest.mark.asyncio
async def test_protocol_error_does_not_echo_authorization(plugin, monkeypatch):
    secret = "offline-secret-not-a-real-key"

    def handler(req):
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {secret}'", request=req)

    calls = 装载HTTP(monkeypatch, handler)
    result = await generation.执行普通生成(plugin, 请求())
    assert not result["success"] and secret not in json.dumps(result)
    assert "LocalProtocolError" in result["message"] and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 400, 500])
async def test_http_failure_never_reposts(plugin, monkeypatch, status):
    calls = 装载HTTP(monkeypatch, lambda req: httpx.Response(status, headers={"Location": "https://other.invalid"}))
    result = await generation.执行普通生成(plugin, 请求())
    assert not result["success"] and len(calls) == 1


@pytest.mark.asyncio
async def test_qwen_compatible_url_reference_contract(plugin, monkeypatch):
    plugin.config.models.items[0].model = "qwen-image-3.0-pro"

    def handler(req):
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["model"] == "qwen-image-3.0-pro" and body["n"] == 3
            assert len(body["image"]) == 2 and "response_format" not in body
            return httpx.Response(200, json={"data": [{"url": f"https://cdn.invalid/{i}"} for i in range(3)]})
        assert "Authorization" not in req.headers
        return httpx.Response(200, content=base64.b64decode(PNG_A))

    calls = 装载HTTP(monkeypatch, handler)
    result = await generation.执行普通生成(plugin, 请求(operation="edit", references=REFS, count=3))
    assert result["sent_count"] == 3 and [req.method for req in calls] == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_unsupported_edit_contract_rejects_without_loading_references(plugin, monkeypatch):
    plugin.config.models.items[0].image_input_mode = "disabled"
    calls = 装载HTTP(monkeypatch, lambda req: pytest.fail("unsupported editing submitted"))
    result = await generation.执行普通生成(plugin, 请求(operation="edit", references=REFS))
    assert not result["success"] and not calls
    assert plugin.ctx.maisaka.context.resolve_image.await_count == 0


def test_sdk_component_schema_and_manifest():
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert "maisaka.context.resolve_image" in manifest["capabilities"]
    schema = entry.MaisArtPlugin.build_config_schema()
    assert "default_generation_model" in json.dumps(schema)
    assert "max_reference_images" in json.dumps(schema)
    components = entry.MaisArtPlugin().get_components()
    tool = next(item for item in components if item["name"] == "draw_picture")
    assert tool["metadata"]["timeout_ms"] == 600000
    assert entry.MaisArtPlugin._DRAW_TOOL_PARAMETERS["references"]["items"]["properties"]["index"]["minimum"] == 0
