"""BVSR — Branch / Variants / Score / Rank (v1.0 chunk 7).

Given N candidate drafts + N critic reports, scores each variant with:

    score = hard * 10 + soft * 2 + ai_trap * 5

Lower is better. The lowest-scoring variant becomes the "winner" used for the
subsequent rewrite phase; the remaining variants are persisted in
``chapter_variants`` so the author can review or manually select a different
one.

The BVSR loop itself lives in :mod:`app.services.generation_runner`; this
module only contains the pure scoring + persistence helpers.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import ChapterVariant

logger = logging.getLogger(__name__)

# Scoring weights (V10_DESIGN §2: "hard × 10 + soft × 2 + ai_trap × 5").
W_HARD = 10.0
W_SOFT = 2.0
W_AI_TRAP = 5.0


@dataclass
class VariantCandidate:
    variant_idx: int
    content_text: str
    critic_report: dict[str, Any]

    @property
    def word_count(self) -> int:
        return len(self.content_text)

    @property
    def hard_count(self) -> int:
        return int(self.critic_report.get("hard_count", 0) or 0)

    @property
    def soft_count(self) -> int:
        return int(self.critic_report.get("soft_count", 0) or 0)

    @property
    def ai_trap_count(self) -> int:
        return int(self.critic_report.get("ai_trap_count", 0) or 0)

    @property
    def score(self) -> float:
        return (
            self.hard_count * W_HARD
            + self.soft_count * W_SOFT
            + self.ai_trap_count * W_AI_TRAP
        )


def is_bvsr_enabled() -> bool:
    return os.getenv("BVSR_ENABLED", "false").lower() in ("true", "1", "yes")


def bvsr_n() -> int:
    try:
        n = int(os.getenv("BVSR_N", "3"))
    except ValueError:
        n = 3
    return max(1, min(n, 8))  # sanity clamp: 1..8


def rank(candidates: Sequence[VariantCandidate]) -> list[VariantCandidate]:
    """Return candidates sorted ascending by score (best first)."""
    return sorted(candidates, key=lambda c: c.score)


async def persist_variants(
    db: AsyncSession,
    *,
    chapter_id: uuid.UUID | str,
    run_id: uuid.UUID | str | None,
    candidates: Sequence[VariantCandidate],
    winner_idx: int,
    commit: bool = False,
) -> list[ChapterVariant]:
    """Store all N variants (including the winner) for later inspection."""
    out: list[ChapterVariant] = []
    for c in candidates:
        row = ChapterVariant(
            chapter_id=chapter_id,
            run_id=run_id,
            variant_idx=c.variant_idx,
            content_text=c.content_text,
            word_count=c.word_count,
            score=c.score,
            hard_count=c.hard_count,
            soft_count=c.soft_count,
            ai_trap_count=c.ai_trap_count,
            critic_report_json=c.critic_report or {},
            is_winner=(c.variant_idx == winner_idx),
            selected_by_user=False,
        )
        db.add(row)
        out.append(row)
    await db.flush()
    if commit:
        await db.commit()
    return out


async def select_variant(
    db: AsyncSession,
    *,
    variant_id: uuid.UUID | str,
) -> ChapterVariant:
    """Mark the given variant as the user-selected winner for its chapter.

    Clears is_winner/selected_by_user flags on the chapter's other variants.
    Does NOT modify chapter.content_text; the caller is responsible for that
    (it may want to go through a rewrite phase first).
    """
    res = await db.execute(
        select(ChapterVariant).where(ChapterVariant.id == variant_id)
    )
    variant = res.scalar_one_or_none()
    if variant is None:
        raise ValueError(f"chapter_variant {variant_id} not found")
    chapter_id = variant.chapter_id

    await db.execute(
        update(ChapterVariant)
        .where(ChapterVariant.chapter_id == chapter_id)
        .values(is_winner=False, selected_by_user=False)
    )
    variant.is_winner = True
    variant.selected_by_user = True
    await db.flush()
    return variant
