"""自动风格和换装的共同规则源，配置默认值与确定性场景使用同一份映射。"""

from typing import Dict, List, Optional

from .schedule_provider import ActivityInfo, ActivityType


ACTIVITY_STYLES: Dict[ActivityType, str] = {
    ActivityType.SLEEPING: "photo", ActivityType.WAKING_UP: "standard",
    ActivityType.EATING: "standard", ActivityType.WORKING: "photo",
    ActivityType.STUDYING: "photo", ActivityType.EXERCISING: "photo",
    ActivityType.RELAXING: "standard", ActivityType.SOCIALIZING: "standard",
    ActivityType.COMMUTING: "photo", ActivityType.HOBBY: "photo",
    ActivityType.SELF_CARE: "mirror", ActivityType.COOKING: "photo",
    ActivityType.SHOPPING: "mirror", ActivityType.GAMING: "photo",
    ActivityType.BATHING: "photo", ActivityType.OTHER: "standard",
}
ACTIVITY_OUTFITS: Dict[ActivityType, str] = {
    ActivityType.SLEEPING: "comfortable pajamas",
    ActivityType.WAKING_UP: "comfortable pajamas",
    ActivityType.EATING: "casual everyday clothes",
    ActivityType.WORKING: "neat work clothes",
    ActivityType.STUDYING: "comfortable everyday clothes",
    ActivityType.EXERCISING: "sportswear and running shoes",
    ActivityType.RELAXING: "comfortable loungewear",
    ActivityType.SOCIALIZING: "casual outfit suitable for meeting friends",
    ActivityType.COMMUTING: "weather-appropriate outdoor clothes",
    ActivityType.HOBBY: "casual everyday clothes",
    ActivityType.SELF_CARE: "comfortable loungewear",
    ActivityType.COOKING: "casual clothes with a cooking apron",
    ActivityType.SHOPPING: "casual outdoor clothes",
    ActivityType.GAMING: "comfortable loungewear",
    ActivityType.BATHING: "fully covered bathrobe after bathing",
    ActivityType.OTHER: "casual everyday clothes",
}


def default_activity_rules() -> List[Dict[str, str]]:
    """按枚举顺序生成完整默认规则，避免 WebUI 和执行逻辑各维护一份默认值。"""
    return [
        {"activity_type": activity.value, "selfie_style": ACTIVITY_STYLES[activity], "outfit": ACTIVITY_OUTFITS[activity]}
        for activity in ActivityType
    ]


def style_candidates(activity: ActivityInfo) -> List[str]:
    """镜子必须属于当前场景；需要双手的活动优先外部镜头。"""
    scene = f"{activity.description} {activity.location} {activity.scene}".lower()
    mirror_available = any(word in scene for word in ("mirror", "vanity", "dressing room", "镜", "梳妆", "试衣"))
    taking_break = any(word in scene for word in ("休息", "做完", "结束", "之后", "跑完", "break", "after", "finished"))
    hands_busy = activity.activity_type in {
        ActivityType.SLEEPING, ActivityType.WORKING, ActivityType.STUDYING,
        ActivityType.EXERCISING, ActivityType.COOKING, ActivityType.GAMING, ActivityType.BATHING,
    } and not taking_break
    candidates = ["photo"] if hands_busy else ["standard", "photo"]
    if mirror_available:
        candidates.append("mirror")
    return candidates


def choose_auto_style(activity: ActivityInfo, preferred: Optional[str] = None, recent_styles: Optional[List[str]] = None) -> str:
    """从合理构图中按活动偏好和最近使用次数选择；无历史时保留活动偏好。"""
    candidates = style_candidates(activity)
    preferred = preferred or ACTIVITY_STYLES[activity.activity_type]
    recent = (recent_styles or [])[-6:]
    return min(candidates, key=lambda style: (
        style == recent[-1] if recent else False,
        recent.count(style), style != preferred, candidates.index(style),
    ))
