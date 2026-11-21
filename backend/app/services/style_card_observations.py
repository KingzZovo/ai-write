"""Describe sampled style cards without selecting a mandatory writing style."""
from collections import Counter, defaultdict
import math


def render_style_cards(profiles) -> list[str]:
    texts = defaultdict(Counter)
    sensory = defaultdict(list)
    count = 0
    scalar_fields = {
        "pov": "视角记录", "tense": "时态记录", "sentence_rhythm": "句式节奏",
        "dialogue_style": "对话风格", "pacing": "节奏", "emotional_register": "情绪基调",
    }
    list_fields = {
        "vocab_tone": "词汇调性", "forbidden_tells": "样本中回避的表达",
        "signature_moves": "样本常见手法",
    }
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        count += 1
        for key in scalar_fields:
            value = profile.get(key)
            if isinstance(value, str) and value.strip():
                texts[key][value.strip()] += 1
        for key in list_fields:
            values = profile.get(key)
            if isinstance(values, list):
                texts[key].update({v.strip() for v in values if isinstance(v, str) and v.strip()})
        values = profile.get("sensory_mix")
        if isinstance(values, dict):
            for key, raw in values.items():
                if not isinstance(key, str) or not key or key != key.strip() or raw is None or isinstance(raw, bool):
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(value) and 0 <= value <= 1:
                    sensory[key].append(value)

    lines = []
    for key, label in {**scalar_fields, **list_fields}.items():
        # Frequency describes coverage, not correctness. Lexical tie-breaking
        # gives stable output independent of hash seed or input row order.
        ranked = sorted(texts[key].items(), key=lambda item: (-item[1], item[0]))
        if ranked:
            shown = " / ".join(f"{text}（{n}/{count}片）" for text, n in ranked[:3])
            suffix = f"；另有{len(ranked) - 3}种记录未列出" if len(ranked) > 3 else ""
            lines.append(f"- {label}: {shown}{suffix}")
    if sensory:
        ranked = sorted(sensory.items(), key=lambda item: (-len(item[1]), item[0]))
        shown = " / ".join(
            f"{key} {sum(values)/len(values):.1%}（有效{len(values)}片）" for key, values in ranked[:5]
        )
        if len(ranked) > 5:
            shown += f"；另有{len(ranked) - 5}项未列出"
        lines.append(f"- 感官分布记录: {shown}。各项仅按提供该项的有效切片求均值，缺失不补零，不作为本章比例。")
    if not lines:
        return []
    return ["\n".join([
        f"【参考书切片观察 — {count}片，不代表全书】",
        "以下是样本的不同表现，不是本章必须采用的规则。视角与时态服从本章既定要求；人物口吻、详略与手法按当前关系和场面选择，不照搬样本禁忌或招牌。",
        *lines,
    ])]
