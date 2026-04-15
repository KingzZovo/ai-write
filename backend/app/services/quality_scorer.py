"""
Novel Quality Scorer

Evaluates the quality of imported reference novels before using them
for style learning. Prevents learning from low-quality content.

Scoring dimensions:
- Writing quality (语言表达水平)
- Plot coherence (情节连贯性)
- Character depth (人物刻画深度)
- Narrative technique (叙事技巧)
- Overall readability (整体可读性)

Each dimension scores 0-10. Total quality score is the weighted average.
Books below threshold (default 6.0) are flagged as unsuitable for style learning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

QUALITY_THRESHOLD = 6.0

QUALITY_EVAL_PROMPT = """你是一位专业的文学评论编辑。请评估以下小说节选的写作质量。

评分维度（每项 0-10 分）：
1. writing_quality（语言表达）：用词准确性、句式多样性、语法规范
2. plot_coherence（情节连贯）：逻辑自洽、因果合理、节奏把控
3. character_depth（人物刻画）：性格鲜明度、对话区分度、行为动机
4. narrative_technique（叙事技巧）：视角运用、悬念设置、场景描写
5. readability（可读性）：流畅度、吸引力、整体阅读体验

输出纯 JSON 格式：
{
  "writing_quality": 分数,
  "plot_coherence": 分数,
  "character_depth": 分数,
  "narrative_technique": 分数,
  "readability": 分数,
  "overall": 加权平均分,
  "verdict": "high_quality" 或 "acceptable" 或 "low_quality",
  "brief_comment": "一句话评价"
}

以下是需要评估的小说节选（取自不同章节的多个片段）：
"""


@dataclass
class QualityScore:
    writing_quality: float = 0.0
    plot_coherence: float = 0.0
    character_depth: float = 0.0
    narrative_technique: float = 0.0
    readability: float = 0.0
    overall: float = 0.0
    verdict: str = "low_quality"
    brief_comment: str = ""
    is_suitable_for_learning: bool = False

    def to_dict(self) -> dict:
        return {
            "writing_quality": self.writing_quality,
            "plot_coherence": self.plot_coherence,
            "character_depth": self.character_depth,
            "narrative_technique": self.narrative_technique,
            "readability": self.readability,
            "overall": self.overall,
            "verdict": self.verdict,
            "brief_comment": self.brief_comment,
            "is_suitable_for_learning": self.is_suitable_for_learning,
        }


class QualityScorer:
    """Evaluates novel quality using LLM-as-a-judge."""

    def __init__(self, threshold: float = QUALITY_THRESHOLD):
        self.router = get_model_router()
        self.threshold = threshold

    async def score(self, text_samples: list[str]) -> QualityScore:
        """
        Score a novel's quality from sampled text blocks.

        Args:
            text_samples: 3-5 text blocks sampled from different parts of the novel

        Returns:
            QualityScore with dimensional scores and overall verdict
        """
        combined = "\n\n---\n\n".join(text_samples[:5])

        result = await self.router.generate(
            task_type="evaluation",
            messages=[
                {"role": "system", "content": "你是文学评论专家，只输出 JSON。"},
                {"role": "user", "content": QUALITY_EVAL_PROMPT + combined},
            ],
            max_tokens=512,
        )

        try:
            data = _parse_json(result.text)
            score = QualityScore(
                writing_quality=float(data.get("writing_quality", 0)),
                plot_coherence=float(data.get("plot_coherence", 0)),
                character_depth=float(data.get("character_depth", 0)),
                narrative_technique=float(data.get("narrative_technique", 0)),
                readability=float(data.get("readability", 0)),
                overall=float(data.get("overall", 0)),
                verdict=data.get("verdict", "low_quality"),
                brief_comment=data.get("brief_comment", ""),
            )
            score.is_suitable_for_learning = score.overall >= self.threshold
            return score
        except Exception as e:
            logger.warning("Failed to parse quality score: %s", e)
            return QualityScore(brief_comment=f"Evaluation failed: {e}")

    async def score_and_filter(
        self,
        text_samples: list[str],
    ) -> tuple[QualityScore, bool]:
        """
        Score and determine if the novel is suitable for style learning.

        Returns:
            Tuple of (QualityScore, is_suitable)
        """
        score = await self.score(text_samples)
        return score, score.is_suitable_for_learning


def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return json.loads(cleaned)
