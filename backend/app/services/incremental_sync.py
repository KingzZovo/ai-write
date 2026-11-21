"""
Incremental Sync Service

When user edits chapter content, detects substantive changes
and updates the knowledge base accordingly.
"""

from __future__ import annotations

import difflib
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter

logger = logging.getLogger(__name__)


class IncrementalSyncService:
    """Detects substantive edits and syncs the knowledge base."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def process_edit(
        self,
        chapter_id: str,
        old_text: str,
        new_text: str,
    ) -> dict:
        """
        Process a user edit and update knowledge base if substantive.

        Steps:
        1. Diff old vs new
        2. Skip identical text; even a one-character edit can change a fact.
        3. Refresh summary and vector projection; extract entity diagnostics.
        4. Check foreshadow impact

        Returns:
            A status dict with keys: synced (bool), reason (str), details (dict)
        """
        # 1. Compute diff ratio
        diff_ratio = self._compute_diff_ratio(old_text, new_text)
        logger.info(
            "Chapter %s edit diff ratio: %.3f", chapter_id, diff_ratio
        )

        # Small edits can reverse a fact or change an amount.
        if old_text == new_text:
            return {
                "synced": False,
                "reason": "unchanged",
                "diff_ratio": round(diff_ratio, 4),
                "details": {},
            }

        # 3. Substantive change: update knowledge base
        with self.db.no_autoflush:
            chapter = await self.db.get(Chapter, chapter_id)
        if not chapter:
            return {
                "synced": False,
                "reason": "chapter_not_found",
                "diff_ratio": round(diff_ratio, 4),
                "details": {},
            }

        # The sync API does not save author text. Never summarize an unsaved edit.
        from sqlalchemy import select
        current = await self.db.scalar(select(Chapter.content_text).where(
            Chapter.id == chapter.id,
        ).execution_options(autoflush=False))
        if chapter in self.db.dirty or current != new_text or chapter.content_text != new_text:
            return {"synced": False, "reason": "source_mismatch",
                    "diff_ratio": round(diff_ratio, 4), "details": {}}

        details: dict = {"diff_ratio": round(diff_ratio, 4)}

        # 3a. Re-generate chapter summary
        try:
            summary = await self._regenerate_summary(chapter, new_text)
            details["summary_updated"] = bool(summary)
            details["summary"] = summary
        except Exception as exc:
            logger.warning("Failed to regenerate summary for %s: %s", chapter_id, exc)
            details["summary_updated"] = False

        if not details["summary_updated"]:
            return {"synced": False, "reason": "summary_failed_or_stale",
                    "diff_ratio": round(diff_ratio, 4), "details": details}

        # 3b. Extraction is diagnostic only until versioned Neo4j projection exists.
        # 2026-11-17 审计:抽取结果只进日志,却每改一次稿就付一次模型调用。
        # 默认关闭,诊断时用 INCREMENTAL_SYNC_ENTITY_PROBE=1 打开。
        from app.config import settings as _settings

        if bool(getattr(_settings, "INCREMENTAL_SYNC_ENTITY_PROBE", False)):
            try:
                entity_updates = await self._update_entities(chapter, new_text)
                details["entities_updated"] = False
                details["entities_extracted"] = True
                details["entity_changes"] = entity_updates
            except Exception as exc:
                logger.warning("Failed to update entities for %s: %s", chapter_id, exc)
                details["entities_updated"] = False
        else:
            details["entities_updated"] = False
            details["entities_extracted"] = False

        # 3c. Update Qdrant chapter summary embedding
        try:
            details["qdrant_updated"] = await self._update_qdrant_summary(chapter, new_text)
        except Exception as exc:
            logger.warning("Failed to update Qdrant for %s: %s", chapter_id, exc)
            details["qdrant_updated"] = False

        # 4. Check foreshadow impact
        try:
            foreshadow_impact = await self._check_foreshadow_impact(
                chapter, old_text, new_text
            )
            details["foreshadow_impact"] = foreshadow_impact
        except Exception as exc:
            logger.warning(
                "Failed to check foreshadow impact for %s: %s", chapter_id, exc
            )
            details["foreshadow_impact"] = []

        return {
            "synced": False,
            "reason": "partial_sync",
            "diff_ratio": round(diff_ratio, 4),
            "details": details,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_diff_ratio(old_text: str, new_text: str) -> float:
        """Compute the ratio of changed characters between old and new text."""
        if not old_text and not new_text:
            return 0.0
        if not old_text or not new_text:
            return 1.0

        matcher = difflib.SequenceMatcher(None, old_text, new_text)
        similarity = matcher.ratio()
        return 1.0 - similarity

    async def _regenerate_summary(
        self, chapter: Chapter, new_text: str
    ) -> str:
        """Commit a summary bound to the already saved edit, then project it."""
        from app.services.chapter_summarizer import summarize_and_save_chapter

        _, summary = await summarize_and_save_chapter(
            chapter_id=chapter.id, db=self.db, overwrite=True,
            project_vectors=False, expected_content=new_text,
        )
        return summary

    async def _update_entities(
        self, chapter: Chapter, new_text: str
    ) -> list[str]:
        """Re-extract entities from the new text and return list of changes."""
        from app.services.feature_extractor import PlotExtractor

        extractor = PlotExtractor()
        features = await extractor.extract(new_text)

        # Return the entity changes for logging
        changes: list[str] = []
        if features.characters:
            changes.append(f"characters: {', '.join(features.characters)}")
        if features.events:
            changes.append(f"events: {len(features.events)} detected")
        if features.locations:
            changes.append(f"locations: {', '.join(features.locations)}")

        return changes

    async def _update_qdrant_summary(
        self, chapter: Chapter, new_text: str
    ) -> bool:
        """Project the exact committed summary; never generate a second one."""
        from sqlalchemy import select
        from app.models.project import Volume
        from app.services.chapter_summarizer import upsert_chapter_summary_embedding
        from app.services.summary_provenance import current_summary_sql

        row = (await self.db.execute(select(
            Chapter.id, Chapter.volume_id, Chapter.chapter_idx, Chapter.global_idx,
            Chapter.title, current_summary_sql().label("summary"),
            Chapter.summary_source_hash, Volume.project_id, Volume.volume_idx,
        ).join(Volume, Chapter.volume_id == Volume.id).where(
            Chapter.id == chapter.id, Chapter.content_text == new_text,
            Chapter.summary == chapter.summary,
            Chapter.summary_source_hash == chapter.summary_source_hash,
        ).execution_options(autoflush=False))).one_or_none()
        if row is None or not row.summary:
            return False
        return await upsert_chapter_summary_embedding(
            project_id=row.project_id, volume_id=row.volume_id,
            chapter_idx=row.chapter_idx, global_idx=row.global_idx,
            chapter_title=row.title, summary=row.summary, volume_idx=row.volume_idx,
            chapter_id=row.id, source_hash=row.summary_source_hash,
        )

    async def _check_foreshadow_impact(
        self,
        chapter: Chapter,
        old_text: str,
        new_text: str,
    ) -> list[dict]:
        """Report changed source bindings; word overlap is not literary impact.

        Only this chapter's retained author registrations identify related
        clues. A full-prose change invalidates the old source snapshot but
        does not decide whether its narrative meaning changed.
        """
        from sqlalchemy import select
        from app.models.project import Foreshadow, Volume, ForeshadowDiscovery as Candidate, ForeshadowDiscoveryRun as Run
        from app.services.foreshadow_sources import inspect_foreshadow_sources

        volume = await self.db.get(Volume, chapter.volume_id)
        if volume is None:
            return []
        registered = select(Candidate.foreshadow_id).join(Run, Candidate.run_id == Run.id).where(
            Run.project_id == volume.project_id, Run.chapter_id == chapter.id, Candidate.status == 'accepted')
        records = list((await self.db.execute(select(*Foreshadow.__table__.columns).where(
            Foreshadow.project_id == volume.project_id, Foreshadow.id.in_(registered))
            .execution_options(autoflush=False))).mappings().all())
        sources = await inspect_foreshadow_sources(self.db, volume.project_id, records)
        return [{
            'foreshadow_id': str(row['id']), 'description': row['description'], 'type': row['type'],
            'source_state': sources[str(row['id'])]['source_state'],
            'reason': '登记时的正文、位置或描述需要重新核对；尚未判断剧情含义是否变化。',
        } for row in records if sources[str(row['id'])]['source_state'] != 'current']
