"""
Feature Extraction Services

Extracts two types of features from text blocks:
1. Plot features: characters, events, summary, emotional tone, causal chains
2. Style features: sentence length stats, dialogue ratio, word frequency, rhetoric

Plot extraction uses LLM (lightweight model).
Style extraction uses jieba + statistical analysis + LLM.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

import jieba
import jieba.posseg as pseg

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PlotFeatures:
    """Extracted plot features from a text block."""
    characters: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    summary: str = ""
    emotional_tone: str = ""
    locations: list[str] = field(default_factory=list)
    causal_chains: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "characters": self.characters,
            "events": self.events,
            "summary": self.summary,
            "emotional_tone": self.emotional_tone,
            "locations": self.locations,
            "causal_chains": self.causal_chains,
        }


@dataclass
class StyleFeatures:
    """Extracted style features from a text block."""
    avg_sentence_length: float = 0.0
    sentence_length_variance: float = 0.0
    dialogue_ratio: float = 0.0   # proportion of dialogue vs narration
    narration_ratio: float = 0.0
    description_ratio: float = 0.0
    top_words: list[tuple[str, int]] = field(default_factory=list)
    rhetoric_frequency: dict[str, float] = field(default_factory=dict)
    paragraph_rhythm: str = ""  # e.g., "short-long-short"
    pov_type: str = ""  # first_person, third_person, omniscient

    def to_dict(self) -> dict:
        return {
            "avg_sentence_length": self.avg_sentence_length,
            "sentence_length_variance": self.sentence_length_variance,
            "dialogue_ratio": self.dialogue_ratio,
            "narration_ratio": self.narration_ratio,
            "description_ratio": self.description_ratio,
            "top_words": self.top_words,
            "rhetoric_frequency": self.rhetoric_frequency,
            "paragraph_rhythm": self.paragraph_rhythm,
            "pov_type": self.pov_type,
        }


# =============================================================================
# Plot Feature Extraction (LLM-based)
# =============================================================================


PLOT_EXTRACTION_PROMPT = """分析以下小说文本，提取剧情特征。输出纯 JSON 格式：

{
  "characters": ["出场人物名"],
  "events": ["关键事件描述"],
  "summary": "50字以内的段落摘要",
  "emotional_tone": "情绪基调(如：紧张、温馨、悲伤、激昂)",
  "locations": ["场景地点"],
  "causal_chains": ["因果关系，格式：因为A所以B"]
}

文本：
"""


class PlotExtractor:
    """Extracts plot features using a lightweight LLM."""

    def __init__(self):
        self.router = get_model_router()

    async def extract(self, text: str) -> PlotFeatures:
        """Extract plot features from a text block."""
        result = await self.router.generate(
            task_type="extraction",
            messages=[
                {"role": "system", "content": "你是一个文本分析助手，只输出 JSON。"},
                {"role": "user", "content": PLOT_EXTRACTION_PROMPT + text},
            ],
            max_tokens=1024,
        )

        try:
            data = _parse_json(result.text)
            return PlotFeatures(
                characters=data.get("characters", []),
                events=data.get("events", []),
                summary=data.get("summary", ""),
                emotional_tone=data.get("emotional_tone", ""),
                locations=data.get("locations", []),
                causal_chains=data.get("causal_chains", []),
            )
        except Exception as e:
            logger.warning("Failed to parse plot features: %s", e)
            return PlotFeatures(summary=text[:100])


# =============================================================================
# Style Feature Extraction (Statistical + jieba)
# =============================================================================


# Sentence-ending punctuation
SENTENCE_ENDINGS = re.compile(r"[。！？!?…]+")
# Dialogue markers
DIALOGUE_PATTERN = re.compile("[\u201c\u300c\u300e].*?[\u201d\u300d\u300f]|\u201c.*?\u201d")
# Description indicators (adjectives, scenery words)
DESCRIPTION_WORDS = {"\u7684", "\u5730", "\u5f97", "\u7740", "\u4e86", "\u8fc7", "\u822c", "\u4f3c", "\u5982"}
# Common rhetoric patterns
SIMILE_PATTERN = re.compile("(\u50cf|\u5982\u540c|\u4eff\u4f5b|\u597d\u50cf|\u5b9b\u5982|\u72b9\u5982|\u597d\u4f3c)")
PARALLEL_PATTERN = re.compile("(\uff0c.*?\uff0c.*?\uff0c)")
RHETORICAL_Q = re.compile("[\u96be\u9053\u5c82\u600e\u80fd\u4f55\u5fc5\u4f55\u66fe].*?[\uff1f?]")
# POV indicators
FIRST_PERSON = re.compile("[\u6211]")
THIRD_PERSON_HE = re.compile("[\u4ed6\u5979\u5b83]")


class StyleExtractor:
    """Extracts style features using statistical analysis and jieba."""

    def extract(self, text: str) -> StyleFeatures:
        """Extract style features from a text block."""
        features = StyleFeatures()

        sentences = [s.strip() for s in SENTENCE_ENDINGS.split(text) if s.strip()]
        if not sentences:
            return features

        # Sentence length statistics
        lengths = [len(s) for s in sentences]
        features.avg_sentence_length = sum(lengths) / len(lengths)
        mean = features.avg_sentence_length
        features.sentence_length_variance = sum((l - mean) ** 2 for l in lengths) / len(lengths)

        # Dialogue / narration / description ratios
        dialogue_chars = sum(len(m.group()) for m in DIALOGUE_PATTERN.finditer(text))
        total_chars = len(text) if text else 1
        features.dialogue_ratio = dialogue_chars / total_chars

        # Estimate description ratio by counting descriptive sentences
        desc_count = 0
        for sent in sentences:
            words = set(jieba.cut(sent))
            if words & DESCRIPTION_WORDS and len(sent) > 20:
                desc_count += 1
        features.description_ratio = desc_count / len(sentences) if sentences else 0
        features.narration_ratio = 1 - features.dialogue_ratio - features.description_ratio
        features.narration_ratio = max(0, features.narration_ratio)

        # Top words (excluding stop words and punctuation)
        word_counter: Counter[str] = Counter()
        for word, flag in pseg.cut(text):
            if len(word) >= 2 and flag.startswith(("n", "v", "a", "d")):
                word_counter[word] += 1
        features.top_words = word_counter.most_common(30)

        # Rhetoric frequency
        simile_count = len(SIMILE_PATTERN.findall(text))
        parallel_count = len(PARALLEL_PATTERN.findall(text))
        rhetorical_count = len(RHETORICAL_Q.findall(text))
        n_sentences = len(sentences) or 1
        features.rhetoric_frequency = {
            "simile": simile_count / n_sentences,
            "parallelism": parallel_count / n_sentences,
            "rhetorical_question": rhetorical_count / n_sentences,
        }

        # Paragraph rhythm
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        if paragraphs:
            para_lengths = [len(p) for p in paragraphs]
            rhythm_parts = []
            for pl in para_lengths[:10]:  # Sample first 10 paragraphs
                if pl < 50:
                    rhythm_parts.append("S")
                elif pl < 150:
                    rhythm_parts.append("M")
                else:
                    rhythm_parts.append("L")
            features.paragraph_rhythm = "-".join(rhythm_parts)

        # POV detection
        first_count = len(FIRST_PERSON.findall(text))
        third_count = len(THIRD_PERSON_HE.findall(text))
        if first_count > third_count * 2:
            features.pov_type = "first_person"
        elif third_count > first_count * 2:
            features.pov_type = "third_person"
        else:
            features.pov_type = "mixed"

        return features


# =============================================================================
# Embedding Generation
# =============================================================================


async def generate_embedding(text: str) -> list[float]:
    """Generate embedding using ModelRouter's dedicated embedding provider."""
    router = get_model_router()
    return await router.embed(text)


# =============================================================================
# Utilities
# =============================================================================


def _parse_json(text: str) -> dict:
    """Parse JSON from LLM output, handling markdown code blocks."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return json.loads(cleaned)
