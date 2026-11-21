"""Scene planning data and delivery checks; these do not grade fiction."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass


DEFAULT_TARGET_WORDS = 3500
MIN_SCENE_WORDS = 1
MAX_SCENE_WORDS = 6000
MAX_SCENE_COUNT = 20

_REQUIRED_CONTRACT_FIELDS = (
    "start_state", "time_delta", "location_path", "entity_transfers",
    "power_resource_map", "information_state", "mechanism_limits",
    "result_strength", "transition_bridge", "continuity_ledger",
)
_CONDITIONAL_CONTRACT_FIELDS = (
    "action_budget", "inference_ledger", "evidence_strength",
    "npc_disclosure_budget", "actor_boundary", "foreshadow_seed", "register_guard",
    # 2026-11-17:凡本场涉及算钱/结算/收付,规划必须给出总额与口径,否则写手会当场
    # 自己发明一个自洽的数(实测 ch07 写 880 / 840,而 ch08 才写明 820)。
    # **可选**:不写不算错,坏值只丢弃,绝不因此阻断生成。
    "money_totals",
)
_FIELD_ALIASES = {
    "transition_bridge": ("handoff_to_next",),
    "continuity_ledger": ("ledger", "连续性台账"),
    "action_budget": ("动作预算",),
    "inference_ledger": ("推理台账",),
}


class ScenePlanValidationError(ValueError):
    """A known plan needs correction, rather than replacement by a template."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def scene_budget_bounds(target_words: int) -> tuple[int, int]:
    if type(target_words) is not int or target_words <= 0:
        raise ScenePlanValidationError("invalid_chapter_target", "target_words 必须为正整数")
    if target_words > MAX_SCENE_COUNT * MAX_SCENE_WORDS:
        raise ScenePlanValidationError(
            "scene_target_exceeds_capacity",
            f"单章 target_words={target_words} 超出 {MAX_SCENE_COUNT} 场 × "
            f"{MAX_SCENE_WORDS} 字的技术容量；不能静默缩减章节目标",
        )
    return (target_words * 85 + 99) // 100, target_words * 115 // 100


def recommended_scene_count(target_words: int, max_scene_words: int) -> int:
    """按"单场不超过 max_scene_words"算推荐场数。

    分段生成不是为了凑场数,是为了**限制单次生成的输出长度**:每场都会重新注入
    完整上下文和"本章此前已写正文",一次生成越长,开头的数量/时间/称谓约束
    越容易在中后段失效(实测 2026-11-14:默认整章一场)。

    上限未配置或非法时返回 1 = 不强制拆分,与改动前的行为一致。
    """
    try:
        ceiling = int(max_scene_words or 0)
    except (TypeError, ValueError):
        ceiling = 0
    if ceiling <= 0:
        return 1
    ceiling = max(MIN_SCENE_WORDS, min(MAX_SCENE_WORDS, ceiling))
    if type(target_words) is not int or target_words <= 0:
        return 1
    return max(1, min(MAX_SCENE_COUNT, -(-target_words // ceiling)))


def _text(raw: dict, field: str, idx: int, *, required: bool = False) -> str:
    value = raw.get(field)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ScenePlanValidationError("invalid_scene_field", f"scene[{idx}].{field} 必须为文字")
    text = value.strip()
    if required and not text:
        raise ScenePlanValidationError("missing_scene_field", f"scene[{idx}].{field} 不能为空")
    return text


def _has_content(value) -> bool:
    """Accept descriptive JSON ledgers, including numeric quantities, intact.

    Flags/null/empty containers alone do not describe a scene. They can still
    be used inside a ledger alongside names, states or quantities.
    """
    if isinstance(value, str):
        return bool(value.strip())
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_content(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_content(v) for v in value)
    return False


def _contract_value(value, field: str, idx: int) -> str:
    if value is None or value == "" or value == {} or value == []:
        return ""
    if isinstance(value, str):
        return value.strip()
    if field == "time_delta" and (type(value) is int or type(value) is float and math.isfinite(value)):
        # Preserve the supplied unitless number; do not invent a unit.
        return json.dumps(value, allow_nan=False)
    if isinstance(value, (dict, list)) and _has_content(value):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError):
            pass
    raise ScenePlanValidationError(
        "invalid_scene_field", f"scene[{idx}].{field} 必须为文字或有内容的 JSON 台账",
    )


def _contract_field(raw: dict, field: str, idx: int) -> str:
    values = [
        _contract_value(raw[key], field, idx)
        for key in (field, *_FIELD_ALIASES.get(field, ())) if key in raw
    ]
    nonempty = [v for v in values if v]
    if len(set(nonempty)) > 1:
        raise ScenePlanValidationError(
            "conflicting_scene_fields", f"scene[{idx}].{field} 与别名字段内容不一致",
        )
    return nonempty[0] if nonempty else ""


@dataclass
class SceneBrief:
    idx: int
    title: str
    brief: str
    pov: str = ""
    location: str = ""
    time_cue: str = ""
    key_action: str = ""
    target_words: int = 1000
    hook: str = ""
    start_state: str = ""
    time_delta: str = ""
    location_path: str = ""
    entity_transfers: str = ""
    power_resource_map: str = ""
    information_state: str = ""
    mechanism_limits: str = ""
    result_strength: str = ""
    transition_bridge: str = ""
    continuity_ledger: str = ""
    action_budget: str = ""
    inference_ledger: str = ""
    evidence_strength: str = ""
    npc_disclosure_budget: str = ""
    actor_boundary: str = ""
    foreshadow_seed: str = ""
    register_guard: str = ""
    # 本场已定的金额(名目=数额(依据)),可选。见 _CONDITIONAL_CONTRACT_FIELDS。
    money_totals: str = ""

    @classmethod
    def from_dict(cls, idx: int, raw: dict) -> "SceneBrief":
        if not isinstance(raw, dict):
            raise ScenePlanValidationError("invalid_scene_entry", f"scene[{idx}] 必须为对象")
        target = raw.get("target_words")
        if type(target) is not int or not MIN_SCENE_WORDS <= target <= MAX_SCENE_WORDS:
            raise ScenePlanValidationError(
                "invalid_scene_budget",
                f"scene[{idx}].target_words 必须为 {MIN_SCENE_WORDS}–{MAX_SCENE_WORDS} 的 JSON 整数",
            )
        text_fields = {
            name: _text(raw, name, idx, required=name == "brief")
            for name in ("title", "brief", "pov", "location", "time_cue", "key_action", "hook")
        }
        # A positional label is metadata, not a fabricated scene or fact.
        text_fields["title"] = text_fields["title"] or f"场景 {idx}"
        contract_fields = {
            name: _contract_field(raw, name, idx)
            for name in (*_REQUIRED_CONTRACT_FIELDS, *_CONDITIONAL_CONTRACT_FIELDS)
        }
        # The list order is canonical; model-supplied idx cannot reorder events.
        return cls(idx=idx, target_words=target, **text_fields, **contract_fields)

    def to_writer_user_content(self) -> str:
        bullets = [f"【场景号】第 {self.idx} 场"]
        fields = (
            ("标题", self.title), ("视角", self.pov), ("地点", self.location),
            ("时间", self.time_cue), ("主要动作", self.key_action),
            ("开场承接", self.start_state), ("时间成本", self.time_delta),
            ("空间路径", self.location_path), ("实体转移", self.entity_transfers),
            ("权力/资源图", self.power_resource_map), ("信息状态", self.information_state),
            ("机制边界", self.mechanism_limits), ("允许结果强度", self.result_strength),
            ("下场交接", self.transition_bridge), ("连续性台账", self.continuity_ledger),
            ("动作预算", self.action_budget), ("推理台账", self.inference_ledger),
            ("证据支持范围", self.evidence_strength), ("人物透露信息的缘由", self.npc_disclosure_budget),
            ("立场变化缘由", self.actor_boundary), ("本场伏笔安排", self.foreshadow_seed),
            ("人物语言", self.register_guard), ("本场已定金额", self.money_totals),
        )
        bullets.extend(f"【{label}】{value}" for label, value in fields if value)
        bullets.append(f"【目标字数】约 {self.target_words} 字(允许 ±15%)。写不满先补足本场转折与人物反应,"
                       "不要靠感慨、环境或重复凑数;写超出先压缩次要过程")
        if self.hook:
            bullets.append(f"【场末过渡】{self.hook}")
        else:
            bullets.append("【场末过渡】未指定额外过渡，按本场任务自然收束；不据此提前写全章结尾或新增悬念。")
        if self.brief:
            bullets.extend(("【场景 brief】", self.brief))
        return "\n".join(bullets)


def _cap_parsed_scenes(parsed: list) -> list:
    if len(parsed) > MAX_SCENE_COUNT:
        raise ScenePlanValidationError("scene_count_exceeds_limit", f"{len(parsed)} > {MAX_SCENE_COUNT}")
    if any(not isinstance(scene, dict) for scene in parsed):
        raise ScenePlanValidationError("invalid_scene_entry", "每个场景必须为对象")
    return parsed


def _missing_contract_fields(brief: SceneBrief) -> list[str]:
    return [
        field for field in _REQUIRED_CONTRACT_FIELDS
        if not isinstance(getattr(brief, field, None), str) or not getattr(brief, field).strip()
    ]


def _has_valid_scene_contract(briefs: list[SceneBrief]) -> bool:
    return bool(briefs) and all(not _missing_contract_fields(brief) for brief in briefs)


def _scene_budget_reaches_target(briefs: list[SceneBrief], target_words: int) -> bool:
    """Compatibility name: now check both sides of the chapter budget."""
    try:
        lower, upper = scene_budget_bounds(target_words)
    except ScenePlanValidationError:
        return False
    if not briefs or any(type(b.target_words) is not int for b in briefs):
        return False
    return lower <= sum(b.target_words for b in briefs) <= upper


def validate_scene_plan(payload, target_words: int) -> list[SceneBrief]:
    """Keep every scene and every supported contract; never fix by omission."""
    lower, upper = scene_budget_bounds(target_words)
    if isinstance(payload, list):
        scenes = payload
    elif isinstance(payload, dict) and not payload.get("parse_error") and not payload.get("_parse_error"):
        wrappers = [key for key in ("items", "scenes", "scene_briefs", "scene_list") if key in payload]
        if len(wrappers) != 1:
            raise ScenePlanValidationError("invalid_scene_wrapper", "只能有一个明确的场景数组")
        scenes = payload[wrappers[0]]
    else:
        raise ScenePlanValidationError("invalid_scene_wrapper", "需要完整场景数组")
    if not isinstance(scenes, list) or not scenes:
        raise ScenePlanValidationError("empty_scene_plan", "场景数组不能为空且必须为数组")
    _cap_parsed_scenes(scenes)
    briefs = []
    errors = []
    for idx, raw in enumerate(scenes, 1):
        try:
            brief = SceneBrief.from_dict(idx, raw)
            missing = _missing_contract_fields(brief)
            if missing:
                raise ScenePlanValidationError("missing_contract_fields", f"scene[{idx}]: {', '.join(missing)}")
            briefs.append(brief)
        except ScenePlanValidationError as exc:
            errors.append(str(exc))
    if errors:
        raise ScenePlanValidationError("invalid_scene_plan", "; ".join(errors))
    total = sum(b.target_words for b in briefs)
    if not lower <= total <= upper:
        code = "insufficient_scene_budget" if total < lower else "excessive_scene_budget"
        raise ScenePlanValidationError(
            code, f"target_words 合计 {total}，本章目标 {target_words}，允许范围 {lower}–{upper}；"
            "保留全部事件并调整详略分配，不删除中间事件",
        )
    return briefs
