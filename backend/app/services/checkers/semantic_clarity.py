"""PR-B-CRITIC-SEMANTIC-CLARITY (2026-05-13).

Deterministic detector for two common LLM failure modes that the rest of
the critic suite does not cover:

1. **Self-referential paraphrase / circular definition** — the model fills
   pacing by restating itself: "X 是 X", "X 就是 X", "他就是他".
2. **Vague/empty threat statements** — sentence-template clichés like
   "这楼今晚就别想平\" / "今天谁也走不了\" / "别想走\" that read like
   placeholders rather than concrete action.

Bug #7 from the lct2 audit (《这楼今晚就别想平》) motivated this
check — the existing anti-AI scanner caught surface-level metawords but
not template-shaped semantic emptiness.

Output shape matches ``critic_service`` issues:
    {severity, category, desc, location, source}

All matches are reported as ``severity="soft"`` because the model can
argue these phrases are intentional; we want them flagged in the issues
list without auto-triggering a hard rewrite.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pattern 1: self-referential paraphrase
# ---------------------------------------------------------------------------
# Matches Chinese tokens (1-6 chars, no punctuation) repeated around copula.
_TOKEN = r"[\u4e00-\u9fa5A-Za-z0-9]{1,6}"
_SELF_REFERENCE_PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"({_TOKEN})是\1\b",         "X是X 自指复述"),
    (rf"({_TOKEN})就是\1\b",       "X就是X 自指复述"),
    (rf"({_TOKEN})仍是\1\b",       "X仍是X 自指复述"),
    (rf"({_TOKEN})依然是\1\b",     "X依然是X 自指复述"),
)


# ---------------------------------------------------------------------------
# Pattern 2: vague/empty threat templates (bug #7)
# ---------------------------------------------------------------------------
_VAGUE_TEMPLATES: tuple[tuple[str, str], ...] = (
    (r"今晚就别想",              "空洞威胁模板（今晚就别想X）——未交代具体手段/后果"),
    (r"今天就别想",              "空洞威胁模板（今天就别想X）——未交代具体手段/后果"),
    (r"谁也走不了",              "空洞威胁模板（谁也走不了）——未交代具体手段/后果"),
    (r"谁也别想\u8d70",         "空洞威胁模板（谁也别想走）——未交代具体手段/后果"),
    (r"别想走出这",            "空洞威胁模板（别想走出这X）——未交代具体手段/后果"),
    (r"让他好看",                "空洞威胁模板（让他好看）——未交代具体手段/后果"),
    (r"让他付出代价",            "空洞威胁模板（付出代价）——未交代具体手段/后果"),
)


# ---------------------------------------------------------------------------
# Pattern 3: tautological adjective phrases ("不对的地方是不对的")
# ---------------------------------------------------------------------------
_TAUTOLOGY_PATTERNS: tuple[tuple[str, str], ...] = (
    (rf"({_TOKEN})的地方就是\1\b", "同词套同词 同义重复"),
    (rf"({_TOKEN})的原因就是\1\b", "同词套同词 同义重复"),
)


def _snippet(draft: str, idx: int, span: int = 30) -> str:
    start = max(0, idx - span // 2)
    end = min(len(draft), idx + span)
    return draft[start:end].replace("\n", " ")


def _scan_patterns(
    draft: str,
    patterns: tuple[tuple[str, str], ...],
    category: str,
    seen: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for raw_pat, label in patterns:
        try:
            it = re.finditer(raw_pat, draft)
        except re.error as exc:
            logger.debug("semantic_clarity: bad regex %r: %s", raw_pat, exc)
            continue
        for m in it:
            key = (raw_pat, m.start())
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                {
                    "severity": "soft",
                    "category": category,
                    "desc": f"{label}：「{m.group(0)}」",
                    "location": _snippet(draft, m.start()),
                    "source": "rule",
                    "critic_stream": "semantic_clarity",
                }
            )
    return issues


def scan_semantic_clarity(draft: str) -> list[dict[str, Any]]:
    """Synchronous, dependency-free scanner.

    Returns the issue list — may be empty. Designed to be cheap enough to
    run on every draft (no DB / Neo4j / LLM round-trip).
    """
    if not draft:
        return []
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    issues.extend(_scan_patterns(draft, _SELF_REFERENCE_PATTERNS, "self_reference", seen))
    issues.extend(_scan_patterns(draft, _VAGUE_TEMPLATES, "vague_threat", seen))
    issues.extend(_scan_patterns(draft, _TAUTOLOGY_PATTERNS, "tautology", seen))
    return issues
