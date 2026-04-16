"""StyleDetection: Extract writing style features from text.

Analyzes text to identify:
- Narrative voice and tone
- Sentence structure patterns
- Common vocabulary and phrases
- Dialogue style
- Description density
- Anti-AI word usage
"""

from __future__ import annotations

import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

# Common AI-generated patterns to detect
AI_MARKERS = [
    "璀璨", "瑰丽", "熠熠生辉", "油然而生", "心潮澎湃",
    "不禁", "缓缓", "仿佛", "宛如", "犹如", "此外",
    "值得注意的是", "需要强调的是", "不可忽视", "彰显",
    "诠释", "赋能", "映射", "折射", "毫无疑问",
    "不言而喻", "无法自拔", "血液沸腾", "电光火石",
]


def detect_style_features(text: str) -> dict:
    """Analyze text and extract style features as a structured dict.

    Returns a dict suitable for storing as StyleProfile.rules_json source data.
    """
    if not text or len(text) < 100:
        return {"error": "文本过短，无法分析"}

    features: dict = {}

    # 1. Basic stats
    sentences = re.split(r'[。！？…]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    avg_sentence_len = sum(len(s) for s in sentences) / max(len(sentences), 1)

    features["basic"] = {
        "total_chars": len(text),
        "sentence_count": len(sentences),
        "avg_sentence_length": round(avg_sentence_len, 1),
        "paragraph_count": text.count("\n\n") + 1,
    }

    # 2. Dialogue ratio
    dialogue_chars = sum(len(m) for m in re.findall(r'[""「」『』].*?[""「」『』]', text))
    features["dialogue_ratio"] = round(dialogue_chars / max(len(text), 1), 3)

    # 3. Description density (adjectives, adverbs via simple heuristic)
    adj_patterns = re.findall(r'[\u7684][\u4e00-\u9fff]{1,4}', text)
    features["description_density"] = round(len(adj_patterns) / max(len(sentences), 1), 2)

    # 4. AI marker detection
    ai_hits: list[str] = []
    for marker in AI_MARKERS:
        count = text.count(marker)
        if count > 0:
            ai_hits.append(f"{marker}({count})")
    features["ai_markers"] = ai_hits
    features["ai_marker_density"] = round(len(ai_hits) / max(len(text) / 1000, 1), 2)

    # 5. Sentence length distribution
    short = sum(1 for s in sentences if len(s) < 10)
    medium = sum(1 for s in sentences if 10 <= len(s) < 30)
    long = sum(1 for s in sentences if len(s) >= 30)
    total = max(len(sentences), 1)
    features["sentence_distribution"] = {
        "short_pct": round(short / total, 2),
        "medium_pct": round(medium / total, 2),
        "long_pct": round(long / total, 2),
    }

    # 6. High-frequency words (excluding common function words)
    import jieba
    words = list(jieba.cut(text))
    stop_words = {"的", "了", "在", "是", "我", "他", "她", "你", "不", "有", "这", "那", "就", "都", "也", "要", "会", "对"}
    word_counts = Counter(w for w in words if len(w) >= 2 and w not in stop_words)
    features["top_words"] = [{"word": w, "count": c} for w, c in word_counts.most_common(20)]

    # 7. Tone classification (simple heuristic)
    exclamation_ratio = text.count("！") / max(len(sentences), 1)
    question_ratio = text.count("？") / max(len(sentences), 1)
    features["tone"] = {
        "exclamation_ratio": round(exclamation_ratio, 3),
        "question_ratio": round(question_ratio, 3),
    }

    return features


def features_to_rules(features: dict, source_name: str = "") -> list[dict]:
    """Convert detected features into StyleProfile rules_json format."""
    rules: list[dict] = []

    # Sentence length rule
    dist = features.get("sentence_distribution", {})
    if dist.get("short_pct", 0) > 0.4:
        rules.append({"rule": "偏好短句，节奏紧凑", "weight": 0.7, "category": "rhythm"})
    elif dist.get("long_pct", 0) > 0.4:
        rules.append({"rule": "偏好长句，叙事从容", "weight": 0.7, "category": "rhythm"})
    else:
        rules.append({"rule": "长短句交替，节奏自然", "weight": 0.6, "category": "rhythm"})

    # Dialogue style
    dr = features.get("dialogue_ratio", 0)
    if dr > 0.3:
        rules.append({"rule": "对话密集，以对话推动情节", "weight": 0.75, "category": "structure"})
    elif dr < 0.1:
        rules.append({"rule": "叙述为主，少量对话", "weight": 0.65, "category": "structure"})

    # Description density
    dd = features.get("description_density", 0)
    if dd > 1.5:
        rules.append({"rule": "注重环境和细节描写", "weight": 0.7, "category": "style"})

    # AI markers → Anti-AI rules
    anti_ai: list[dict] = []
    for marker in features.get("ai_markers", []):
        word = marker.split("(")[0]
        anti_ai.append({"pattern": word, "replacement": "", "autoRewrite": False})

    return rules, anti_ai
