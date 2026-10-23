"""日程自拍发布决策：硬规则筛选候选，模型只能选择本地候选编号或跳过。"""

from typing import TYPE_CHECKING, Any, Dict, List

import asyncio
import json
import re

from ..prompts.prompt_templates import load_prompt_template
from .caption_generator import _get_bot_profile
from .proactive_context import GroupContext
from .schedule_provider import ActivityInfo

if TYPE_CHECKING:
    from ...plugin import MaisArtPlugin


def parse_publication_decision(response: str, candidates: List[str]) -> Dict[str, Any]:
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
    decision = json.loads(clean)
    if not isinstance(decision, dict) or set(decision) - {"destination", "reason", "prompt"}:
        raise ValueError("自拍发布判断必须为限定字段的 JSON 对象")
    destination = decision.get("destination")
    reason = decision.get("reason")
    if not isinstance(destination, str) or destination not in ["skip", *candidates]:
        raise ValueError("自拍发布渠道不在本轮候选中")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
        raise ValueError("自拍发布判断缺少有效理由")
    prompt = decision.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 1500 or (destination != "skip" and not prompt.strip()):
        raise ValueError("自拍发布判断缺少有效图片描述")
    return {"destination": destination, "reason": reason.strip(), "prompt": prompt.strip()}


async def decide_publication(plugin: "MaisArtPlugin", activity: ActivityInfo, groups: Dict[str, GroupContext], qzone_allowed: bool) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    if qzone_allowed:
        candidates.append({"id": "qzone", "channel": "qzone"})
    candidates.extend({"id": identifier, "channel": "group", "name": context.name, "messages": context.messages} for identifier, context in groups.items())
    payload = {
        "persona": await _get_bot_profile(plugin.ctx), "activity": activity.to_payload(),
        "candidates": candidates, "recent_publications": plugin.proactive_state.recent(),
    }
    system_prompt = await asyncio.to_thread(load_prompt_template, "selfie_publication")
    result = await plugin.ctx.llm.generate(
        prompt=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model=plugin.config.basic.llm_task_name, temperature=0.2, max_tokens=2048,
        timeout_ms=plugin.config.basic.llm_timeout_seconds * 1000,
    )
    if not isinstance(result, dict) or result.get("success") is False:
        raise RuntimeError("自拍发布判断 LLM 调用失败")
    response = result.get("response", result.get("content", ""))
    if not isinstance(response, str):
        raise ValueError("自拍发布判断 LLM 返回非文本响应")
    return parse_publication_decision(response, [item["id"] for item in candidates])
