"""Private administrator publication tests: no image generation or real API calls."""
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import base64
import hashlib
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = '_mais_art_prepared_publication_tests'
for suffix, path in (('', ROOT), ('.core', ROOT/'core'), ('.core.config', ROOT/'core/config')):
    module = ModuleType(NAME+suffix)
    module.__path__ = [str(path)]
    sys.modules[module.__name__] = module
entry = import_module(NAME+'.plugin')


@pytest.fixture
def prepared(tmp_path):
    plugin = entry.MaisArtPlugin()
    plugin._ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), api=SimpleNamespace(call=AsyncMock()))
    directory = tmp_path/'selfie_test'
    directory.mkdir()
    binary = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4XcAAAAASUVORK5CYII=')
    file = directory/'preview-0.png'
    file.write_bytes(binary)
    return SimpleNamespace(plugin=plugin, file=file, binary=binary, sha=hashlib.sha256(binary).hexdigest())


def test_prepared_publication_is_private_and_declared_in_admin_gateway(prepared):
    components = prepared.plugin.get_components()
    api = next(item for item in components if item['name']=='start_preview_publication')
    assert api['type']=='API' and api['metadata']['public'] is False
    binding = json.loads((ROOT/'webui.json').read_text())['pages'][0]['actions']['publish_preview']
    assert binding['api']=='start_preview_publication' and binding['confirmation']
    assert set(binding['parameters'])=={'expected_sha256','message','expected_qq'}
    assert all(item['required'] for item in binding['parameters'].values())


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt,status', [
    ({'result':True,'message':'sent tid=known_123'},'success'),
    ({'result':False},'failed'),
    ({'result':True,'uncertain':True},'unknown'),
    ({'result':False,'uncertain':True},'unknown'),
    ({'result':1},'unknown'),
    (None,'unknown'),
])
async def test_prepared_image_is_published_once_without_generation(prepared, receipt, status):
    prepared.plugin.ctx.api.call.return_value = receipt
    launch = await prepared.plugin.api_start_preview_publication(prepared.sha, 'approved caption', expected_qq='123456')
    assert launch['status']=='running'
    await prepared.plugin._manual_test_task
    result = await prepared.plugin.api_selfie_test_status()
    assert result['stage']=='publication' and result['status']==status
    prepared.plugin.ctx.api.call.assert_awaited_once_with(
        'Rabbit-Jia-Er.MaiTrace.send_feed_checked_api', expected_qq='123456', message='approved caption',
        images=[base64.b64encode(prepared.binary).decode('ascii')])
    if status=='success':
        assert result['publication_id']=='known_123'
    assert 'approved caption' not in json.dumps(result)
    assert base64.b64encode(prepared.binary).decode('ascii') not in json.dumps(result)


@pytest.mark.asyncio
async def test_changed_or_missing_approved_image_never_publishes(prepared):
    prepared.file.write_bytes(b'changed after approval')
    await prepared.plugin.api_start_preview_publication(prepared.sha, 'approved caption', expected_qq='123456')
    await prepared.plugin._manual_test_task
    result = await prepared.plugin.api_selfie_test_status()
    assert result['stage']=='preview' and result['status']=='failed'
    prepared.plugin.ctx.api.call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('sha,message', [('bad','caption'),('0'*64,''),('0'*64,'  ')])
async def test_invalid_approval_does_not_launch(prepared, sha, message):
    assert (await prepared.plugin.api_start_preview_publication(sha,message,expected_qq='123456'))['success'] is False
    assert prepared.plugin._manual_test_task is None
    prepared.plugin.ctx.api.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_task_blocks_prepared_publication(prepared):
    prepared.plugin._auto_selfie_task = SimpleNamespace(is_running=True)
    result = await prepared.plugin.api_start_preview_publication(prepared.sha,'caption',expected_qq='123456')
    assert result['success'] is False
    prepared.plugin.ctx.api.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_exception_is_unknown_and_does_not_leak_raw_error(prepared):
    prepared.plugin.ctx.api.call.side_effect = RuntimeError('sensitive-cookie-in-error')
    await prepared.plugin.api_start_preview_publication(prepared.sha,'caption',expected_qq='123456')
    await prepared.plugin._manual_test_task
    result = await prepared.plugin.api_selfie_test_status()
    assert result['status']=='unknown' and result['exception_type']=='RuntimeError'
    assert 'sensitive' not in json.dumps(result)
    assert prepared.plugin.ctx.api.call.await_count==1


@pytest.mark.asyncio
async def test_cancellation_during_submission_is_unknown_and_not_retried(prepared):
    started = asyncio.Event()
    async def pending(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    prepared.plugin.ctx.api.call.side_effect = pending
    await prepared.plugin.api_start_preview_publication(prepared.sha,'caption',expected_qq='123456')
    await started.wait()
    result = await prepared.plugin.api_start_preview_publication(prepared.sha,'caption',expected_qq='123456')
    assert result['success'] is False
    await prepared.plugin.on_unload()
    assert (await prepared.plugin.api_selfie_test_status())['status']=='unknown'
    assert prepared.plugin.ctx.api.call.await_count==1


@pytest.mark.asyncio
async def test_missing_expected_account_does_not_launch_or_publish(prepared):
    result=await prepared.plugin.api_start_preview_publication(prepared.sha,'caption',expected_qq='')
    assert result['success'] is False
    assert prepared.plugin._manual_test_task is None
    prepared.plugin.ctx.api.call.assert_not_awaited()
