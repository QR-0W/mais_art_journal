"""自拍参考图只保留身份锚点；服装和后端降噪参数由本次场景决定。"""

from pathlib import Path
from typing import Optional

import base64


def load_reference_image(plugin_dir: str, configured_path: str) -> Optional[str]:
    """未配置时返回空；已配置但不可读时暴露文件错误，避免悄悄改走另一条生成链。"""
    if not configured_path.strip():
        return None
    path = Path(configured_path.strip())
    if not path.is_absolute():
        path = Path(plugin_dir) / path
    data = path.read_bytes()
    if not data:
        raise ValueError(f"自拍参考图为空文件: {path}")
    return base64.b64encode(data).decode("ascii")
