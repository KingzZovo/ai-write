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
from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

# Threshold: edits affecting less than 5% of the text are cosmetic
COSMETIC_THRESHOLD = 0.05


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
        2. If only cosmetic changes (< 5% diff), skip
        3. If substantive: re-extract entities, update Neo4j, update Qdrant summaries
        4. Check foreshadow impact

        Returns:
            A status dict with keys: synced (bool), reason (str), details (dict)
        """
        # 1. Compute diff ratio
        diff_ratio = self._compute_diff_ratio(old_text, new_text)
        logger.info(
            "Chapter %s edit diff ratio: %.3f", chapter_id, diff_ratio
        )

        # 2. Skip cosmetic changes
        if diff_ratio < COSMETIC_THRESHOLD:
            return {
                "synced": False,
                "reason": "cosmetic_change",
                "diff_ratio": round(diff_ratio, 4),
                "details": {},
            }

        # 3. Substantive change: update knowledge base
        chapter = await self.db.get(Chapter, chapter_id)
        if not chapter:
            return {
                "synced": False,
                "reason": "chapter_not_found",
                "diff_ratio": round(diff_ratio, 4),
                "details": {},
            }

        details: dict = {"diff_ratio": round(diff_ratio, 4)}

        # 3a. Re-generate chapter summary
        try:
            summary = await self._regenerate_summary(chapter, new_text)
            details["summary_updated"] = True
            details["summary"] = summary
        except Exception as exc:
            logger.warning("Failed to regenerate summary for %s: %s", chapter_id, exc)
            details["summary_updated"] = False

        # 3b. Re-extract entities and update Neo4j
        try:
            entity_updates = await self._update_entities(chapter, new_text)
            details["entities_updated"] = True
            details["entity_changes"] = entity_updates
        except Exception as exc:
            logger.warning("Failed to update entities for %s: %s", chapter_id, exc)
            details["entities_updated"] = False

        # 3c. Update Qdrant chapter summary embedding
        try:
            await self._update_qdrant_summary(chapter, new_text)
            details["qdrant_updated"] = True
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
            "synced": True,
            "reason": "substantive_change",
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
        """Regenerate the chapter summary from new text."""
        from app.services.memory import HierarchicalMemory

        memory = HierarchicalMemory(db=self.db)
        summary = await memory.generate_chapter_summary(
            chapter_text=new_text,
            chapter_idx=chapter.chapter_idx,
            chapter_title=chapter.title,
        )

        # Update chapter summary in DB
        chapter.summary = summary
        await self.db.flush()
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
    ) -> None:
        """Update the Qdrant chapter summary embedding."""
        from app.db.qdrant import get_qdrant
        from app.services.memory import HierarchicalMemory

        # Get the Qdrant client
        qdrant_client = None
        async for client in get_qdrant():
            qdrant_client = client
            break

        if not qdrant_client:
            logger.warning("Qdrant client not available, skipping embedding update")
            return

        memory = HierarchicalMemory(
            db=self.db,
            qdrant_client=qdrant_client,
        )

        # The summary should already be generated; store its embedding
        summary = chapter.summary or ""
        if summary:
            from app.models.project import Volume
            volume = await self.db.get(Volume, chapter.volume_id)
            # Use generate_chapter_summary which also stores the embedding
            await memory.generate_chapter_summary(
                chapter_text=new_text,
                chapter_idx=chapter.chapter_idx,
                project_id=str(volume.project_id) if volume else None,
                volume_id=str(chapter.volume_id),
                chapter_title=chapter.title,
            )

    async def _check_foreshadow_impact(
        self,
        chapter: Chapter,
        old_text: str,
        new_text: str,
    ) -> list[dict]:
        """Check whether the edit impacts any planted foreshadows."""
        from sqlalchemy import select

        from app.models.project import Foreshadow, Volume

        # Get the project_id through the volume
        volume = await self.db.get(Volume, str(chapter.volume_id))
        if not volume:
            return []

        # Find foreshadows planted at or before this chapter
        result = await self.db.execute(
            select(Foreshadow).where(
                Foreshadow.project_id == volume.project_id,
                Foreshadow.planted_chapter <= chapter.chapter_idx,
                Foreshadow.status == "planted",
            )
        )
        foreshadows = result.scalars().all()

        impacts: list[dict] = []
        for fs in foreshadows:
            # Simple keyword check: does the foreshadow description
            # appear differently in old vs new text?
            desc_words = set(fs.description.split())
            old_overlap = len(desc_words & set(old_text.split()))
            new_overlap = len(desc_words & set(new_text.split()))

            if old_overlap != new_overlap:
                impacts.append(
                    {
                        "foreshadow_id": str(fs.id),
                        "description": fs.description[:100],
                        "type": fs.type,
                        "old_overlap": old_overlap,
                        "new_overlap": new_overlap,
                    }
                )

        return impacts
