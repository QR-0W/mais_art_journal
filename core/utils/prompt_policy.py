"""生图后端共用的提示词语法与参考图能力规则。"""

import re


def uses_weighted_tags(api_format: str, prompt_syntax: str = "auto") -> bool:
    """兼容网关可显式覆盖；原生 NovelAI 客户端转换内部 SD 权重。"""
    if prompt_syntax not in {"auto", "natural", "tags"}:
        raise ValueError(f"未知提示词语法: {prompt_syntax}")
    if prompt_syntax != "auto":
        return prompt_syntax == "tags"
    return api_format in {"modelscope", "shatangyun", "mengyuai", "comfyui", "novelai"}


def supports_reference_strength(api_format: str, prompt_syntax: str = "auto") -> bool:
    """只给实际消费强度的客户端设值，保留调用方显式指定的强度。"""
    return api_format in {"comfyui", "novelai"} or (api_format == "openai" and prompt_syntax == "tags")


def strip_prompt_weights(text: str) -> str:
    """去除强调语法，保留词语和引号内需要画出的原始文字。"""
    def strip_segment(segment: str) -> str:
        previous = ""
        while previous != segment:
            previous = segment
            segment = re.sub(r"\(([^()]*?):[+-]?(?:\d+(?:\.\d*)?|\.\d+)\)", r"\1", segment)
            segment = re.sub(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*::([^:]+)::", r"\1", segment)
            segment = re.sub(r"\{([^{}]+)\}", r"\1", segment)
        return segment

    segments = re.split(r'''("(?:\\.|[^"\\])*"|(?<!\w)'(?:\\.|[^'\\])*'(?!\w))''', text)
    return "".join(segment if index % 2 else strip_segment(segment) for index, segment in enumerate(segments))
