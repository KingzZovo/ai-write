"""Render measured reference style without inventing chapter-level quotas."""
import math


def render_observations(data) -> str:
    if not isinstance(data, dict):
        return ""
    observations = []
    metrics = (
        ("dialogue", "ratio", "对话文字占比", "%", 100),
        ("metaphor", "total_per_kchar", "比喻统计", "次/千字", 1),
        ("metaphor", "sentence_end_per_kchar", "句尾比喻统计", "次/千字", 1),
        ("psychology", "pattern_total_per_kchar", "预定义心理套语命中", "次/千字", 1),
        ("psychology", "neutral_words_per_kchar", "心理相关词命中", "次/千字", 1),
        ("sentence", "mean_chars", "平均句长", "字", 1),
        ("paragraph", "mean_chars", "平均段长", "字", 1),
        ("colloquial", "particles_per_kchar", "口语助词命中", "次/千字", 1),
    )
    for section, key, label, unit, scale in metrics:
        group = data.get(section)
        if not isinstance(group, dict):
            continue
        raw = group.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        if key == "ratio" and value > 1:
            continue
        observations.append(f"· {label}：{value * scale:.3g}{unit}。")
    if not observations:
        return ""
    source = data.get("source")
    source = source.strip()[:80] if isinstance(source, str) and source.strip() else "所选参考样本"
    return "\n".join([
        f"【参考样本统计 — {source}】",
        "以下仅描述样本，不是本章目标、上下限或合格线；未提供的统计不推定为零。",
        "人物身份、当前关系、叙述视角和场面需要优先；对话、心理与句段长短随场景变化，不为接近数字增删文字。",
        *observations,
        "词语命中不等于心理深度或语言优劣；结合原句判断表达是否准确、重复是否有新的作用。参考作品的口吻不自动适用于本书人物。",
    ])
