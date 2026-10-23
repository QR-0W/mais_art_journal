"""离线测试环境：只导入插件代码，禁止真实 HTTP 请求。"""

from pathlib import Path
from typing import Any

import pytest
import requests
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_request(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("离线回归禁止发起真实 HTTP 请求")

    monkeypatch.setattr(requests.Session, "request", reject_request)
