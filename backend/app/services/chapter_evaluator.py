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
from dataclasses import dataclass, field

from app.services.model_router import get_model_router_async
from app.services.narrative_contract import EVALUATOR_CONTRACT_PROMPT

logger = logging.getLogger(__name__)

EVALUATION_SYSTEM_PROMPT = """\
你是小说章节质量评审。只输出合法 JSON，不要输出解释、Markdown 或正文摘录。
按 5 个维度评分，每项 0-10：plot_coherence、character_consistency、style_adherence、narrative_pacing、foreshadow_handling。
issues 只列关键问题，最多 12 条；每条只写元数据和简短诊断，禁止引用/复述原文章句子。
JSON 格式必须是：
{
  "plot_coherence": {"score": 0, "issues": [{"paragraph": 0, "description": "", "suggestion": "", "violation_type": "", "severity": ""}]},
  "character_consistency": {"score": 0, "issues": []},
  "style_adherence": {"score": 0, "issues": []},
  "narrative_pacing": {"score": 0, "issues": []},
  "foreshadow_handling": {"score": 0, "issues": []}
}
""" + EVALUATOR_CONTRACT_PROMPT


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
        parts.append("\n\n## 目标风格描述\n")
        parts.append(_limit_text(style_profile, 1200))

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

    data: dict = json.loads(text)

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
    ) -> EvaluationResult:
        """
        Evaluate a chapter using task_type='evaluation'.

        Args:
            chapter_text: The full text of the chapter to evaluate.
            chapter_outline: The outline/plan for this chapter.
            previous_summary: Summary of preceding chapters for context.
            style_profile: Description of the target writing style.
            active_foreshadows: List of currently active foreshadow descriptions.

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
            return EvaluationResult(
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
            return EvaluationResult(
                issues=[
                    {
                        "dimension": "system",
                        "location": 0,
                        "description": f"Evaluation error: {exc}",
                        "suggestion": "Check model configuration and retry",
                    }
                ]
            )
