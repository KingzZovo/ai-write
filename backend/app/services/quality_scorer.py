"""
Novel Quality Scorer — 10-Dimension Evaluation

Evaluates reference novels across 10 dimensions covering both literary
quality and web novel-specific metrics (hook power, pacing, anti-AI).

Each dimension scores 0-10. Books below threshold (6.0) are flagged
as unsuitable for style learning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

QUALITY_THRESHOLD = 6.0

QUALITY_EVAL_PROMPT = """你是一位资深的网络小说评论编辑。请从以下10个维度评估小说节选的写作质量。

评分维度（每项 0-10 分）：
1. writing_quality（语言表达）：用词准确性、句式多样性、语法规范、文笔流畅度
2. plot_coherence（情节连贯）：逻辑自洽、因果合理、剧情推进合理性
3. character_depth（人物刻画）：性格鲜明度、行为动机、成长弧线
4. narrative_technique（叙事技巧）：视角运用、场景描写、展示而非讲述
5. readability（可读性）：流畅度、吸引力、整体阅读体验
6. hook_power（追读力）：章末悬念、情节牵引力、让人想继续读的冲动
7. immersion（沉浸感）：五感描写、代入感、环境氛围营造
8. dialogue_quality（对话质量）：目的性、角色区分度、潜台词层次
9. pacing（节奏控制）：松紧交替、高潮安排、信息密度合理性
10. anti_ai_score（自然度）：是否有AI痕迹词(璀璨/油然而生等)、句式是否自然多变

输出纯 JSON 格式：
{
  "writing_quality": 分数,
  "plot_coherence": 分数,
  "character_depth": 分数,
  "narrative_technique": 分数,
  "readability": 分数,
  "hook_power": 分数,
  "immersion": 分数,
  "dialogue_quality": 分数,
  "pacing": 分数,
  "anti_ai_score": 分数,
  "overall": 加权平均分,
  "verdict": "high_quality" 或 "acceptable" 或 "low_quality",
  "brief_comment": "一句话总评"
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
    hook_power: float = 0.0
    immersion: float = 0.0
    dialogue_quality: float = 0.0
    pacing: float = 0.0
    anti_ai_score: float = 0.0
    overall: float = 0.0
    verdict: str = "low_quality"
    brief_comment: str = ""
    is_suitable_for_learning: bool = False

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class QualityScorer:
    """Evaluates novel quality using LLM-as-a-judge with 10 dimensions."""

    def __init__(self, threshold: float = QUALITY_THRESHOLD):
        self.router = get_model_router()
        self.threshold = threshold

    async def score(self, text_samples: list[str]) -> QualityScore:
        combined = "\n\n---\n\n".join(text_samples[:5])

        result = await self.router.generate(
            task_type="evaluation",
            messages=[
                {"role": "system", "content": "你是资深网络小说评论编辑，只输出 JSON。"},
                {"role": "user", "content": QUALITY_EVAL_PROMPT + combined},
            ],
            max_tokens=800,
        )

        try:
            data = _parse_json(result.text)
            score = QualityScore(
                writing_quality=float(data.get("writing_quality", 0)),
                plot_coherence=float(data.get("plot_coherence", 0)),
                character_depth=float(data.get("character_depth", 0)),
                narrative_technique=float(data.get("narrative_technique", 0)),
                readability=float(data.get("readability", 0)),
                hook_power=float(data.get("hook_power", 0)),
                immersion=float(data.get("immersion", 0)),
                dialogue_quality=float(data.get("dialogue_quality", 0)),
                pacing=float(data.get("pacing", 0)),
                anti_ai_score=float(data.get("anti_ai_score", 0)),
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
