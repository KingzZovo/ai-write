"""v1.5.0 C2 — auto-revise helpers for chapter generation.

Given a ChapterEvaluator result (overall + 5 dim scores + issues), decide
whether to revise (low score or located factual defect) and convert the issues list into a
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
  are returned to the caller for review; a failed targeted pass does not
  authorize full-chapter regeneration.
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
import re
from typing import Any, Awaitable, Callable

from app.services.narrative_contract import (
    REVISE_CALIBRATION_PROMPT,
    REVISE_CONTRACT_PROMPT,
)
from app.services.narrative_quality_gates import (
    BLOCKING_CONTRACT_VIOLATION_TYPES,
    ALLOWED_REPAIR_ACTIONS,
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
            "以下诊断曾跨轮出现，须再次对照当前原文核实，不能仅因重复标签判错："
            + "、".join(repeated)
        )
    else:
        lines.append("先核对诊断与作者要求及原文，确认问题存在后再决定修法。")
    lines.append("生成前写作决策优先选以下五类：" + " / ".join(ALLOWED_REPAIR_ACTIONS) + "。")

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

    lines.append("\n【改写前事实核对】")
    lines.append("- 从完整原文核对人物、物件、消息在各场的实际状态；台账是辅助，不能把未发生的计划结果当事实。")
    lines.append("- 本章到期事件应落实，结尾结果按作者要求；不因通用模板而降级确认、拖延交付或新增损失。")
    lines.append("- 若涉及紧迫动作，再核对 action_budget 中的实际时间与动作；若涉及推理，再核对 inference_ledger 中的来源与结论，不强制每场编造危机或替代解释。")
    lines.append("- 只修经原文核实的问题；普通合作、自然省略和已有充分支撑的确认不需要增加阻挠。")
    return "\n".join(lines)



def should_revise(
    eval_obj: Any,
    threshold: float = DEFAULT_REVISE_THRESHOLD,
) -> bool:
    """Revise low-scoring drafts or reported, source-located factual defects.

    A high average must not hide an unresolved contradiction simply because
    the evaluator called it low severity. Aesthetic advice and unverified
    labels alone do not trigger extra model calls. Parse failures remain
    untrusted: preserve the draft instead of rewriting on a sentinel score.
    """
    if getattr(eval_obj, "parse_failed", False):
        return False
    if isinstance(eval_obj, dict) and eval_obj.get("parse_failed"):
        return False
    overall = _coerce_overall(eval_obj)
    from app.services.chapter_acceptance import has_located_causal_issue
    return overall < float(threshold) or has_located_causal_issue(eval_obj)


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
        "未涉及的问题保留原句，不要插入元评论。"
    )
    lines.append(
        "硬约束：诊断必须先由原文核实；与已发生的正文事实、人物已知状态、道具来源及地点关系保持一致；后文只是衔接参考，不能提前变成当前事实；"
        "禁止临时新增未铺垫人物、道具、能力和设定；禁止让角色忘记上一章已经知道的事实；"
        "禁止用突然自曝、机械巧合、全知视角一次性解释来补洞；伏笔只能自然推进，不能过早讲完。"
    )
    lines.append("")
    lines.append(REVISE_CONTRACT_PROMPT)
    lines.append(REVISE_CALIBRATION_PROMPT)
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

# Hard cap on merged-span size (chars). Texts that use single-\n paragraph
# breaks collapse into one giant "paragraph" under the \n\n splitter, so a
# ±1-paragraph span can cover the whole scene; rewriting it would exceed
# SPAN_REWRITE_MAX_TOKENS (2000 中文字 ≈ 2000-3000 tokens) and splice a
# truncated rewrite back. Oversized spans skip targeted revision instead.
SPAN_REWRITE_MAX_CHARS: int = 2000
SPAN_REWRITE_MAX_INPUT_CHARS: int = 24000

SPAN_REWRITE_SYSTEM_PROMPT = (
    "你是小说定点修订编辑。只重写【待修订区间原文】给出的正文区间，逐条修复列出的问题；"
    "保持与上文、下文自然衔接，保持人物、情节、时间线与原文一致；"
    "修订建议描述的是要解决的问题，不是可粘贴进正文的句子。用人物的动作、回应和所见呈现修复后的场面，保持原叙述口吻，不把小说改成工作安排或完成情况说明。"
    "字数变化控制在 ±20% 以内。"
    "只输出替换后的区间文本，不要输出解释、标题、JSON、引号包裹或区间以外的内容。"
)


def targeted_revision_enabled() -> bool:
    """Env switch for the targeted revision path (default on)."""
    return os.getenv("TARGETED_REVISION_ENABLED", "1") != "0"


def locate_revision_span(text: str, quote: str) -> tuple[int, int] | None:
    """Locate a unique verbatim quote, including every paragraph it spans.

    Widen by one nonempty paragraph on each side. Single newlines and CRLF
    are paragraph boundaries too; offsets always refer to the original text.
    Ambiguous quotations are not safe edit anchors, even with a model's
    guessed paragraph number.
    """
    if not isinstance(quote, str) or not text:
        return None
    quote = quote.strip()
    if not quote:
        return None
    idx = text.find(quote)
    if idx < 0 or text.find(quote, idx + 1) >= 0:
        return None
    quote_end = idx + len(quote)
    paras = [(m.start(), m.end()) for m in re.finditer(r"[^\r\n]+", text) if m.group().strip()]
    hits = [i for i, (start, end) in enumerate(paras) if start < quote_end and end > idx]
    if not hits:
        return None
    start = paras[max(0, hits[0] - 1)][0]
    end = paras[min(len(paras) - 1, hits[-1] + 1)][1]
    return (start, end)


def splice_revision(text: str, span: tuple[int, int], revised: str) -> str:
    """Replace text[start:end] with `revised`, leaving the rest untouched."""
    start, end = span
    return text[:start] + revised + text[end:]


# Same shape as chapter_summarizer's fence stripper: outer ``` fence with an
# optional language tag on the opening line.
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?```$", re.DOTALL)


def _strip_code_fence(s: str) -> str:
    """Strip one outer markdown code fence (incl. language tag) + whitespace."""
    s = (s or "").strip()
    m = _CODE_FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    return s


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


@dataclass(frozen=True)
class RevisionContext:
    user_instruction: str = ""
    chapter_outline: dict | None = None
    previous_context: str = ""
    following_context: str = ""
    style_profile: str = ""
    cognition_ledger: str = ""


def build_span_rewrite_prompt(
    span_text: str,
    issues: list[dict],
    *,
    before: str = "",
    after: str = "",
    context: RevisionContext | None = None,
) -> str:
    """Build the user prompt for one merged-span rewrite call. Pure."""
    lines: list[str] = []
    if context is not None:
        sections = (
            ("作者要求", context.user_instruction),
            ("本章细纲（计划不等于已经发生）", json.dumps(context.chapter_outline, ensure_ascii=False) if context.chapter_outline else ""),
            ("前章事实与承接", context.previous_context),
            ("后章只读参照（不提前兑现）", context.following_context),
            ("风格参考（服从作者要求与本场人物）", context.style_profile),
            ("人物认知记录（不得越过当前知情范围）", context.cognition_ledger),
        )
        for label, value in sections:
            if value:
                lines.extend([f"【{label}】", value, ""])
    lines.append("评审意见是待核查诊断。先对照作者要求、既有事实和原文；不执行与这些材料冲突的建议，不添加无依据的关系、经历或机制。无法可靠修复时原样返回待修订区间，不编造补丁。")
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
        "只重写【待修订区间原文】这一区间，修复经材料核实的问题，"
        "保持与上下文衔接，字数变化 ±20% 内，输出仅替换文本。"
    )
    return "\n".join(lines)


@dataclass
class SpanRevisionResult:
    """Outcome of revise_spans.

    spans_revised == 0 means nothing was spliced; preserve the candidate for
    review. unlocatable_issues lists missing, ambiguous, unverified or oversized
    anchors. This result does not authorize widening into a full rewrite.
    """

    text: str
    spans_revised: int = 0
    unlocatable_issues: list[dict] = field(default_factory=list)


async def _router_span_llm_call(prompt: str, *, project_id=None, chapter_id=None) -> str:
    """Use the same registered rewrite route as manual and quality-gate edits."""
    from app.services.prompt_registry import run_text_prompt

    result = await run_text_prompt(
        task_type="rewrite", user_content=prompt, db=None,
        extra_system=SPAN_REWRITE_SYSTEM_PROMPT,
        project_id=project_id, chapter_id=chapter_id,
        temperature=0.4, max_tokens=SPAN_REWRITE_MAX_TOKENS,
    )
    reason = getattr(result, "finish_reason", None)
    if reason is not None and reason not in {"stop", "end_turn", "stop_sequence"}:
        # Do not splice a known incomplete/filtered/tool response into prose.
        # Keep the diagnostic bounded: neither response text nor relay details.
        raise ValueError("targeted_revision_incomplete_response")
    return result.text or ""


async def revise_spans(
    text: str,
    issues: list[dict],
    llm_call: Callable[[str], Awaitable[str]] | None = None,
    *, project_id=None, chapter_id=None,
    context: RevisionContext | None = None,
) -> SpanRevisionResult:
    """Targeted revision: rewrite only the spans located by issue quotes.

    - Issues with a verbatim-locatable `quote` are grouped per merged span
      (paragraph ±1, overlapping/adjacent spans merged) — one LLM call per
      merged span, spliced back in reverse order so offsets stay valid.
    - Merged spans longer than SPAN_REWRITE_MAX_CHARS skip targeted revision
      (a rewrite would overflow the token budget and splice back truncated
      output); their issues join `unlocatable_issues` for review.
    - Degenerate rewrites (empty, much shorter, or wildly longer than the
      original span; code fences are stripped first) are rejected and leave
      the original span untouched.
    - Issues without a reliable anchor are returned for review; an unapplied
      targeted pass must not automatically widen to a full rewrite.
    """
    if llm_call is None:
        async def llm_call(prompt):
            return await _router_span_llm_call(prompt, project_id=project_id, chapter_id=chapter_id)

    locatable: list[tuple[tuple[int, int], dict]] = []
    unlocatable: list[dict] = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        quote = issue.get("quote")
        span = locate_revision_span(text, quote) if issue.get("quote_verified") is not False else None
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
        if len(span_text) > SPAN_REWRITE_MAX_CHARS:
            # A genuinely oversized paragraph/span can overflow the token
            # budget and splice truncated output back. Route these issues to
            # review instead of expanding the rewrite scope.
            unlocatable.extend(related)
            continue
        prompt = build_span_rewrite_prompt(
            span_text,
            related,
            before=text[:m_start],
            after=text[m_end:],
            context=context,
        )
        if len(prompt) > SPAN_REWRITE_MAX_INPUT_CHARS:
            unlocatable.extend(related)
            continue
        revised = _strip_code_fence((await llm_call(prompt)) or "")
        if (
            not revised
            or revised == span_text.strip()
            # Shorter than 1/3 of the original span is a degenerate reply
            # (e.g. "好。") — the system prompt demands ±20% length.
            or len(revised) < len(span_text) // 3
            or len(revised) > max(len(span_text) * 3, len(span_text) + 400)
        ):
            continue
        out = splice_revision(out, (m_start, m_end), revised)
        revised_count += 1

    return SpanRevisionResult(text=out, spans_revised=revised_count, unlocatable_issues=unlocatable)
