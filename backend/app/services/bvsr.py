"""Rank fully reviewed drafts as starting points for revision.

Ordering is lexicographic (hard, soft); hard conflicts cannot be averaged
away. The old numeric score remains a diagnostic for compatible readers,
without pattern-match penalties. Unreviewed/stale drafts have a null score
and no automatic rank; all delivered candidates remain available to authors.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, ChapterVariant
from app.services.review_contract import COMPLETE, critic_review_status

logger = logging.getLogger(__name__)

# Compatibility diagnostic, not the ranking order or literary acceptance.
W_HARD = 10.0
W_SOFT = 2.0


@dataclass
class VariantCandidate:
    variant_idx: int
    content_text: str
    critic_report: dict[str, Any]

    @property
    def review_status(self) -> str:
        return critic_review_status(self.critic_report, self.content_text)

    @property
    def eligible(self) -> bool:
        return self.review_status == COMPLETE

    @property
    def word_count(self) -> int:
        return len(self.content_text)

    @property
    def hard_count(self) -> int:
        return self._issue_count('hard')

    @property
    def soft_count(self) -> int:
        return self._issue_count('soft')

    def _issue_count(self, severity: str) -> int:
        issues = self.critic_report.get('issues') if isinstance(self.critic_report, dict) else None
        if not isinstance(issues, list):
            return 0
        return sum(isinstance(i, dict) and i.get('severity') == severity for i in issues)

    @property
    def ai_trap_count(self) -> int:
        observations = self.critic_report.get('observations', []) if isinstance(self.critic_report, dict) else []
        return len(observations) if isinstance(observations, list) else 0

    @property
    def score(self) -> float | None:
        # Compatibility diagnostic only, never a literary acceptance score.
        if not self.eligible:
            return None
        return self.hard_count * W_HARD + self.soft_count * W_SOFT


def is_bvsr_enabled() -> bool:
    return os.getenv("BVSR_ENABLED", "false").lower() in ("true", "1", "yes")


def bvsr_n() -> int:
    try:
        n = int(os.getenv("BVSR_N", "3"))
    except ValueError:
        n = 3
    return max(1, min(n, 8))  # sanity clamp: 1..8


def rank(candidates: Sequence[VariantCandidate]) -> list[VariantCandidate]:
    """Rank only fully reviewed drafts; a hard issue cannot be averaged away."""
    return sorted((c for c in candidates if c.eligible), key=lambda c: (c.hard_count, c.soft_count))


async def _lock_variant_chapter(db: AsyncSession, chapter_id: uuid.UUID | str) -> None:
    """All candidate writers acquire the parent before touching child rows.

    This also precedes INSERT foreign-key locks, avoiding a refresh/apply
    cycle between an existing candidate and its chapter. No body is loaded.
    """
    parent = (await db.execute(
        select(Chapter.id).where(Chapter.id == uuid.UUID(str(chapter_id)))
        .with_for_update().execution_options(autoflush=False)
    )).scalar_one_or_none()
    if parent is None:
        raise ValueError('variant_chapter_not_found')


async def persist_variants(
    db: AsyncSession,
    *,
    chapter_id: uuid.UUID | str,
    run_id: uuid.UUID | str | None,
    candidates: Sequence[VariantCandidate],
    winner_idx: int | None,
    commit: bool = False,
    refresh_existing: bool = False,
) -> list[ChapterVariant]:
    """Store delivered variants; on retry, refresh only the same saved texts.

    Review refreshes preserve stable IDs and author decisions. The caller
    keeps earlier review receipts separately in critic_reports.
    """
    if winner_idx is not None and not any(c.variant_idx == winner_idx and c.eligible for c in candidates):
        raise ValueError('unreviewed_variant_cannot_be_selected_automatically')
    await _lock_variant_chapter(db, chapter_id)
    existing = {}
    if refresh_existing:
        if run_id is None:
            raise ValueError('variant_refresh_requires_run')
        result = await db.execute(
            select(ChapterVariant)
            .where(ChapterVariant.chapter_id == chapter_id, ChapterVariant.run_id == run_id)
            .order_by(ChapterVariant.variant_idx)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing = {row.variant_idx: row for row in result.scalars().all()}
        candidates_by_index = {c.variant_idx: c for c in candidates}
        for index, row in existing.items():
            candidate = candidates_by_index.get(index)
            if candidate is None or row.content_text != candidate.content_text:
                raise ValueError('saved_variant_content_changed')
    choices = [row for row in existing.values() if row.selected_by_user]
    if len(choices) > 1:
        raise ValueError('variant_author_choice_conflict')
    effective_winner = choices[0].variant_idx if choices else winner_idx
    out: list[ChapterVariant] = []
    for c in candidates:
        row = existing.get(c.variant_idx)
        if row is None:
            row = ChapterVariant(
                chapter_id=chapter_id, run_id=run_id, variant_idx=c.variant_idx,
                content_text=c.content_text, word_count=c.word_count, selected_by_user=False,
            )
            db.add(row)
        row.score = c.score
        row.hard_count = c.hard_count
        row.soft_count = c.soft_count
        row.ai_trap_count = c.ai_trap_count
        row.critic_report_json = c.critic_report or {}
        row.is_winner = c.variant_idx == effective_winner
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
    chapter_id = (await db.execute(select(ChapterVariant.chapter_id)
        .where(ChapterVariant.id == variant_id).execution_options(autoflush=False))).scalar_one_or_none()
    if chapter_id is None:
        raise ValueError(f"chapter_variant {variant_id} not found")
    await _lock_variant_chapter(db, chapter_id)
    variant = (await db.execute(select(ChapterVariant)
        .where(ChapterVariant.id == variant_id, ChapterVariant.chapter_id == chapter_id)
        .execution_options(populate_existing=True, autoflush=False))).scalar_one_or_none()
    if variant is None:
        raise ValueError(f"chapter_variant {variant_id} not found")

    await db.execute(
        update(ChapterVariant)
        .where(ChapterVariant.chapter_id == chapter_id)
        .values(is_winner=False, selected_by_user=False)
    )
    variant.is_winner = True
    variant.selected_by_user = True
    await db.flush()
    return variant
