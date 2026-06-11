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
- The original helpers have zero DB / LLM / IO side effects — pure functions,
  easy to unit-test deterministically. The only exception is the Q2 targeted
  revision entry `revise_spans`, which performs LLM calls through an injected
  (or lazily resolved) `llm_call` coroutine; module import stays side-effect
  free.

Q2 targeted revision (QMAI rewriteTarget):
  Evaluator issues now carry a verbatim `quote` snippet (10-40 chars). When a
  quote can be located in the chapter text, we rewrite only the containing
  paragraph ±1 (merged across overlapping issues) and splice the result back,
  instead of regenerating the whole chapter. Issues without a locatable quote
  are returned to the caller, which keeps full-chapter regeneration as the
  fallback path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
import re
from typing import Any, Awaitable, Callable

from app.services.narrative_contract import REVISE_CONTRACT_PROMPT
from app.services.narrative_quality_gates import (
    BLOCKING_CONTRACT_VIOLATION_TYPES,
    QUALITY_GATE_RULES,
    blocking_contract_violations,
    blocking_contract_violation_set,
    coerce_issues as _gate_coerce_issues,
    group_issues_by_violation_type as _gate_group_issues_by_violation_type,
    issue_violation_type as _gate_issue_violation_type,
    repair_method_for,
    reusable_bucket_lines as _gate_reusable_bucket_lines,
)

# Tunable defaults; overridable via GenerateChapterRequest fields.
DEFAULT_REVISE_THRESHOLD: float = 8.2
DEFAULT_MAX_REVISE_ROUNDS: int = 3
MAX_ISSUES_PER_DIMENSION: int = 5
MAX_ISSUES_PER_VIOLATION_TYPE: int = 6

# Blocking violation taxonomy is centralized in narrative_quality_gates.

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
    return _gate_coerce_issues(eval_obj)


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
    return _gate_issue_violation_type(issue)


_VIOLATION_REPAIR_METHODS: dict[str, str] = {tag: repair_method_for(tag) for tag in QUALITY_GATE_RULES}
_VIOLATION_REPAIR_METHODS["untyped_issue"] = repair_method_for("untyped_issue")
# Reusable revise buckets are centralized in narrative_quality_gates.


def _group_issues_by_violation_type(issues: list[dict]) -> dict[str, list[dict]]:
    return _gate_group_issues_by_violation_type(issues)


def _reusable_bucket_lines(issues: list[dict]) -> list[str]:
    """Summarize repeated issue classes into actionable revise buckets."""
    return _gate_reusable_bucket_lines(issues)


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
    lines.append("【生成前内化诊断蓝图】")
    if repeated:
        lines.append(
            "以下结构风险曾跨轮重复出现；下一次生成前必须内化为写作约束，而不是写完后再靠流程纠错："
            + "、".join(repeated)
        )
    else:
        lines.append("先按违规类型确定生成前约束，再写正文；目标是第一稿直接规避这些问题。")
    lines.append("生成前写作决策优先选以下五类：补支撑 / 降级结果 / 删除冲突设定 / 拆分人物或资源 / 提前埋伏笔。")

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
    lines.append("- 高压场面必须先列 action_budget：可用时间、身体姿态、双手限制、预置动作、最多动作数、代价；超预算时必须删动作、拆场或降级结果。")
    lines.append("- 关键判断必须先列 inference_ledger：感知来源/证据、可推出结论强度、替代解释、允许写法；弱证据不得升级成定案。")
    lines.append("- 复发硬门槛：别人直接说秘密、高资源证物低成本接触、疑似线索写成确认、追捕中长问答、对白百科、章末线索过准，必须在生成前改结构，不允许等生成后只补一句解释。")
    return "\n".join(lines)



def should_revise(
    eval_obj: Any,
    threshold: float = DEFAULT_REVISE_THRESHOLD,
) -> bool:
    """True iff score is below threshold.

    Issue tags remain useful diagnostics, but they should not force extra
    repair rounds once the chapter reaches the target score. The goal is to
    move those diagnostics into pre-generation constraints so the first draft
    is already compliant.
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
    lines.append(
        build_root_cause_repair_plan(eval_obj, max_per_type=max_per_dimension)
    )
    lines.append("")
    bucket_lines = _reusable_bucket_lines(issues)
    if bucket_lines:
        lines.append("【可复用问题归类与本轮修复优先级】")
        lines.extend(bucket_lines)
        lines.append(
            "执行顺序：先修空间/时间承接与证据强度，再修情报泄露和人物边界，最后处理语体与重复表达；"
            "每一类都要在正文中体现具体修复，而不是只加解释句。"
        )
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


# ---------------------------------------------------------------------------
# Q2 — targeted span revision (QMAI rewriteTarget)
# ---------------------------------------------------------------------------

# Spans rewrites are short (~3 paragraphs), so a modest token budget suffices.
SPAN_REWRITE_MAX_TOKENS: int = 2000

SPAN_REWRITE_SYSTEM_PROMPT = (
    "你是小说定点修订编辑。只重写【待修订区间原文】给出的正文区间，逐条修复列出的问题；"
    "保持与上文、下文自然衔接，保持人物、情节、时间线与原文一致；"
    "字数变化控制在 ±20% 以内。"
    "只输出替换后的区间文本，不要输出解释、标题、JSON、引号包裹或区间以外的内容。"
)


def targeted_revision_enabled() -> bool:
    """Env switch for the targeted revision path (default on)."""
    return os.getenv("TARGETED_REVISION_ENABLED", "1") != "0"


def locate_revision_span(text: str, quote: str) -> tuple[int, int] | None:
    """Find the paragraph containing `quote`, widened to ±1 paragraph.

    Returns (start, end) char offsets into `text`, or None if the quote does
    not occur verbatim.
    """
    if not quote or not text:
        return None
    idx = text.find(quote.strip())
    if idx < 0:
        return None
    paras: list[tuple[int, int]] = []
    cursor = 0
    for part in text.split("\n\n"):
        paras.append((cursor, cursor + len(part)))
        cursor += len(part) + 2
    hit = next((i for i, (s, e) in enumerate(paras) if s <= idx < e), None)
    if hit is None:
        return None
    start = paras[max(0, hit - 1)][0]
    end = paras[min(len(paras) - 1, hit + 1)][1]
    return (start, end)


def splice_revision(text: str, span: tuple[int, int], revised: str) -> str:
    """Replace text[start:end] with `revised`, leaving the rest untouched."""
    start, end = span
    return text[:start] + revised + text[end:]


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort spans and merge overlapping/adjacent ones into disjoint spans."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def build_span_rewrite_prompt(
    span_text: str,
    issues: list[dict],
    *,
    before: str = "",
    after: str = "",
) -> str:
    """Build the user prompt for one merged-span rewrite call. Pure."""
    lines: list[str] = []
    if before:
        lines.append("【上文（只读，不要重写）】")
        lines.append(before)
        lines.append("")
    lines.append("【待修订区间原文】")
    lines.append(span_text)
    lines.append("")
    if after:
        lines.append("【下文（只读，不要重写）】")
        lines.append(after)
        lines.append("")
    lines.append("【需要修复的问题】")
    for i, issue in enumerate(issues, 1):
        desc = (issue.get("description") or "").strip()
        sugg = (issue.get("suggestion") or "").strip()
        lines.append(f"{i}. {desc or '（无描述）'}")
        if sugg:
            lines.append(f"   修复建议：{sugg}")
    lines.append("")
    lines.append(
        "只重写【待修订区间原文】这一区间，修复以上全部问题，"
        "保持与上下文衔接，字数变化 ±20% 内，输出仅替换文本。"
    )
    return "\n".join(lines)


@dataclass
class SpanRevisionResult:
    """Outcome of revise_spans.

    spans_revised == 0 means nothing was spliced; the caller should fall back
    to full-chapter regeneration. unlocatable_issues lists issues whose quote
    was missing or not found verbatim — they ride along the fallback path.
    """

    text: str
    spans_revised: int = 0
    unlocatable_issues: list[dict] = field(default_factory=list)


async def _router_span_llm_call(prompt: str) -> str:
    """Default LLM callable: model_router task_type='rewrite' (lazy import)."""
    from app.services.model_router import get_model_router_async

    router = await get_model_router_async()
    result = await router.generate_with_tier_fallback(
        task_type="rewrite",
        messages=[
            {"role": "system", "content": SPAN_REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        max_tokens=SPAN_REWRITE_MAX_TOKENS,
        _log_meta={"caller": "auto_revise.revise_spans"},
    )
    return result.text or ""


async def revise_spans(
    text: str,
    issues: list[dict],
    llm_call: Callable[[str], Awaitable[str]] | None = None,
) -> SpanRevisionResult:
    """Targeted revision: rewrite only the spans located by issue quotes.

    - Issues with a verbatim-locatable `quote` are grouped per merged span
      (paragraph ±1, overlapping/adjacent spans merged) — one LLM call per
      merged span, spliced back in reverse order so offsets stay valid.
    - Degenerate rewrites (empty, or wildly longer than the original span)
      are rejected and leave the original span untouched.
    - Issues without a locatable quote are returned for the caller to decide
      on full-chapter fallback (spans_revised == 0 → fall back).
    """
    if llm_call is None:
        llm_call = _router_span_llm_call

    locatable: list[tuple[tuple[int, int], dict]] = []
    unlocatable: list[dict] = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        quote = str(issue.get("quote") or "").strip()
        span = locate_revision_span(text, quote) if quote else None
        if span is None:
            unlocatable.append(issue)
        else:
            locatable.append((span, issue))

    if not locatable:
        return SpanRevisionResult(text=text, spans_revised=0, unlocatable_issues=unlocatable)

    merged = merge_spans([span for span, _ in locatable])
    grouped: list[tuple[tuple[int, int], list[dict]]] = []
    for m_start, m_end in merged:
        related = [it for (s, e), it in locatable if s < m_end and e > m_start]
        grouped.append(((m_start, m_end), related))

    out = text
    revised_count = 0
    # Reverse order: merged spans are disjoint and sorted, so splicing from
    # the end keeps earlier offsets valid.
    for (m_start, m_end), related in sorted(grouped, key=lambda g: g[0][0], reverse=True):
        span_text = text[m_start:m_end]
        prompt = build_span_rewrite_prompt(
            span_text,
            related,
            before=text[max(0, m_start - 200):m_start].strip(),
            after=text[m_end:m_end + 200].strip(),
        )
        revised = ((await llm_call(prompt)) or "").strip()
        if not revised or len(revised) > max(len(span_text) * 3, len(span_text) + 400):
            continue
        out = splice_revision(out, (m_start, m_end), revised)
        revised_count += 1

    return SpanRevisionResult(text=out, spans_revised=revised_count, unlocatable_issues=unlocatable)
