"""Deterministic post-generation prose sanitizer.

Mechanism-level guard against structural/meta-narrative leakage that the
LLM occasionally emits despite prompt-level prohibitions. This runs on the
final text right before persistence, so it protects both the single-shot
and scene-staged generation paths regardless of which model produced the
prose.

Scope is intentionally narrow: it only strips references that a character
inside the story can never legitimately utter (chapter/volume numbering,
outline structure tags, context-pack delimiters). It does NOT rewrite prose
or touch story content — a regex strip is reliable for these well-defined
patterns, whereas an LLM rewrite would be slow and could introduce new
drift.
"""

from __future__ import annotations

import re

# Context-pack delimiters that must never survive into prose. These are the
# neutral markers the ContextPack now renders instead of "第X章" (see
# context_pack.py). If the model parrots one back, strip it.
_CONTEXT_DELIMITER_RE = re.compile(r"\[(?:CH|VOL|前文|后续)-\d+\]")

# Meta-narrative chapter/volume references inside prose, e.g. "在第10章",
# "第9章从暗格", "上一章". A character does not know which chapter they are in.
# We only target the numbered/structural forms; ordinary words like "章程"
# are left alone because the pattern requires 第…章/卷 or explicit 上/下一章.
_CHAPTER_REF_RE = re.compile(
    r"第\s*[0-9零一二三四五六七八九十百]+\s*[章卷]"
    r"|[上下]一[章卷]"
    r"|后续第\s*[0-9零一二三四五六七八九十百]+\s*[章卷]"
)

# Half-width punctuation contamination (E2E 2026-07-26): generated prose mixes
# half-width commas into CJK sentences ("皮带轮卡了一下,又转起来"). Normalize
# conservatively so numbers ("1,000"), code, URLs and latin text are never
# touched:
#   - , ; : ? ! directly BETWEEN two CJK characters -> full-width equivalent
#   - a half-width comma directly FOLLOWED by a CJK character (no space) ->
#     full-width comma (covers "…了”,他说" / "3000,又转起来" style joins)
_CJK = "㐀-䶿一-鿿豈-﫿"
_HALFWIDTH_TO_FULLWIDTH = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}
_PUNCT_BETWEEN_CJK_RE = re.compile(rf"(?<=[{_CJK}])([,;:?!])(?=[{_CJK}])")
_COMMA_BEFORE_CJK_RE = re.compile(rf",(?=[{_CJK}])")


def _normalize_halfwidth_punct(text: str) -> str:
    """Convert half-width punctuation to full-width in CJK context only."""
    text = _PUNCT_BETWEEN_CJK_RE.sub(
        lambda m: _HALFWIDTH_TO_FULLWIDTH[m.group(1)], text
    )
    return _COMMA_BEFORE_CJK_RE.sub("，", text)


def sanitize_prose(text: str) -> tuple[str, list[str]]:
    """Strip meta/structural leakage from generated prose.

    Returns ``(cleaned_text, hits)`` where ``hits`` lists the raw leaked
    fragments that were removed (for logging/telemetry). Half-width
    punctuation between CJK characters is normalized to full-width as a
    silent pass — it is a mechanical cleanup, not leakage, so it does not
    populate ``hits``. When nothing leaks and no punctuation needs
    normalizing, the text is returned unchanged.
    """
    if not text:
        return text, []

    text = _normalize_halfwidth_punct(text)
    hits: list[str] = []

    def _record(m: re.Match) -> str:
        hits.append(m.group(0))
        return ""

    cleaned = _CONTEXT_DELIMITER_RE.sub(_record, text)
    cleaned = _CHAPTER_REF_RE.sub(_record, cleaned)

    # 2026-11-20:服务话术与中转诊断也走这里 —— **统一落库前净化**。
    # 根因:provider 以 HTTP 200 把"面向用户的话术"当 message 正文透传,位置不定
    # (实测可追在章末最后 70 字:"…留在身后。I seem to be encountering an error.
    # Can I try something else for you?");而旧守卫只**判定**(accept/reject)、
    # 且只认"整章拒答/头部拒答",于是"正文 + 尾随话术"一路通过验收并落库。
    # 剥离规则(位置无关、引号内豁免)见 app/services/prose_purity.py。
    from app.services.prose_purity import strip_service_noise

    purity = strip_service_noise(cleaned)
    if purity.removed:
        hits.extend(f"service_noise:{span.reason}" for span in purity.removed)
        cleaned = purity.text
        if purity.fatal:
            # 整章都是话术 ⇒ 这是"拒答"场景,交回既有拒答守卫(looks_like_refusal)
            # 判 draft —— 净化器不擅自清空正文:清空会被上游当成"空稿异常"
            # (generation_tasks 的 ValueError),改变既有草稿语义。
            # 混合形态(正文 + 话术)仍按上面剥离,那种情况才有正文可救。
            hits.append("service_noise_fatal")
            return text, hits

    if not hits:
        return text, []

    # Collapse any double spaces / dangling punctuation the removal may have
    # left behind, but stay conservative — do not touch newlines/paragraphs.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, hits
