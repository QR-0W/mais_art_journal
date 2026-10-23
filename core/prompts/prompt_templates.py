"""加载随插件分发的多语言提示词，默认使用简体中文。"""

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=15)
def load_prompt_template(name: str, language: str = "zh") -> str:
    """模板缺失或语言不受支持时直接报错，避免悄悄使用不同规则。"""
    if name not in {"scene", "group_image_decision", "selfie_publication", "selfie_caption", "prompt_optimizer"}:
        raise ValueError(f"未知提示词模板: {name}")
    if language not in {"zh", "en", "ja"}:
        raise ValueError(f"不支持的提示词语言: {language}")
    return (Path(__file__).parent / "templates" / f"{name}_{language}.txt").read_text(encoding="utf-8").strip()
