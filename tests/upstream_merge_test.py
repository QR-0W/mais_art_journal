"""上游兼容修复离线回归：只用测试内配置、工作流和传输替身。"""

from importlib import import_module
from inspect import signature
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, mock_open

import json
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_mais_art_upstream_merge_tests"
for suffix, path in (
    ("", ROOT),
    (".core", ROOT / "core"),
    (".core.api_clients", ROOT / "core/api_clients"),
):
    module = ModuleType(PACKAGE + suffix)
    module.__path__ = [str(path)]
    sys.modules[module.__name__] = module

contexts = import_module(PACKAGE + ".core.api_clients.client_context")
base = import_module(PACKAGE + ".core.api_clients.base_client")
comfy = import_module(PACKAGE + ".core.api_clients.comfyui_client")


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("离线测试禁止真实网络")

    monkeypatch.setattr("socket.socket.connect", reject)
    monkeypatch.setattr("socket.socket.connect_ex", reject)
    monkeypatch.setattr(comfy.urllib.request.OpenerDirector, "open", reject)


def test_context_configuration_error_is_not_replaced_with_default():
    error = RuntimeError("offline getter failed")
    context = contexts.ClientContext(config_getter=Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        context.get_config("proxy.enabled", False)
    assert caught.value is error


def test_proxy_configuration_error_is_not_replaced_with_direct_connection():
    context = contexts.ClientContext()
    error = RuntimeError("offline proxy failed")
    context.get_proxy_config = Mock(side_effect=error)
    with pytest.raises(RuntimeError) as caught:
        base.BaseApiClient(context)._get_proxy_config()
    assert caught.value is error


@pytest.mark.parametrize("key", ["proxy.enabled", "proxy.url", "proxy.timeout"])
def test_legacy_context_errors_remain_visible(key):
    error = RuntimeError("offline legacy getter failed")

    def getter(requested, default=None):
        if requested == key:
            raise error
        return {"proxy.enabled": True, "proxy.url": "http://offline-proxy.invalid:7890", "proxy.timeout": 5}.get(requested, default)

    context = SimpleNamespace(log_prefix="[legacy offline]", get_config=getter)
    client = base.BaseApiClient(context)
    assert client.ctx is context and client.action is context
    with pytest.raises(RuntimeError) as caught:
        client._get_proxy_config()
    assert caught.value is error


def test_legacy_context_proxy_contract_is_preserved():
    values = {"proxy.enabled": True, "proxy.url": "http://offline-proxy.invalid:7890", "proxy.timeout": 5}
    context = SimpleNamespace(log_prefix="[legacy offline]", get_config=lambda key, default=None: values.get(key, default))
    client = base.BaseApiClient(context)
    assert client._get_proxy_config() == {"http": values["proxy.url"], "https": values["proxy.url"], "timeout": 5}
    values["proxy.enabled"] = False
    assert client._get_proxy_config() is None


def workflow_client(monkeypatch, template):
    client = comfy.ComfyUIClient(contexts.ClientContext())
    monkeypatch.setattr(client, "_build_opener", Mock(return_value=(object(), 120)))
    monkeypatch.setattr(comfy.os.path, "exists", Mock(return_value=True))
    monkeypatch.setattr("builtins.open", mock_open(read_data=template))
    return client


@pytest.mark.asyncio
async def test_required_reference_missing_placeholder_never_checks_queue_uploads_or_retries(monkeypatch):
    client = workflow_client(monkeypatch, '{"prompt":"${prompt}"}')
    for name in ("_queue_has_own_task", "_upload_image_sync", "_queue_prompt_sync"):
        monkeypatch.setattr(client, name, Mock(side_effect=AssertionError("参考图未使用时不能继续请求")))
    request = Mock(wraps=client._make_request)
    monkeypatch.setattr(client, "_make_request", request)
    success, error = await client.generate_image(
        "offline prompt", {"model": "offline_workflow.json"}, "64x64",
        input_image_base64="required-reference", max_retries=2,
    )
    assert success is False and "${image}" in error and "本次未提交" in error
    assert request.call_count == 1
    client._queue_has_own_task.assert_not_called()
    client._upload_image_sync.assert_not_called()
    client._queue_prompt_sync.assert_not_called()


@pytest.mark.asyncio
async def test_text_only_workflow_keeps_existing_queue_short_circuit(monkeypatch):
    client = workflow_client(monkeypatch, '{"prompt":"${prompt}"}')
    monkeypatch.setattr(client, "_queue_has_own_task", Mock(return_value=True))
    monkeypatch.setattr(client, "_queue_prompt_sync", Mock(side_effect=AssertionError("已有自己的队列任务时不能提交")))
    request = Mock(wraps=client._make_request)
    monkeypatch.setattr(client, "_make_request", request)
    success, error = await client.generate_image(
        "offline prompt", {"model": "offline_workflow.json"}, "64x64", max_retries=2,
    )
    assert success is False and error.startswith(base.QUEUED_MARK)
    assert request.call_count == 1
    client._queue_prompt_sync.assert_not_called()


@pytest.mark.asyncio
async def test_reference_workflow_uses_reference_and_polls_once(monkeypatch):
    client = workflow_client(monkeypatch, '{"prompt":"${prompt}","image":"${image}"}')
    monkeypatch.setattr(client, "_queue_has_own_task", Mock(return_value=False))
    monkeypatch.setattr(client, "_resize_image_b64_for_img2img", Mock(return_value="prepared-reference"))
    monkeypatch.setattr(client, "_upload_image_sync", Mock(return_value="uploaded-reference.png"))
    monkeypatch.setattr(client, "_queue_prompt_sync", Mock(return_value="offline-job"))
    monkeypatch.setattr(client, "_poll_history_sync", Mock(return_value="result.png"))
    monkeypatch.setattr(client, "_download_image_sync", Mock(return_value="offline-output"))
    success, result = await client.generate_image(
        "offline prompt", {"model": "offline_workflow.json"}, "64x64",
        input_image_base64="required-reference", max_retries=2,
    )
    assert success is True and result == "offline-output"
    assert client._queue_prompt_sync.call_count == 1
    submitted = client._queue_prompt_sync.call_args.args[1]
    assert submitted == {"prompt": "offline prompt", "image": "uploaded-reference.png"}
    assert client._upload_image_sync.call_args.args[1] == "prepared-reference"
    client._poll_history_sync.assert_called_once_with("http://127.0.0.1:8188", "offline-job", client._build_opener.return_value[0], timeout=120)


def test_history_polling_default_is_bounded_without_network_or_sleep(monkeypatch):
    assert signature(comfy.ComfyUIClient._poll_history_sync).parameters["timeout"].default == 120
    client = comfy.ComfyUIClient(contexts.ClientContext())
    opener = Mock()
    response = Mock(status=200)
    response.read.return_value = json.dumps({}).encode()
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(comfy.time, "time", Mock(side_effect=[0, 0, 121]))
    sleep = Mock()
    monkeypatch.setattr(comfy.time, "sleep", sleep)
    assert client._poll_history_sync("https://offline.invalid", "offline-job", opener) is None
    assert opener.open.call_count == 1
    sleep.assert_called_once_with(1)
