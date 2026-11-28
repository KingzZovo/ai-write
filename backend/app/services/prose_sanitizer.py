"""Deterministic post-generation prose sanitizer.

Mechanism-level guard against structural/meta-narrative leakage that the
LLM occasionally emits despite prompt-level prohibitions. This runs on the
final text right before persistence, so it protects both the single-shot
and scene-staged generation paths regardless of which model produced the
prose.

Scope is intentionally narrow: it only strips references that a character
inside the story can never legitimately utter (chapter/volume numbering,
outline structure tags, context-pack delimiters). It does NOT rewrite prose
or rephrase story content — a regex strip is reliable for these well-defined
patterns, whereas an LLM rewrite would be slow and could introduce new drift.

**唯一的例外是章内同源段落折叠**(2026-11-28):生成侧会把同一段反复吐出来
(实测卷 4 第 22 章 52 段里 18 段同源、卷 3 第 83 章 152 段 / 10,678 字),
那是解码/续写重叠的产物,不是编辑意义上的重复。它同时抬高字数、压低
narrative_pacing(章内近重复段落占比与 pacing 相关 r=-0.44,强于字数的 -0.38),
并让评审报出**没有唯一锚点**的 issue(重复的东西本来就多处出现)→ 局部修订
0 span → 整章进复核。这属于"机械性清理",与糟粕化的词频清零/近义词替换
(项目文风契约明令禁止)不是一回事,见 ``collapse_duplicate_paragraphs``。
"""

from __future__ import annotations

import difflib
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
# touched. 2026-11-28 全书质量检查发现旧规则只认"夹在两个汉字之间",于是
# **句尾/引号前**的半角标点整片漏网(实测 2,016 处 / 230 章:"这份?"、
# "走了!"、"说:"、"了,"),现改为"汉字之后、且后面不是拉丁字母或数字":
#   - CJK + , ; : ? ! 之后不是 [0-9A-Za-z] -> 全角(覆盖"汉字之间"、
#     句尾、换行前、引号/括号前)
#   - 半角逗号直接接汉字(前面不是汉字,如 "3000,又转起来")-> 全角
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_HALFWIDTH_TO_FULLWIDTH = {",": "\uff0c", ";": "\uff1b", ":": "\uff1a", "?": "\uff1f", "!": "\uff01"}
_PUNCT_AFTER_CJK_RE = re.compile(rf"(?<=[{_CJK}])([,;:?!])(?![0-9A-Za-z\"'])")
_COMMA_BEFORE_CJK_RE = re.compile(rf",(?=[{_CJK}])")


def _normalize_halfwidth_punct(text: str) -> str:
    """Convert half-width punctuation to full-width in CJK context only."""
    text = _PUNCT_AFTER_CJK_RE.sub(
        lambda m: _HALFWIDTH_TO_FULLWIDTH[m.group(1)], text
    )
    return _COMMA_BEFORE_CJK_RE.sub("\uff0c", text)

# --- 章内同源段落折叠 --------------------------------------------------------
# 生成侧会把同一段反复吐出来(解码/续写重叠),不是编辑意义的重复。参数保守:
DUPLICATE_PREFIX = 24         # 前 N 字作为同源指纹
DUPLICATE_MIN_PARAGRAPH = 24  # 太短的段落不参与(对白短句天然会重复)
DUPLICATE_SIMILARITY = 0.85   # 指纹相同还不够,整体相似度也要到这条线


def _paragraph_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def collapse_duplicate_paragraphs(text: str) -> tuple[str, int]:
    """折叠**同一章内**的同源段落,只保留首次出现;返回 ``(新文本, 折叠段数)``。

    为什么这与项目文风契约不冲突:契约禁止的是**按词频清零**与**为避重换成失真
    的近义词**(``narrative_quality_gates`` 明写"不按词频清零");这里删的是
    **成段重复**——解码/续写重叠的产物,首次出现原样保留,措辞一个字不改。

    边界:只在章内比较;指纹(前 24 字)相同 **且** 整体相似度 >= 0.85 才算同源;
    短段落不参与;永远保留第一次出现;没有折叠时原样返回(调用方据此保持
    "无变更"的既有语义)。
    """
    if not text:
        return text, 0
    parts = re.split(r"(\n+)", text)      # [段, 分隔, 段, 分隔, ...]
    kept: dict[str, list[str]] = {}
    out: list[str] = []
    dropped = 0
    for i, chunk in enumerate(parts):
        if i % 2 == 1:                    # 分隔符
            out.append(chunk)
            continue
        body = chunk.strip()
        if len(body) < DUPLICATE_MIN_PARAGRAPH:
            out.append(chunk)
            continue
        key = body[:DUPLICATE_PREFIX]
        prior = kept.get(key)
        if prior and any(_paragraph_similarity(body, seen) >= DUPLICATE_SIMILARITY
                         for seen in prior):
            dropped += 1
            if i + 1 < len(parts):
                parts[i + 1] = ""         # 连它前面的空行一起去掉,避免留下空洞
            continue
        kept.setdefault(key, []).append(body)
        out.append(chunk)
    if not dropped:
        return text, 0
    return "".join(out), dropped


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

    # 章内同源段落折叠 —— 本模块唯一"动内容"的例外,理由见模块 docstring。
    # 放在 `if not hits` 之前:折叠本身要计入 hits,否则会被当成"无变更"而丢弃。
    cleaned, collapsed = collapse_duplicate_paragraphs(cleaned)
    if collapsed:
        hits.append(f"duplicate_paragraphs:{collapsed}")

    if not hits:
        return text, []

    # Collapse any double spaces / dangling punctuation the removal may have
    # left behind, but stay conservative — do not touch newlines/paragraphs.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, hits
