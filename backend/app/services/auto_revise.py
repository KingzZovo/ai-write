"""v1.5.0 C2 — auto-revise helpers for chapter generation.

Given a ChapterEvaluator result (overall + 5 dim scores + issues), decide
whether to revise (overall < threshold) and convert the issues list into a
Chinese revise instruction that can be appended to the next writer pass
(SceneOrchestrator.orchestrate_chapter_stream's user_instruction).

Design notes:
- Threshold defaults to 8.2 so project generation only accepts chapters that
  pass the same quality bar used in manual验收.
- Maximum revise rounds defaults to 3 to bound LLM cost while allowing
  convergence on continuity / hallucination defects.
- We cap issues per dimension to 5 to avoid prompt explosion when the LLM
  evaluator returns a long flat list.
- This module has zero DB / LLM / IO side effects — pure helpers, easy to
  unit-test deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.services.narrative_contract import REVISE_CONTRACT_PROMPT

# Tunable defaults; overridable via GenerateChapterRequest fields.
DEFAULT_REVISE_THRESHOLD: float = 8.2
DEFAULT_MAX_REVISE_ROUNDS: int = 3
MAX_ISSUES_PER_DIMENSION: int = 5
MAX_ISSUES_PER_VIOLATION_TYPE: int = 6

# Numeric score alone is not enough for acceptance. These labels mean the
# chapter still has continuity / causality jumps under the world-logic
# contract, even if the evaluator phrases them as minor issues. The list is
# genre-agnostic: it is based on contract violation categories, not on any
# specific book, era, or plot element.
BLOCKING_CONTRACT_VIOLATION_TYPES: frozenset[str] = frozenset(
    {
        "time_rule_violation",
        "space_rule_violation",
        "information_rule_violation",
        "mechanism_rule_violation",
        "power_resource_violation",
        "result_strength_violation",
    }
)

_DIMENSION_LABELS: dict[str, str] = {
    "plot_coherence": "剧情连贯性",
    "character_consistency": "角色一致性",
    "style_adherence": "风格贴合度",
    "narrative_pacing": "叙事节奏",
    "foreshadow_handling": "伏笔处理",
}


@dataclass
class EvaluationLite:
    """Lightweight stand-in for ChapterEvaluator.EvaluationResult.

    Lets unit tests build inputs without importing the heavy evaluator
    module / hitting model_router import side effects.
    """

    plot_coherence: float = 0.0
    character_consistency: float = 0.0
    style_adherence: float = 0.0
    narrative_pacing: float = 0.0
    foreshadow_handling: float = 0.0
    overall: float = 0.0
    issues: list[dict] | None = None


def _coerce_overall(eval_obj: Any) -> float:
    """Tolerate either an EvaluationResult dataclass or a plain dict."""
    if eval_obj is None:
        return 0.0
    if isinstance(eval_obj, dict):
        return float(eval_obj.get("overall", 0.0) or 0.0)
    return float(getattr(eval_obj, "overall", 0.0) or 0.0)


def _coerce_issues(eval_obj: Any) -> list[dict]:
    if eval_obj is None:
        return []
    if isinstance(eval_obj, dict):
        return list(eval_obj.get("issues") or [])
    return list(getattr(eval_obj, "issues", None) or [])


def _coerce_scores(eval_obj: Any) -> dict[str, float]:
    if eval_obj is None:
        return {}
    out: dict[str, float] = {}
    for dim in _DIMENSION_LABELS:
        if isinstance(eval_obj, dict):
            v = eval_obj.get(dim, 0.0)
        else:
            v = getattr(eval_obj, dim, 0.0)
        try:
            out[dim] = float(v or 0.0)
        except (TypeError, ValueError):
            out[dim] = 0.0
    return out


def _issue_violation_type(issue: dict) -> str:
    """Extract a contract violation type from explicit fields or labels."""
    for key in ("violation_type", "type"):
        raw = issue.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    text = " ".join(
        str(issue.get(key) or "") for key in ("description", "suggestion")
    )
    match = re.search(r"\[([a-z_]+_violation)\]", text)
    return match.group(1) if match else ""


def blocking_contract_violations(eval_obj: Any) -> list[str]:
    """Return blocking world-logic violation labels present in evaluation."""
    found: list[str] = []
    seen: set[str] = set()
    for issue in _coerce_issues(eval_obj):
        if not isinstance(issue, dict):
            continue
        vtype = _issue_violation_type(issue)
        if vtype in BLOCKING_CONTRACT_VIOLATION_TYPES and vtype not in seen:
            found.append(vtype)
            seen.add(vtype)
    return found


def blocking_contract_violation_set(eval_obj: Any) -> set[str]:
    """Set form for retry-gate comparisons."""
    return set(blocking_contract_violations(eval_obj))


def should_stop_random_retry(
    eval_obj: Any,
    previous_blocking_violations: set[str] | None,
) -> bool:
    """Stop blind retries when the same structural violation type persists.

    Re-running the whole chapter after the same time/space/information/
    mechanism/power/result-strength class survives a rewrite is lottery-style
    generation. At that point the system should stop, surface a root-cause
    repair plan, and require targeted upstream/outline/local fixes instead of
    drawing another sample from the model.
    """
    if not previous_blocking_violations:
        return False
    current = blocking_contract_violation_set(eval_obj)
    return bool(current & previous_blocking_violations)


_VIOLATION_REPAIR_METHODS: dict[str, str] = {
    "time_rule_violation": "补时间差、准备/恢复/传递耗时；若补不了，降低行动速度和结果强度。",
    "space_rule_violation": "建立人物/物件/消息移动路径；明确入口、权限、见证者、时间成本；若补不了，拆分角色或删除新增资源。",
    "power_resource_violation": "写清双方资源差、低资源方的规则漏洞/信息差/代价；禁止无成本压倒高资源方。",
    "information_rule_violation": "给信息来源、可接触性、可信度和替代解释；若来源不足，把强结论降级为疑点/假设。",
    "mechanism_rule_violation": "补触发顺序、条件、成本、边界、副作用、冷却和反制；让机关/能力像可运行流程而不是视觉效果。",
    "result_strength_violation": "把定论、翻盘、可靠收获降级为局部胜利、暂缓、诱饵风险或后续线索。",
    "expression_contract_violation": "压缩说明腔、拆短句、替换重复动作和口号式对白。",
    "untyped_issue": "先判断问题属于补支撑、降级结果、删除冲突设定、拆分人物还是提前埋伏笔。",
}


def _group_issues_by_violation_type(issues: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        vtype = _issue_violation_type(issue) or "untyped_issue"
        grouped.setdefault(vtype, []).append(issue)
    return grouped


def build_root_cause_repair_plan(
    eval_obj: Any,
    *,
    previous_blocking_violations: set[str] | None = None,
    max_per_type: int = MAX_ISSUES_PER_VIOLATION_TYPE,
) -> str:
    """Convert evaluator issues into a deterministic root-cause repair plan.

    This is intentionally not a prose rewrite prompt alone. It forces a repair
    decision before another generation pass so the system fixes structure
    rather than repeatedly sampling full chapters until one happens to pass.
    """
    issues = _coerce_issues(eval_obj)
    grouped = _group_issues_by_violation_type(issues)
    blocking_now = blocking_contract_violation_set(eval_obj)
    repeated = sorted(blocking_now & (previous_blocking_violations or set()))

    lines: list[str] = []
    lines.append("【根因修复蓝图】")
    if repeated:
        lines.append(
            "以下世界逻辑违规类型已经跨轮重复出现，禁止继续整章随机重抽；必须先按蓝图定点修复："
            + "、".join(repeated)
        )
    else:
        lines.append("先按违规类型确定修复策略，再进入正文改写；不得只把问题列表塞回去重写。")
    lines.append("修复决策只能选以下五类：补支撑 / 降级结果 / 删除冲突设定 / 拆分人物或资源 / 提前埋伏笔。")

    for vtype, items in grouped.items():
        method = _VIOLATION_REPAIR_METHODS.get(vtype, _VIOLATION_REPAIR_METHODS["untyped_issue"])
        lines.append(f"\n【{vtype}】根因：{method}")
        for item in items[:max_per_type]:
            loc = item.get("location") or item.get("paragraph") or "?"
            desc = (item.get("description") or "").strip()
            sugg = (item.get("suggestion") or "").strip()
            if desc:
                lines.append(f"- 段落 {loc}: {desc}")
            if sugg:
                lines.append(f"  修复动作: {sugg}")
        if len(items) > max_per_type:
            lines.append(f"…（另有 {len(items) - max_per_type} 条同类问题，按同一根因一起修）")

    lines.append("\n【改写前强制台账】")
    lines.append("- 列出每个关键人物、物件、消息、证据、资源在每场开始/结束的位置、持有人、知情人、转移路径和代价。")
    lines.append("- 下一场只能从上一场台账状态推进；若台账无法解释，就必须降级结果或删除该桥段。")
    lines.append("- 章节结尾只能带走台账允许的局部成果；疑似线索必须标记风险和待验证项。")
    return "\n".join(lines)



def should_revise(
    eval_obj: Any,
    threshold: float = DEFAULT_REVISE_THRESHOLD,
) -> bool:
    """True iff score is below threshold or blocking contract issues remain.

    A chapter cannot be accepted while it still has time/space/information/
    mechanism/power/result-strength violations: those are continuity jumps even
    when phrased as "minor" issues by the evaluator. NaN / None overall is
    treated as 0 (revise).
    """
    overall = _coerce_overall(eval_obj)
    return overall < float(threshold) or bool(blocking_contract_violations(eval_obj))


def issues_to_revise_instruction(
    eval_obj: Any,
    *,
    round_idx: int = 1,
    max_per_dimension: int = MAX_ISSUES_PER_DIMENSION,
) -> str:
    """Convert evaluator issues + scores into a Chinese revise instruction.

    The output is intended to be APPENDED to the next writer's
    user_instruction (scene-mode), so it must be self-contained and not
    leak the original instruction.

    Always emits a non-empty string — even when issues list is empty,
    we still surface the dimension scores so the writer knows where to
    aim higher.
    """
    overall = _coerce_overall(eval_obj)
    scores = _coerce_scores(eval_obj)
    issues = _coerce_issues(eval_obj)

    lines: list[str] = []
    lines.append(build_root_cause_repair_plan(eval_obj))
    lines.append("")
    lines.append(f"【重写要求 - 第 {round_idx} 轮】")
    lines.append(f"上一稿质量评分 overall={overall:.2f}/10。各维度得分：")
    for dim, label in _DIMENSION_LABELS.items():
        s = scores.get(dim, 0.0)
        lines.append(f"  - {label} ({dim}): {s:.1f}")

    # Group issues by dimension so the writer sees clustered guidance.
    grouped: dict[str, list[dict]] = {}
    for it in issues:
        if not isinstance(it, dict):
            continue
        dim = str(it.get("dimension", "general"))
        grouped.setdefault(dim, []).append(it)

    if grouped:
        lines.append("")
        lines.append("需要重点修正的问题（按维度分组）：")
        for dim, items in grouped.items():
            label = _DIMENSION_LABELS.get(dim, dim)
            lines.append(f"\n【{label}】")
            for it in items[:max_per_dimension]:
                loc = it.get("location") or it.get("paragraph") or "?"
                desc = (it.get("description") or "").strip()
                sugg = (it.get("suggestion") or "").strip()
                if desc:
                    lines.append(f"- 段落 {loc}：{desc}")
                    if sugg:
                        lines.append(f"  改进建议：{sugg}")
            extra = max(0, len(items) - max_per_dimension)
            if extra:
                lines.append(f"…（其他 {extra} 条同类问题一并改进）")

    lines.append("")
    lines.append(
        "请重写本章：保留原情节主线与关键成果，在上述问题维度上明显提升。"
        "不要重复原文句式，不要插入元评论。"
    )
    lines.append(
        "硬约束：必须逐条修复低分问题；必须与前后章节摘要、人物已知状态、道具来源、地点空间关系保持一致；"
        "禁止临时新增未铺垫人物、道具、能力和设定；禁止让角色忘记上一章已经知道的事实；"
        "禁止用突然自曝、机械巧合、全知视角一次性解释来补洞；伏笔只能自然推进，不能过早讲完。"
    )
    lines.append("")
    lines.append(REVISE_CONTRACT_PROMPT)
    return "\n".join(lines)


def merge_revise_into_user_instruction(
    base_instruction: str | None,
    revise_instruction: str,
) -> str:
    """Compose the next writer-pass user_instruction.

    Deterministic ordering: original instruction first (unchanged), then a
    blank line, then the revise block. Original may be empty.
    """
    base = (base_instruction or "").strip()
    revise = revise_instruction.strip()
    if not base:
        return revise
    return f"{base}\n\n{revise}"
