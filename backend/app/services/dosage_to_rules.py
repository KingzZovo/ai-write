"""DosageToRules: deterministic deriver from a v8 dosage_profile to rules_json.

The v8 "剂量画像" pipeline produces rich statistical profiles in
``StyleProfile.config_json['dosage_profile']`` (sentence stats, dialogue
ratios, metaphor / psychology / parallelism patterns, AI-metaword counts,
etc.) but historically did NOT populate ``rules_json``. As a result the
styles UI shows "0 rules" for dosage-only profiles, and ``compile_style``
produces no style guidance for the LLM.

This module translates a dosage profile into a list of human-readable,
weight-tagged rules that the existing ``rules_json`` consumers (UI counter,
``style_compiler.compile_style``) already understand. It also extends the
Anti-AI rule list with deterministic prompt-leak / metaword guards driven
by the ``ai_metawords_count`` slice of the dosage profile.

The deriver is **deterministic** — same input always produces byte-stable
output — to make regression / golden tests easy.

Schema (subset that we read; missing keys degrade gracefully to 'no rule'):

  dosage_profile:
    sentence:    {count, mean_chars, std_chars}
    paragraph:   {count, mean_chars, std_chars}
    dialogue:    {ratio, per_kchar, turn_count, turn_chars_mean}
    metaphor:    {total_per_kchar, sentence_end_per_kchar,
                  patterns_per_kchar: {<中文 pattern>: float, ...}}
    psychology:  {pattern_total_per_kchar, neutral_words_per_kchar,
                  pattern_per_chapter_7k,
                  patterns_per_kchar: {<pattern>: float, ...}}
    parallelism: {XYX_per_kchar, ABAB_per_kchar}
    colloquial:  {particles_per_kchar, onomatopoeia_per_kchar}
    total_chars / total_kchars
    ai_metawords_count: {<word>: int, ...}

Returns ``(rules, anti_ai_additions)``:
  - ``rules`` matches the existing rule shape used by ``style_compiler``:
        {rule, weight, category, source_metric}
  - ``anti_ai_additions`` matches the existing anti-AI shape:
        {pattern, replacement, autoRewrite, reason}

Weight tiers are picked from how strongly each metric stands out from the
genre baseline (see ``_BASELINE``). Missing/zero metrics produce no rule.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Genre baseline — used to size weights deterministically
# ----------------------------------------------------------------------------
#
# These are MEAN values observed across the v8 dosage profiles we currently
# have in the corpus (龙族 / 天之炽 / 天之炽②). They serve as a "current
# Chinese genre fiction baseline". When a profile deviates from baseline
# we tag the rule with a stronger weight so the LLM treats it as a
# defining feature.
_BASELINE: dict[str, float] = {
    "sentence.mean_chars": 28.0,
    "sentence.std_chars": 22.0,
    "paragraph.mean_chars": 65.0,
    "dialogue.ratio": 0.30,
    "dialogue.per_kchar": 11.0,
    "dialogue.turn_chars_mean": 28.0,
    "metaphor.total_per_kchar": 4.0,
    "metaphor.sentence_end_per_kchar": 1.5,
    "psychology.pattern_total_per_kchar": 0.04,
    "psychology.neutral_words_per_kchar": 4.0,
    "parallelism.XYX_per_kchar": 0.05,
    "parallelism.ABAB_per_kchar": 0.40,
    "colloquial.particles_per_kchar": 0.70,
    "colloquial.onomatopoeia_per_kchar": 0.10,
}

# Threshold for "this metric is meaningful" — below this we skip rule emission.
_NEGLIGIBLE = 1e-6

# AI metawords that are virtually always prompt leakage. If their count is
# >= 1 we add an anti-ai rule even if the model already filled some.
_PROMPT_LEAK_WORDS = {
    "prompt": "prompt self-reference",
    "黑名单": "prompt self-reference",
    "护城词": "prompt self-reference",
    "以下是": "prompt format leakage",
    "根据您": "prompt format leakage",
    "以上便是": "prompt format leakage",
    "以下是我": "prompt format leakage",
}

# AI metawords that signal meta narration about writing itself — should
# never appear in the prose unless used in dialogue.
_META_NARRATION_WORDS = {
    "节奏": "meta narration about pacing",
    "节拍": "meta narration about pacing",
    "转折": "meta narration about plot",
    "钩子": "meta narration about plot",
    "伏笔": "meta narration about plot",
    "五感": "meta narration about description",
    "短句": "meta narration about style",
}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _weight_for(metric_value: float, baseline: float) -> float:
    """Map deviation from baseline to a weight tier.

    Weights match the existing UI/compile_style buckets:
      - >= 0.85 "必须" / must maintain (signature feature)
      - >= 0.65 "优先" / preferred
      - <  0.65 "参考" / reference

    Closer to baseline = lower weight. Far from baseline = stronger
    signature so worth higher weight.
    """
    if baseline <= _NEGLIGIBLE:
        # Cannot compute relative deviation; use absolute size buckets.
        if metric_value >= 1.0:
            return 0.85
        if metric_value >= 0.1:
            return 0.7
        return 0.55
    deviation = abs(metric_value - baseline) / baseline
    if deviation >= 0.30:
        return 0.85
    if deviation >= 0.10:
        return 0.70
    return 0.55


def _topn_patterns(patterns: dict[str, float], n: int) -> list[tuple[str, float]]:
    """Return top-N patterns sorted by descending value, filtering zeros."""
    if not isinstance(patterns, dict):
        return []
    items = [(k, _safe_float(v)) for k, v in patterns.items()]
    items = [(k, v) for k, v in items if v > _NEGLIGIBLE]
    items.sort(key=lambda x: (-x[1], x[0]))  # stable for ties
    return items[:n]


# ----------------------------------------------------------------------------
# Per-dimension rule emitters — each returns a list[dict].
# ----------------------------------------------------------------------------


def _rules_sentence(s: dict) -> list[dict]:
    out: list[dict] = []
    mean = _safe_float(s.get("mean_chars"))
    std = _safe_float(s.get("std_chars"))
    if mean <= _NEGLIGIBLE:
        return out
    weight = _weight_for(mean, _BASELINE["sentence.mean_chars"])
    out.append({
        "rule": f"句长均值约 {mean:.1f} 字，标准差 {std:.1f}；长短句交替、宁紧勿堆",
        "weight": weight,
        "category": "rhythm",
        "source_metric": f"sentence.mean_chars={mean:.2f},std={std:.2f}",
    })
    return out


def _rules_paragraph(p: dict) -> list[dict]:
    out: list[dict] = []
    mean = _safe_float(p.get("mean_chars"))
    if mean <= _NEGLIGIBLE:
        return out
    if mean < 50:
        text = f"段落紧凑，平均约 {mean:.0f} 字一段；以点到为止的短段推进节奏"
    elif mean > 90:
        text = f"段落厚重，平均约 {mean:.0f} 字一段；重场面可拉长营造沉浸感"
    else:
        text = f"段落粒度折中，平均约 {mean:.0f} 字；该需使你能需要剩能调节"
    out.append({
        "rule": text,
        "weight": _weight_for(mean, _BASELINE["paragraph.mean_chars"]),
        "category": "rhythm",
        "source_metric": f"paragraph.mean_chars={mean:.2f}",
    })
    return out


def _rules_dialogue(d: dict) -> list[dict]:
    out: list[dict] = []
    if not isinstance(d, dict):
        return out
    ratio = _safe_float(d.get("ratio"))
    per_k = _safe_float(d.get("per_kchar"))
    turn_mean = _safe_float(d.get("turn_chars_mean"))
    if ratio <= _NEGLIGIBLE and per_k <= _NEGLIGIBLE:
        return out
    if ratio > 0:
        text = f"对话占比约 {ratio*100:.1f}%，每千字 {per_k:.1f} 段对白；对话推动情节、避免大段独白"
        out.append({
            "rule": text,
            "weight": _weight_for(ratio, _BASELINE["dialogue.ratio"]),
            "category": "dialogue",
            "source_metric": f"dialogue.ratio={ratio:.3f},per_kchar={per_k:.2f}",
        })
    if turn_mean > _NEGLIGIBLE:
        if turn_mean < 18:
            qual = "短锐，多是交锁式快话"
        elif turn_mean > 40:
            qual = "偏长，允许独白、思索、状态描写插入"
        else:
            qual = "适中，对白与动作/心理描写交织"
        out.append({
            "rule": f"平均对白回合约 {turn_mean:.1f} 字，{qual}",
            "weight": _weight_for(turn_mean, _BASELINE["dialogue.turn_chars_mean"]),
            "category": "dialogue",
            "source_metric": f"dialogue.turn_chars_mean={turn_mean:.2f}",
        })
    return out


def _rules_metaphor(m: dict) -> list[dict]:
    out: list[dict] = []
    if not isinstance(m, dict):
        return out
    total = _safe_float(m.get("total_per_kchar"))
    end = _safe_float(m.get("sentence_end_per_kchar"))
    patterns = m.get("patterns_per_kchar") or {}
    if total > _NEGLIGIBLE:
        text = f"比喻密度约 {total:.2f}/千字；比喻应服于场景、不为凑句子凑"
        out.append({
            "rule": text,
            "weight": _weight_for(total, _BASELINE["metaphor.total_per_kchar"]),
            "category": "description",
            "source_metric": f"metaphor.total_per_kchar={total:.3f}",
        })
    top = _topn_patterns(patterns, 3)
    if top:
        sample = "、".join(f"「{k}」" for k, _ in top)
        out.append({
            "rule": f"常用比喻句式：{sample}；优先使用这些外形避免生造奇喻",
            "weight": 0.65,
            "category": "description",
            "source_metric": "metaphor.patterns_per_kchar.top3",
        })
    if end > _NEGLIGIBLE:
        out.append({
            "rule": f"句末隐喻约 {end:.2f}/千字；段末可以隐喻收尾、加重余韵",
            "weight": _weight_for(end, _BASELINE["metaphor.sentence_end_per_kchar"]),
            "category": "style",
            "source_metric": f"metaphor.sentence_end_per_kchar={end:.3f}",
        })
    return out


def _rules_psychology(ps: dict) -> list[dict]:
    out: list[dict] = []
    if not isinstance(ps, dict):
        return out
    pat_total = _safe_float(ps.get("pattern_total_per_kchar"))
    neutral = _safe_float(ps.get("neutral_words_per_kchar"))
    patterns = ps.get("patterns_per_kchar") or {}
    top = _topn_patterns(patterns, 4)
    if top:
        sample = "、".join(f"「{k}」" for k, _ in top)
        out.append({
            "rule": f"心理描写偏身体反应型：{sample}；避免抽象心理词堆砌",
            "weight": _weight_for(pat_total, _BASELINE["psychology.pattern_total_per_kchar"]),
            "category": "description",
            "source_metric": "psychology.patterns_per_kchar.top4",
        })
    if neutral > _NEGLIGIBLE:
        out.append({
            "rule": f"中性描写词密度约 {neutral:.2f}/千字；设定、场景、人物描写使用适量中性词避免象象化",
            "weight": 0.55,
            "category": "description",
            "source_metric": f"psychology.neutral_words_per_kchar={neutral:.3f}",
        })
    return out


def _rules_parallelism(p: dict) -> list[dict]:
    out: list[dict] = []
    if not isinstance(p, dict):
        return out
    xyx = _safe_float(p.get("XYX_per_kchar"))
    abab = _safe_float(p.get("ABAB_per_kchar"))
    if abab > _NEGLIGIBLE:
        out.append({
            "rule": f"ABAB 型排比约 {abab:.2f}/千字；重场面、独白、收段可使用节奏感",
            "weight": _weight_for(abab, _BASELINE["parallelism.ABAB_per_kchar"]),
            "category": "custom",
            "source_metric": f"parallelism.ABAB_per_kchar={abab:.3f}",
        })
    if xyx > _NEGLIGIBLE:
        out.append({
            "rule": f"XYX 型排比约 {xyx:.2f}/千字；使用于冲突高潮、心理重者",
            "weight": _weight_for(xyx, _BASELINE["parallelism.XYX_per_kchar"]),
            "category": "custom",
            "source_metric": f"parallelism.XYX_per_kchar={xyx:.3f}",
        })
    return out


def _rules_colloquial(c: dict) -> list[dict]:
    out: list[dict] = []
    if not isinstance(c, dict):
        return out
    par = _safe_float(c.get("particles_per_kchar"))
    ono = _safe_float(c.get("onomatopoeia_per_kchar"))
    if par > _NEGLIGIBLE:
        out.append({
            "rule": f"口语语气词约 {par:.2f}/千字；允许“哦/啦/嘿/呀”等吹槽、避免过于书面中性",
            "weight": _weight_for(par, _BASELINE["colloquial.particles_per_kchar"]),
            "category": "dialogue",
            "source_metric": f"colloquial.particles_per_kchar={par:.3f}",
        })
    if ono > _NEGLIGIBLE:
        out.append({
            "rule": f"拟声词约 {ono:.2f}/千字；动作/环境/状态可限制使用拟声提高场景感",
            "weight": _weight_for(ono, _BASELINE["colloquial.onomatopoeia_per_kchar"]),
            "category": "description",
            "source_metric": f"colloquial.onomatopoeia_per_kchar={ono:.3f}",
        })
    return out


def _anti_ai_from_metawords(meta: dict) -> list[dict]:
    """Translate ai_metawords_count into anti-ai pattern guards."""
    out: list[dict] = []
    if not isinstance(meta, dict):
        return out
    seen: set[str] = set()
    for word, reason in _PROMPT_LEAK_WORDS.items():
        cnt = int(meta.get(word, 0) or 0)
        if cnt >= 1 and word not in seen:
            out.append({
                "pattern": word,
                "replacement": "",
                "autoRewrite": False,
                "reason": reason,
            })
            seen.add(word)
    for word, reason in _META_NARRATION_WORDS.items():
        cnt = int(meta.get(word, 0) or 0)
        # For meta-narration, even a small count above 0 in the prose is
        # a sign the model is talking about writing instead of writing.
        if cnt >= 1 and word not in seen:
            out.append({
                "pattern": word,
                "replacement": "",
                "autoRewrite": False,
                "reason": reason,
            })
            seen.add(word)
    return out


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def derive_rules_from_dosage(
    dosage: dict,
    profile_version: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Derive (rules, anti_ai_additions) from a dosage_profile dict.

    The output format matches what ``style_compiler.compile_style`` expects:
    each rule has ``{rule, weight, category, source_metric}``, each anti-ai
    entry has ``{pattern, replacement, autoRewrite, reason}``.

    Deterministic: same input will always produce byte-stable output for
    easy regression / golden testing.
    """
    if not isinstance(dosage, dict) or not dosage:
        return [], []

    rules: list[dict] = []
    rules.extend(_rules_sentence(dosage.get("sentence") or {}))
    rules.extend(_rules_paragraph(dosage.get("paragraph") or {}))
    rules.extend(_rules_dialogue(dosage.get("dialogue") or {}))
    rules.extend(_rules_metaphor(dosage.get("metaphor") or {}))
    rules.extend(_rules_psychology(dosage.get("psychology") or {}))
    rules.extend(_rules_parallelism(dosage.get("parallelism") or {}))
    rules.extend(_rules_colloquial(dosage.get("colloquial") or {}))

    anti_ai = _anti_ai_from_metawords(dosage.get("ai_metawords_count") or {})

    if profile_version:
        for r in rules:
            r["profile_version"] = profile_version

    return rules, anti_ai


def merge_anti_ai_rules(
    existing: list[dict] | None,
    additions: list[dict],
) -> list[dict]:
    """Merge anti-ai entries by ``pattern`` (case-sensitive), preserving
    order: existing entries first, then new ones not already present.

    Used by recompile-rules to non-destructively append metaword guards.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for entry in existing or []:
        if isinstance(entry, dict):
            pat = str(entry.get("pattern", ""))
            if pat and pat not in seen:
                merged.append(entry)
                seen.add(pat)
    for entry in additions or []:
        if isinstance(entry, dict):
            pat = str(entry.get("pattern", ""))
            if pat and pat not in seen:
                merged.append(entry)
                seen.add(pat)
    return merged
