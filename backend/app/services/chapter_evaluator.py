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

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

EVALUATION_SYSTEM_PROMPT = """\
你是一位专业的小说质量评审专家。你将对一段章节文本进行多维度评估。

请严格按照以下JSON格式输出评估结果，不要添加任何其他文字：

{
  "plot_coherence": {
    "score": <0-10的浮点数>,
    "issues": [
      {
        "paragraph": <段落编号，从1开始>,
        "description": "<问题描述>",
        "suggestion": "<改进建议>"
      }
    ]
  },
  "character_consistency": {
    "score": <0-10的浮点数>,
    "issues": [...]
  },
  "style_adherence": {
    "score": <0-10的浮点数>,
    "issues": [...]
  },
  "narrative_pacing": {
    "score": <0-10的浮点数>,
    "issues": [...]
  },
  "foreshadow_handling": {
    "score": <0-10的浮点数>,
    "issues": [...]
  }
}

评分标准：
- 0-3: 严重问题，需要重写
- 4-5: 明显问题，需要大幅修改
- 6-7: 有一些问题，需要小幅修改
- 8-9: 质量良好，仅有细微问题
- 10: 完美，无需修改

评估维度说明：
1. plot_coherence (剧情连贯性): 检查情节是否与大纲一致、是否有逻辑矛盾、转折是否合理
2. character_consistency (角色一致性): 检查角色行为是否符合人设、对话是否符合角色性格
3. style_adherence (风格贴合度): 检查写作风格是否与目标风格一致、用词是否恰当
4. narrative_pacing (叙事节奏): 检查叙事节奏是否合适、详略是否得当、高潮/低谷安排
5. foreshadow_handling (伏笔处理): 检查伏笔的埋设和回收是否自然、是否遗漏应有的伏笔
"""


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
    parts.append(chapter_text)

    if chapter_outline:
        parts.append("\n\n## 本章大纲\n")
        parts.append(json.dumps(chapter_outline, ensure_ascii=False, indent=2))

    if previous_summary:
        parts.append("\n\n## 前文摘要\n")
        parts.append(previous_summary)

    if style_profile:
        parts.append("\n\n## 目标风格描述\n")
        parts.append(style_profile)

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
        self._router = get_model_router()

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
            result = await self._router.generate(
                task_type="evaluation",
                messages=messages,
                temperature=0.3,
                max_tokens=4096,
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
