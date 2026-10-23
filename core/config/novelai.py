"""原生 NovelAI 的模型专属选项；通用采样参数沿用 ModelConfig。"""

from maibot_sdk import Field, PluginConfigBase
from typing import Literal


class NovelAIConfig(PluginConfigBase):
    """仅接受已实现的单图参数，避免额外字段绕过请求约束。"""

    model_config = {"extra": "forbid", "validate_assignment": True}

    __ui_label__ = "原生 NovelAI"

    mode: Literal["anime", "furry", "background"] = Field(
        default="anime",
        description="数据集模式；V3 须与 Anime/Furry 模型对应，background 仅支持 V4.5/V5",
        json_schema_extra={"label": "数据集模式", "order": 0},
    )
    quality_tags: bool = Field(
        default=True,
        description="在本地按模型版本添加质量标签，不发送旧版 qualityToggle 参数",
        json_schema_extra={"label": "添加质量标签", "order": 1},
    )
    smea: bool = Field(
        default=False,
        description="SMEA，仅支持 V3 文生图；其他模式开启时会明确报错",
        json_schema_extra={"label": "SMEA", "order": 2},
    )
    smea_dynamic: bool = Field(
        default=False,
        description="动态 SMEA，须同时开启 SMEA",
        json_schema_extra={"label": "动态 SMEA", "order": 3},
    )
    img2img_strength: float = Field(
        default=0.6, ge=0.0, le=1.0,
        description="普通 NovelAI 图生图的重绘强度；自拍默认用模型 selfie_reference_strength，本次显式 strength 的优先级最高",
        json_schema_extra={"label": "图生图强度", "step": 0.05, "order": 4},
    )
    img2img_noise: float = Field(
        default=0.1, ge=0.0, le=1.0,
        description="仅原生 NovelAI 图生图的附加噪声强度，文生图不使用",
        json_schema_extra={"label": "图生图噪声", "step": 0.05, "order": 5},
    )
    request_timeout_seconds: int = Field(
        default=120, ge=5, le=150,
        description="单次 HTTP 请求超时，仍受绘卷整条生成链的总时限约束",
        json_schema_extra={"label": "请求超时（秒）", "order": 6},
    )
