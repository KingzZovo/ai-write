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


def sanitize_prose(text: str) -> tuple[str, list[str]]:
    """Strip meta/structural leakage from generated prose.

    Returns ``(cleaned_text, hits)`` where ``hits`` lists the raw leaked
    fragments that were removed (for logging/telemetry). When nothing leaks,
    ``hits`` is empty and the text is returned unchanged.
    """
    if not text:
        return text, []

    hits: list[str] = []

    def _record(m: re.Match) -> str:
        hits.append(m.group(0))
        return ""

    cleaned = _CONTEXT_DELIMITER_RE.sub(_record, text)
    cleaned = _CHAPTER_REF_RE.sub(_record, cleaned)

    if not hits:
        return text, []

    # Collapse any double spaces / dangling punctuation the removal may have
    # left behind, but stay conservative — do not touch newlines/paragraphs.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, hits
