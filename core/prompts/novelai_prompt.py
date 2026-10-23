"""把绘卷内部的 SD 权重转换为 NovelAI 提示词语法。"""

from typing import Match

import math
import re


_SD_WEIGHT = re.compile(r"(?<!\\)\(([^()\r\n]+):([+-]?(?:\d+(?:\.\d*)?|\.\d+))\)")


def convert_novelai_weights(text: str, *, v3: bool) -> str:
    """保留原生 NAI 语法，仅转换明确的 `(标签:数值)` 片段。

    V3 的花括号每层增强 1.05 倍、方括号每层衰减 1.05 倍，故转换为
    最接近的层数；V4 及以上使用数值权重，保持原来的数值。
    """

    def replace_weight(match: Match[str]) -> str:
        tag = match.group(1).strip()
        weight = float(match.group(2))
        if not math.isfinite(weight) or abs(weight) > 10:
            raise ValueError("NovelAI 提示词权重须为 -10 到 10 之间的有限数值")
        if not v3:
            return f"{weight:g}::{tag}::"
        if weight <= 0:
            raise ValueError("NovelAI V3 不支持零或负数 SD 权重，请使用正权重或反向提示词")
        layers = round(math.log(weight) / math.log(1.05))
        if abs(layers) > 100:
            raise ValueError("NovelAI V3 的提示词权重过小，无法转换为合理的括号层数")
        if layers > 0:
            return "{" * layers + tag + "}" * layers
        if layers < 0:
            return "[" * -layers + tag + "]" * -layers
        return tag

    return _SD_WEIGHT.sub(replace_weight, text)
