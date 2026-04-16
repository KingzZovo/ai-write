"""StyleDetection: Deep writing style analysis combining statistics + LLM.

Extracts:
- Statistical features (sentence patterns, dialogue ratio, rhythm)
- LLM-powered deep analysis (narrative voice, rhetoric, emotion, style labels)
- Filters out person names and place names from keyword lists
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

# AI-generated patterns to detect
AI_MARKERS = [
    "璀璨", "瑰丽", "熠熠生辉", "油然而生", "心潮澎湃",
    "不禁", "缓缓", "仿佛", "宛如", "犹如", "此外",
    "值得注意的是", "需要强调的是", "不可忽视", "彰显",
    "诠释", "赋能", "映射", "折射", "毫无疑问",
    "不言而喻", "无法自拔", "血液沸腾", "电光火石",
]

LLM_STYLE_PROMPT = """你是一位专业的文学分析师。请分析以下小说文本的写作风格特征。

分析维度：
1. narrative_pov（叙事视角）：第一人称/第三人称有限/第三人称全知/多视角交替
2. narrative_pace（叙事节奏）：快节奏（动作密集）/中节奏（平衡）/慢节奏（描写细腻）
3. rhetoric_techniques（修辞手法）：列出使用的主要修辞手法（比喻/拟人/排比/夸张等）
4. emotional_tone（情感基调）：热血/冷峻/幽默/压抑/温暖/悬疑 等
5. sentence_style（句式偏好）：短句为主/长句为主/长短交替/对话驱动
6. description_focus（描写重点）：动作/心理/环境/对话 中哪些更突出
7. style_labels（风格标签）：3-5个最能概括此文风的标签
8. strengths（写作优势）：这段文字写得好的2-3个方面
9. weaknesses（可改进处）：可以提升的1-2个方面
10. writing_rules（写作规则建议）：基于分析结果，生成3-5条具体的写作规则

输出纯 JSON 格式。

文本内容：
"""


def detect_style_features(text: str) -> dict:
    """Statistical analysis of text style features (no LLM, fast)."""
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

    # 2. Dialogue ratio — match Chinese quotes: \u201c...\u201d and \u300c...\u300d
    dialogue_matches = re.findall(r'\u201c[^\u201d]*\u201d|\u300c[^\u300d]*\u300d|\u300e[^\u300f]*\u300f', text)
    dialogue_chars = sum(len(m) for m in dialogue_matches)
    features["dialogue_ratio"] = round(dialogue_chars / max(len(text), 1), 3)

    # 3. Description density
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

    # 6. Tone
    exclamation_ratio = text.count("！") / max(len(sentences), 1)
    question_ratio = text.count("？") / max(len(sentences), 1)
    features["tone"] = {
        "exclamation_ratio": round(exclamation_ratio, 3),
        "question_ratio": round(question_ratio, 3),
    }

    # 7. Style labels — derived from features, NOT high-frequency words
    features["style_labels"] = _derive_style_labels(features)

    return features


def _derive_style_labels(features: dict) -> list[str]:
    """Derive meaningful style labels from statistical features.

    Returns labels like "节奏紧凑", "对话驱动", "画面感强" instead of
    useless high-frequency words like "没有", "时候".
    """
    labels: list[str] = []

    # Sentence rhythm
    dist = features.get("sentence_distribution", {})
    if dist.get("short_pct", 0) > 0.45:
        labels.append("节奏紧凑")
    elif dist.get("long_pct", 0) > 0.4:
        labels.append("叙事舒缓")
    else:
        labels.append("节奏均衡")

    avg_len = features.get("basic", {}).get("avg_sentence_length", 15)
    if avg_len < 12:
        labels.append("短句为主")
    elif avg_len > 25:
        labels.append("长句绵密")

    # Dialogue
    dr = features.get("dialogue_ratio", 0)
    if dr > 0.35:
        labels.append("对话驱动")
    elif dr > 0.2:
        labels.append("对话适中")
    elif dr < 0.05:
        labels.append("纯叙述")
    else:
        labels.append("叙述为主")

    # Description density
    dd = features.get("description_density", 0)
    if dd > 2.0:
        labels.append("描写细腻")
    elif dd > 1.2:
        labels.append("画面感强")

    # Tone
    tone = features.get("tone", {})
    if tone.get("exclamation_ratio", 0) > 0.15:
        labels.append("情感强烈")
    if tone.get("question_ratio", 0) > 0.1:
        labels.append("悬念感强")

    # AI markers
    ai_count = len(features.get("ai_markers", []))
    if ai_count == 0:
        labels.append("语言自然")
    elif ai_count > 5:
        labels.append("AI痕迹重")

    # Paragraph density
    basic = features.get("basic", {})
    chars = basic.get("total_chars", 0)
    paras = basic.get("paragraph_count", 1)
    if paras > 0 and chars / paras < 100:
        labels.append("段落紧凑")
    elif paras > 0 and chars / paras > 300:
        labels.append("段落厚重")

    return labels


async def detect_style_with_llm(text: str) -> dict:
    """Deep style analysis using LLM. Returns structured analysis."""
    from app.services.model_router import get_model_router

    router = get_model_router()

    # Truncate to ~3000 chars for LLM analysis
    sample = text[:3000] if len(text) > 3000 else text

    try:
        result = await router.generate(
            task_type="extraction",
            messages=[
                {"role": "system", "content": "你是文学风格分析专家，只输出 JSON。"},
                {"role": "user", "content": LLM_STYLE_PROMPT + sample},
            ],
            max_tokens=1024,
        )
        cleaned = result.text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(cleaned)
    except Exception as e:
        logger.warning("LLM style detection failed: %s", e)
        return {"llm_error": str(e)}


def features_to_rules(features: dict, llm_analysis: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Convert detected features + LLM analysis into StyleProfile rules_json format.

    Returns (rules, anti_ai_rules).
    """
    rules: list[dict] = []

    # --- Rules from statistical features ---

    dist = features.get("sentence_distribution", {})
    if dist.get("short_pct", 0) > 0.4:
        rules.append({"rule": "偏好短句，节奏紧凑", "weight": 0.7, "category": "rhythm"})
    elif dist.get("long_pct", 0) > 0.4:
        rules.append({"rule": "偏好长句，叙事从容", "weight": 0.7, "category": "rhythm"})
    else:
        rules.append({"rule": "长短句交替，节奏自然", "weight": 0.6, "category": "rhythm"})

    dr = features.get("dialogue_ratio", 0)
    if dr > 0.3:
        rules.append({"rule": "对话密集，以对话推动情节", "weight": 0.75, "category": "structure"})
    elif dr < 0.1:
        rules.append({"rule": "叙述为主，少量对话", "weight": 0.65, "category": "structure"})

    dd = features.get("description_density", 0)
    if dd > 1.5:
        rules.append({"rule": "注重环境和细节描写", "weight": 0.7, "category": "style"})

    # --- Rules from LLM analysis (higher confidence) ---
    if llm_analysis and "llm_error" not in llm_analysis:
        # Narrative POV
        pov = llm_analysis.get("narrative_pov", "")
        if pov:
            rules.append({"rule": f"叙事视角：{pov}", "weight": 0.85, "category": "structure"})

        # Narrative pace
        pace = llm_analysis.get("narrative_pace", "")
        if pace:
            rules.append({"rule": f"叙事节奏：{pace}", "weight": 0.8, "category": "rhythm"})

        # Rhetoric
        rhetoric = llm_analysis.get("rhetoric_techniques", [])
        if isinstance(rhetoric, list) and rhetoric:
            rules.append({"rule": f"常用修辞：{'、'.join(rhetoric[:4])}", "weight": 0.7, "category": "style"})
        elif isinstance(rhetoric, str) and rhetoric:
            rules.append({"rule": f"常用修辞：{rhetoric}", "weight": 0.7, "category": "style"})

        # Emotional tone
        tone = llm_analysis.get("emotional_tone", "")
        if tone:
            rules.append({"rule": f"情感基调：{tone}", "weight": 0.8, "category": "style"})

        # Sentence style
        ss = llm_analysis.get("sentence_style", "")
        if ss:
            rules.append({"rule": f"句式偏好：{ss}", "weight": 0.75, "category": "rhythm"})

        # Description focus
        df = llm_analysis.get("description_focus", "")
        if df:
            rules.append({"rule": f"描写重点：{df}", "weight": 0.7, "category": "style"})

        # LLM-generated writing rules (highest weight)
        llm_rules = llm_analysis.get("writing_rules", [])
        if isinstance(llm_rules, list):
            for r in llm_rules[:5]:
                rule_text = r if isinstance(r, str) else r.get("rule", str(r))
                rules.append({"rule": rule_text, "weight": 0.85, "category": "llm_derived"})

    # --- Anti-AI rules from markers ---
    anti_ai: list[dict] = []
    for marker_str in features.get("ai_markers", []):
        word = marker_str.split("(")[0]
        anti_ai.append({"pattern": word, "replacement": "", "autoRewrite": False})

    return rules, anti_ai
