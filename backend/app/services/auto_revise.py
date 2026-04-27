"""v1.5.0 C2 — auto-revise helpers for chapter generation.

Given a ChapterEvaluator result (overall + 5 dim scores + issues), decide
whether to revise (overall < threshold) and convert the issues list into a
Chinese revise instruction that can be appended to the next writer pass
(SceneOrchestrator.orchestrate_chapter_stream's user_instruction).

Design notes:
- Threshold defaults to 7.0 (B1' baseline overall ≈ 7.98 — anything below
  7.0 is a clear quality regression worth retrying).
- Maximum revise rounds defaults to 2 to bound LLM cost (3 total writes).
- We cap issues per dimension to 5 to avoid prompt explosion when the LLM
  evaluator returns a long flat list.
- This module has zero DB / LLM / IO side effects — pure helpers, easy to
  unit-test deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tunable defaults; overridable via GenerateChapterRequest fields.
DEFAULT_REVISE_THRESHOLD: float = 7.0
DEFAULT_MAX_REVISE_ROUNDS: int = 2
MAX_ISSUES_PER_DIMENSION: int = 5

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


def should_revise(
    eval_obj: Any,
    threshold: float = DEFAULT_REVISE_THRESHOLD,
) -> bool:
    """True iff overall score is below threshold (strictly less).

    Returns False when overall is exactly at the threshold ("no worse than
    the bar, accept"). NaN / None overall is treated as 0 (revise).
    """
    overall = _coerce_overall(eval_obj)
    return overall < float(threshold)


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
