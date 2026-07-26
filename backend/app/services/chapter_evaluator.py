"""
Chapter Quality Evaluator

Uses an independent LLM (different from the generation model) to evaluate
generated chapters across 5 dimensions:
1. plot_coherence (剧情连贯性) - 0-10
2. character_consistency (角色一致性) - 0-10
3. style_adherence (风格贴合度) - 0-10
4. narrative_pacing (叙事节奏) - 0-10
5. foreshadow_handling (伏笔处理) - 0-10

Each dimension gets a score, specific issue locations, and improvement suggestions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.services.model_router import get_model_router_async
from app.services.narrative_contract import (
    EVALUATOR_CALIBRATION_PROMPT,
    EVALUATOR_CONTRACT_PROMPT,
)
from app.services.style_runtime import split_style_layers

logger = logging.getLogger(__name__)

# B2 hotfix: bare control characters to strip on the JSON repair retry.
# Keeps \t (\x09), \n (\x0a) and \r (\x0d) -- those are legitimate inside
# string values once strict=False is in effect.
_BARE_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

EVALUATION_SYSTEM_PROMPT = """\
你是小说章节质量评审。只输出合法 JSON，不要输出解释、Markdown 或正文摘录。
按 5 个维度评分，每项 0-10：plot_coherence、character_consistency、style_adherence、narrative_pacing、foreshadow_handling。
issues 只列关键问题，最多 12 条；每条只写元数据和简短诊断；除 quote 字段外禁止引用/复述原文章句子。
quote 必须是从原文逐字摘取的 10-40 字连续片段，用于定位问题位置；无法逐字摘取时留空字符串。
JSON 格式必须是：
{
  "plot_coherence": {"score": 0, "issues": [{"paragraph": 0, "description": "", "suggestion": "", "violation_type": "", "severity": "", "quote": ""}]},
  "character_consistency": {"score": 0, "issues": []},
  "style_adherence": {"score": 0, "issues": []},
  "narrative_pacing": {"score": 0, "issues": []},
  "foreshadow_handling": {"score": 0, "issues": []}
}
""" + EVALUATOR_CONTRACT_PROMPT + EVALUATOR_CALIBRATION_PROMPT


def _limit_text(text: str, max_chars: int) -> str:
    if not text or max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n...[已截断，保留首尾用于评分]...\n" + text[-tail:]

def _build_user_prompt(
    chapter_text: str,
    chapter_outline: dict,
    previous_summary: str,
    style_profile: str,
    active_foreshadows: list[str] | None,
    cognition_ledger_text: str = "",
    style_stats_text: str = "",
) -> str:
    """Build the user prompt with all context for evaluation."""
    parts: list[str] = []

    parts.append("## 待评估章节内容\n")
    parts.append(_limit_text(chapter_text, 9000))

    if chapter_outline:
        parts.append("\n\n## 本章大纲\n")
        parts.append(_limit_text(json.dumps(chapter_outline, ensure_ascii=False, indent=2), 2500))

    if previous_summary:
        parts.append("\n\n## 前文摘要\n")
        parts.append(_limit_text(previous_summary, 1200))

    if style_profile:
        # Layered style injection: when the writer prompt carried a stacked
        # 基调层/修正层 block, score style_adherence against both layers —
        # base = "像不像这个基调", override = "这些规则是否被遵守". A plain
        # single-layer style text keeps the original section unchanged.
        base_layer, override_layer = split_style_layers(style_profile)
        if override_layer:
            if base_layer:
                parts.append("\n\n## 目标风格·基调层\n")
                parts.append("style_adherence 先评估整体读感像不像下面这个基调：\n")
                parts.append(_limit_text(base_layer, 1200))
            parts.append("\n\n## 目标风格·修正层（优先级高于基调层）\n")
            parts.append("再逐条核查以下作者本人规则是否被遵守；违反修正层规则的扣分重于偏离基调：\n")
            parts.append(_limit_text(override_layer, 1200))
        else:
            parts.append("\n\n## 目标风格描述\n")
            parts.append(_limit_text(style_profile, 1200))

    if cognition_ledger_text:
        parts.append("\n\n## 当前认知账本\n")
        parts.append(
            "按下列账本核查 cognition_violation：角色不得说破/利用其「不知道」"
            "列表中的信息（除非本章写明获知路径）；不要无故抹平「读者已知-角色未知」的信息差。\n"
        )
        parts.append(_limit_text(cognition_ledger_text, 1200))

    # C2/F1: whole-book style statistics (deterministic numbers; the LLM
    # decides whether this chapter over-reuses book-level tics).
    if style_stats_text:
        parts.append("\n\n")
        parts.append(_limit_text(style_stats_text, 600))

    if active_foreshadows:
        parts.append("\n\n## 当前活跃伏笔\n")
        for i, f in enumerate(active_foreshadows, 1):
            parts.append(f"{i}. {f}")

    parts.append("\n\n请对以上章节进行全面评估，输出JSON格式的评估结果。")

    return "\n".join(parts)


@dataclass
class EvaluationResult:
    """Result of a chapter quality evaluation across 5 dimensions."""

    plot_coherence: float = 0.0
    character_consistency: float = 0.0
    style_adherence: float = 0.0
    narrative_pacing: float = 0.0
    foreshadow_handling: float = 0.0
    overall: float = 0.0
    issues: list[dict] = field(default_factory=list)
    # B2 hotfix: True when the LLM response could not be parsed at all, so
    # the all-zero scores are sentinels rather than a real verdict. Callers
    # (auto_revise.should_revise) must not treat such results as "bad
    # chapter" -- a fake overall=0 previously forced a full-chapter rewrite.
    parse_failed: bool = False

    def to_dict(self) -> dict:
        """Convert to a serializable dictionary."""
        return {
            "plot_coherence": self.plot_coherence,
            "character_consistency": self.character_consistency,
            "style_adherence": self.style_adherence,
            "narrative_pacing": self.narrative_pacing,
            "foreshadow_handling": self.foreshadow_handling,
            "overall": self.overall,
            "issues": self.issues,
            "parse_failed": self.parse_failed,
        }


def _parse_evaluation_response(raw_text: str) -> EvaluationResult:
    """Parse the LLM JSON response into an EvaluationResult."""
    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[: -3]
    text = text.strip()

    # B2 hotfix: evaluation LLMs sometimes emit RAW control characters inside
    # JSON string values (typically a literal newline in an issue `quote`
    # excerpt). json.loads defaults to strict=True and rejects those, which
    # previously zeroed the whole evaluation and triggered a wasted
    # full-chapter rewrite round. strict=False accepts control chars inside
    # string values -- exactly this failure shape.
    try:
        data: dict = json.loads(text, strict=False)
    except json.JSONDecodeError:
        # Minimal repair retry: slice from the first '{' to the last '}'
        # (drops LLM prose/chatter around the JSON object) and drop bare
        # control chars (keeping \n \t \r, which strict=False already
        # tolerates), then try once more. Anything still unparseable
        # propagates to evaluate()'s except branch, which marks the result
        # parse_failed.
        cleaned = text
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
        cleaned = _BARE_CONTROL_CHARS_RE.sub("", cleaned)
        data = json.loads(cleaned, strict=False)

    dimensions = [
        "plot_coherence",
        "character_consistency",
        "style_adherence",
        "narrative_pacing",
        "foreshadow_handling",
    ]

    scores: dict[str, float] = {}
    all_issues: list[dict] = []

    for dim in dimensions:
        dim_data = data.get(dim, {})
        score = float(dim_data.get("score", 0))
        # Clamp to [0, 10]
        score = max(0.0, min(10.0, score))
        scores[dim] = score

        for issue in dim_data.get("issues", []):
            all_issues.append(
                {
                    "dimension": dim,
                    "location": issue.get("paragraph", 0),
                    "description": issue.get("description", ""),
                    "suggestion": issue.get("suggestion", ""),
                    "violation_type": issue.get("violation_type", ""),
                    "severity": issue.get("severity", ""),
                    # Q2 targeted revision: verbatim 10-40 char locator snippet.
                    "quote": issue.get("quote", "") or "",
                }
            )

    for issue in data.get("contract_violations", []) or []:
        if not isinstance(issue, dict):
            continue
        vtype = issue.get("violation_type") or issue.get("type") or "contract_violation"
        all_issues.append(
            {
                "dimension": issue.get("dimension", "plot_coherence"),
                "location": issue.get("paragraph", issue.get("location", 0)),
                "description": f"[{vtype}] {issue.get('description') or issue.get('why') or issue.get('violated_rule') or ''}",
                "suggestion": issue.get("suggestion") or issue.get("required_fix") or issue.get("downgrade_if_unfixable") or "按世界逻辑合同补足支撑或降低结果强度",
                "violation_type": vtype,
                "severity": issue.get("severity", ""),
                "quote": issue.get("quote", "") or "",
            }
        )

    overall = sum(scores.values()) / len(dimensions) if dimensions else 0.0

    return EvaluationResult(
        plot_coherence=scores.get("plot_coherence", 0.0),
        character_consistency=scores.get("character_consistency", 0.0),
        style_adherence=scores.get("style_adherence", 0.0),
        narrative_pacing=scores.get("narrative_pacing", 0.0),
        foreshadow_handling=scores.get("foreshadow_handling", 0.0),
        overall=round(overall, 2),
        issues=all_issues,
    )


class ChapterEvaluator:
    """Evaluates chapter quality using an independent LLM judge."""

    def __init__(self) -> None:
        # Router is resolved lazily inside evaluate() via get_model_router_async
        # so DB-loaded providers are guaranteed (sync get_model_router() inside
        # an async handler returns an unloaded singleton -> 'No model configured').
        pass

    async def evaluate(
        self,
        chapter_text: str,
        chapter_outline: dict,
        previous_summary: str = "",
        style_profile: str = "",
        active_foreshadows: list[str] | None = None,
        cognition_ledger_text: str = "",
        style_stats_text: str = "",
    ) -> EvaluationResult:
        """
        Evaluate a chapter using task_type='evaluation'.

        Args:
            chapter_text: The full text of the chapter to evaluate.
            chapter_outline: The outline/plan for this chapter.
            previous_summary: Summary of preceding chapters for context.
            style_profile: Description of the target writing style.
            active_foreshadows: List of currently active foreshadow descriptions.
            cognition_ledger_text: Serialized character cognition ledger
                (who knows what / reader-only facts) for cognition_violation checks.
            style_stats_text: Whole-book style statistics block (C2/F1) for the
                LLM to judge book-level tic over-reuse.

        Returns:
            EvaluationResult with scores across 5 dimensions and specific issues.
        """
        if not chapter_text or not chapter_text.strip():
            logger.warning("Empty chapter text provided for evaluation")
            return EvaluationResult()

        user_prompt = _build_user_prompt(
            chapter_text=chapter_text,
            chapter_outline=chapter_outline,
            previous_summary=previous_summary,
            style_profile=style_profile,
            active_foreshadows=active_foreshadows,
            cognition_ledger_text=cognition_ledger_text,
            style_stats_text=style_stats_text,
        )

        messages = [
            {"role": "system", "content": EVALUATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            router = await get_model_router_async()
            result = await router.generate_with_tier_fallback(
                task_type="evaluation",
                messages=messages,
                temperature=0.3,
                max_tokens=2400,
                _log_meta={"caller": "chapter_evaluator.evaluate"},
            )

            evaluation = _parse_evaluation_response(result.text)
            logger.info(
                "Chapter evaluation complete: overall=%.2f (tokens=%d)",
                evaluation.overall,
                result.usage.total_tokens,
            )
            return evaluation

        except json.JSONDecodeError as exc:
            logger.error("Failed to parse evaluation response as JSON: %s", exc)
            # parse_failed marks the zero scores as untrusted sentinels:
            # should_revise() skips revision instead of treating overall=0
            # as a genuinely terrible chapter.
            return EvaluationResult(
                parse_failed=True,
                issues=[
                    {
                        "dimension": "system",
                        "location": 0,
                        "description": f"Evaluation response parsing failed: {exc}",
                        "suggestion": "Retry evaluation",
                    }
                ]
            )
        except Exception as exc:
            logger.error("Chapter evaluation failed: %s", exc, exc_info=True)
            # parse_failed marks the zero scores as untrusted: this branch also
            # catches non-dict JSON (AttributeError), bare-fence ValueError, and
            # router/network errors. Without the flag, overall=0 would pass
            # should_revise() and trigger a wasted full-chapter rewrite -- the
            # exact bug B2 fixed for the JSONDecodeError path.
            return EvaluationResult(
                parse_failed=True,
                issues=[
                    {
                        "dimension": "system",
                        "location": 0,
                        "description": f"Evaluation error: {exc}",
                        "suggestion": "Check model configuration and retry",
                    }
                ]
            )
