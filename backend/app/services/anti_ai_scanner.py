"""v0.8 — Anti-AI scan.

Loads active rows from ``anti_ai_traps`` and matches them against a draft.
Each matcher is deliberately tolerant of malformed regex / ngram configs so
that one bad trap cannot break an entire critic pass.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.writing_engine import AntiAITrap

logger = logging.getLogger(__name__)


def _match_keyword(pattern: str, draft: str) -> list[str]:
    if not pattern:
        return []
    return [pattern] if pattern in draft else []


def _match_regex(pattern: str, draft: str) -> list[str]:
    try:
        hits = re.findall(pattern, draft)
    except re.error as exc:
        logger.debug("anti_ai_scanner invalid regex %r: %s", pattern, exc)
        return []
    out: list[str] = []
    for h in hits:
        if isinstance(h, tuple):
            h = next((x for x in h if x), "")
        if isinstance(h, str) and h:
            out.append(h)
    return out


def _match_ngram(pattern: str, draft: str) -> list[str]:
    """Pipe-separated phrase list; any phrase present counts as a hit."""
    phrases = [p.strip() for p in pattern.split("|") if p.strip()]
    return [p for p in phrases if p in draft]


async def scan_anti_ai(
    draft: str,
    db: AsyncSession,
    *,
    locale: str = "zh-CN",
) -> list[dict[str, Any]]:
    """Return a list of critic issues for every active anti-AI trap that matches.

    Each issue has ``severity`` either ``hard`` (force a rewrite) or ``soft``
    (LLM critic may weigh it) and a ``category`` of ``anti_ai``.
    """
    if not draft:
        return []
    rows = await db.execute(
        select(AntiAITrap).where(
            AntiAITrap.is_active.is_(True),
            AntiAITrap.locale == locale,
        )
    )
    traps: Iterable[AntiAITrap] = rows.scalars().all()

    issues: list[dict[str, Any]] = []
    for trap in traps:
        if trap.pattern_type == "keyword":
            hits = _match_keyword(trap.pattern, draft)
        elif trap.pattern_type == "regex":
            hits = _match_regex(trap.pattern, draft)
        elif trap.pattern_type == "ngram":
            hits = _match_ngram(trap.pattern, draft)
        else:
            continue
        if not hits:
            continue
        # De-duplicate within a single trap to keep the report compact.
        seen: set[str] = set()
        unique = [h for h in hits if not (h in seen or seen.add(h))]
        sample = unique[:3]
        desc = f"命中 AI 味模式 [{trap.pattern_type}] {trap.pattern!r}: {sample}"
        if trap.replacement_hint:
            desc += f"  |  改写提示：{trap.replacement_hint}"
        issues.append(
            {
                "severity": trap.severity,
                "category": "anti_ai",
                "desc": desc,
                "location": "",
                "source": "anti_ai_scanner",
                "trap_id": str(trap.id),
            }
        )
    return issues
