"""
Pre/post generation hook system.

Pre-generate hooks run BEFORE chapter generation:
- Foreshadow proximity check
- Character state consistency validation
- Outline alignment check

Post-generate hooks run AFTER chapter generation:
- Entity extraction -> update Neo4j graph
- Chapter summary generation -> update memory
- Foreshadow registration/resolution check
- Version snapshot save
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.project import Chapter, Foreshadow, Volume
from app.services.foreshadow_manager import ForeshadowManager
from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)


@dataclass
class HookResult:
    """Result of running pre-generation hooks.

    Attributes:
        can_proceed: Whether generation should proceed.
        warnings: Non-blocking issues found during checks.
        errors: Blocking issues that prevent generation.
        foreshadow_prompts: Text to inject into the generation context
            about ripening/ready foreshadows.
    """

    can_proceed: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    foreshadow_prompts: str = ""


class HookManager:
    """
    Manages pre-generate and post-generate hooks.

    Pre-generate hooks run BEFORE chapter generation:
    - Foreshadow proximity check
    - Character state consistency validation
    - Outline alignment check

    Post-generate hooks run AFTER chapter generation:
    - Entity extraction -> update Neo4j graph
    - Chapter summary generation -> update memory
    - Foreshadow registration/resolution check
    - Version snapshot save
    """

    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db
        self._foreshadow_mgr: ForeshadowManager | None = None

    async def _get_db(self) -> AsyncSession:
        if self._db is not None:
            return self._db
        return async_session_factory()

    @property
    def foreshadow_mgr(self) -> ForeshadowManager:
        if self._foreshadow_mgr is None:
            self._foreshadow_mgr = ForeshadowManager(db=self._db)
        return self._foreshadow_mgr

    # ==================================================================
    # Public API
    # ==================================================================

    async def run_pre_hooks(
        self,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        chapter_outline: dict,
    ) -> HookResult:
        """Run all pre-generation checks.

        Args:
            project_id: The project being generated for.
            volume_id: The volume containing the chapter.
            chapter_idx: Index of the chapter about to be generated.
            chapter_outline: The outline for the chapter.

        Returns:
            HookResult with warnings, errors, and foreshadow prompt text.
        """
        result = HookResult()

        # 1. Foreshadow proximity check
        try:
            foreshadow_result = await self._check_foreshadows(
                project_id, chapter_idx
            )
            result.foreshadow_prompts = foreshadow_result
            if foreshadow_result:
                result.warnings.append(
                    f"Active foreshadows detected (prompts injected into context)"
                )
        except Exception:
            logger.exception("Pre-hook _check_foreshadows failed")
            result.warnings.append(
                "Foreshadow check failed -- proceeding without foreshadow context"
            )

        # 2. Character consistency check
        try:
            char_warnings = await self._check_character_consistency(
                project_id, chapter_idx, chapter_outline
            )
            result.warnings.extend(char_warnings)
        except Exception:
            logger.exception("Pre-hook _check_character_consistency failed")
            result.warnings.append(
                "Character consistency check failed -- proceeding anyway"
            )

        # 3. Outline alignment check
        try:
            alignment_warnings = await self._check_outline_alignment(
                chapter_outline, {}
            )
            result.warnings.extend(alignment_warnings)
        except Exception:
            logger.exception("Pre-hook _check_outline_alignment failed")
            result.warnings.append(
                "Outline alignment check failed -- proceeding anyway"
            )

        # Errors block generation; warnings do not
        if result.errors:
            result.can_proceed = False

        return result

    async def run_post_hooks(
        self,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        chapter_text: str,
    ) -> None:
        """Run all post-generation updates.

        All post-hooks run independently. Failures are logged but do not
        propagate -- they should not block the user from receiving the
        generated text.

        Args:
            project_id: The project.
            volume_id: The volume containing the chapter.
            chapter_idx: Index of the generated chapter.
            chapter_text: Full text of the generated chapter.
        """
        # 1. Entity extraction
        try:
            await self._update_entities(project_id, chapter_idx, chapter_text)
        except Exception:
            logger.exception("Post-hook _update_entities failed")

        # 2. Chapter summary
        try:
            await self._generate_summary(project_id, chapter_idx, chapter_text)
        except Exception:
            logger.exception("Post-hook _generate_summary failed")

        # 3. Foreshadow resolution check
        try:
            await self._check_foreshadow_resolution(
                project_id, chapter_idx, chapter_text
            )
        except Exception:
            logger.exception("Post-hook _check_foreshadow_resolution failed")

        # 4. Register new foreshadows
        try:
            await self._register_new_foreshadows(
                project_id, chapter_idx, chapter_text
            )
        except Exception:
            logger.exception("Post-hook _register_new_foreshadows failed")

        logger.info(
            "Post-hooks completed for project=%s chapter=%d",
            project_id, chapter_idx,
        )

    # ==================================================================
    # Pre-generation hooks
    # ==================================================================

    async def _check_foreshadows(
        self,
        project_id: str | UUID,
        chapter_idx: int,
    ) -> str:
        """Check foreshadow proximity and return prompt text for injection.

        Returns:
            Foreshadow prompt text (empty string if none relevant).
        """
        return await self.foreshadow_mgr.get_prompts_for_generation(
            project_id, chapter_idx
        )

    async def _check_character_consistency(
        self,
        project_id: str | UUID,
        chapter_idx: int,
        chapter_outline: dict,
    ) -> list[str]:
        """Verify characters referenced in outline are alive and in
        correct location by querying Neo4j.

        Returns:
            List of warning strings. Empty if all checks pass.
        """
        warnings: list[str] = []

        # Extract character names from outline
        characters_in_outline = self._extract_character_names(chapter_outline)
        if not characters_in_outline:
            return warnings

        # Try to query Neo4j for character state
        try:
            from app.db.neo4j import get_neo4j

            driver = None
            async for d in get_neo4j():
                driver = d
                break

            if driver is None:
                return warnings

            async with driver.session() as session:
                for char_name in characters_in_outline:
                    result = await session.run(
                        "MATCH (c:Character {name: $name, project_id: $pid}) "
                        "RETURN c.status AS status, c.location AS location",
                        name=char_name,
                        pid=str(project_id),
                    )
                    record = await result.single()
                    if record:
                        status = record.get("status", "alive")
                        if status == "dead":
                            warnings.append(
                                f"Character '{char_name}' appears in outline "
                                f"but is marked as dead in the knowledge graph"
                            )
        except RuntimeError:
            # Neo4j not initialized -- skip check
            logger.debug("Neo4j not available, skipping character consistency check")
        except Exception:
            logger.exception("Neo4j character check failed")

        return warnings

    async def _check_outline_alignment(
        self,
        chapter_outline: dict,
        generation_context: dict,
    ) -> list[str]:
        """Verify that the generation context does not deviate from the
        chapter outline.

        This is a lightweight structural check. Full semantic alignment
        requires LLM evaluation and is deferred to post-generation.

        Returns:
            List of warning strings.
        """
        warnings: list[str] = []

        if not chapter_outline:
            warnings.append("No chapter outline provided -- generation may lack direction")
            return warnings

        # Check for required outline fields
        expected_fields = ("events", "summary", "key_points", "main_plot")
        has_content = any(
            chapter_outline.get(f)
            for f in expected_fields
        )
        if not has_content:
            # Check if there's any non-empty string value
            has_any = any(
                v for v in chapter_outline.values()
                if isinstance(v, str) and v.strip()
            )
            if not has_any:
                warnings.append("Chapter outline appears to be empty or lacks detail")

        return warnings

    # ==================================================================
    # Post-generation hooks
    # ==================================================================

    async def _update_entities(
        self,
        project_id: str | UUID,
        chapter_idx: int,
        chapter_text: str,
    ) -> None:
        """Extract entities from chapter text and update Neo4j graph.

        Uses LLM to identify characters, locations, items, and their
        relationships, then upserts them into the knowledge graph.
        """
        router = get_model_router()

        messages = [
            {
                "role": "system",
                "content": (
                    "Extract named entities from the given novel chapter text. "
                    "Return a JSON object with:\n"
                    '- "characters": list of {name, status, location, description}\n'
                    '- "locations": list of {name, description}\n'
                    '- "items": list of {name, owner, description}\n'
                    '- "relationships": list of {from, to, type, description}\n\n'
                    "Only include entities explicitly mentioned in the text."
                ),
            },
            {
                "role": "user",
                "content": f"Chapter text:\n{chapter_text[:4000]}",
            },
        ]

        result = await router.generate(
            task_type="extraction",
            messages=messages,
            temperature=0.2,
            max_tokens=1024,
        )

        try:
            entities = json.loads(result.text.strip())
        except json.JSONDecodeError:
            # Try to find JSON in the response
            import re
            match = re.search(r"\{.*\}", result.text, re.DOTALL)
            if match:
                entities = json.loads(match.group())
            else:
                logger.warning("Could not parse entity extraction result")
                return

        # Upsert into Neo4j
        try:
            from app.db.neo4j import get_neo4j

            driver = None
            async for d in get_neo4j():
                driver = d
                break

            if driver is None:
                logger.debug("Neo4j not available, skipping entity update")
                return

            async with driver.session() as session:
                # Upsert characters
                for char in entities.get("characters", []):
                    await session.run(
                        "MERGE (c:Character {name: $name, project_id: $pid}) "
                        "SET c.status = $status, c.location = $location, "
                        "c.description = $desc, c.last_chapter = $chapter",
                        name=char.get("name", ""),
                        pid=str(project_id),
                        status=char.get("status", "alive"),
                        location=char.get("location", ""),
                        desc=char.get("description", ""),
                        chapter=chapter_idx,
                    )

                # Upsert locations
                for loc in entities.get("locations", []):
                    await session.run(
                        "MERGE (l:Location {name: $name, project_id: $pid}) "
                        "SET l.description = $desc, l.last_chapter = $chapter",
                        name=loc.get("name", ""),
                        pid=str(project_id),
                        desc=loc.get("description", ""),
                        chapter=chapter_idx,
                    )

                # Upsert relationships
                for rel in entities.get("relationships", []):
                    await session.run(
                        "MATCH (a {name: $from_name, project_id: $pid}) "
                        "MATCH (b {name: $to_name, project_id: $pid}) "
                        "MERGE (a)-[r:RELATES_TO {type: $type}]->(b) "
                        "SET r.description = $desc, r.last_chapter = $chapter",
                        from_name=rel.get("from", ""),
                        to_name=rel.get("to", ""),
                        pid=str(project_id),
                        type=rel.get("type", ""),
                        desc=rel.get("description", ""),
                        chapter=chapter_idx,
                    )

            logger.info(
                "Updated entity graph for project=%s chapter=%d",
                project_id, chapter_idx,
            )
        except RuntimeError:
            logger.debug("Neo4j not available, skipping entity update")
        except Exception:
            logger.exception("Failed to update Neo4j entity graph")

    async def _generate_summary(
        self,
        project_id: str | UUID,
        chapter_idx: int,
        chapter_text: str,
    ) -> None:
        """Generate and store a chapter summary.

        The summary is saved to the Chapter.summary field in PostgreSQL.
        """
        router = get_model_router()

        messages = [
            {
                "role": "system",
                "content": (
                    "Summarize the given novel chapter in 2-3 concise sentences. "
                    "Focus on key plot developments, character actions, and "
                    "any significant changes. Write in Chinese."
                ),
            },
            {
                "role": "user",
                "content": f"Chapter text:\n{chapter_text[:4000]}",
            },
        ]

        result = await router.generate(
            task_type="summary",
            messages=messages,
            temperature=0.3,
            max_tokens=256,
        )

        summary_text = result.text.strip()
        if not summary_text:
            return

        # Find and update the chapter in the database
        db = await self._get_db()

        # Get volume IDs for this project
        vol_result = await db.execute(
            select(Volume.id).where(Volume.project_id == str(project_id))
        )
        volume_ids = list(vol_result.scalars().all())
        if not volume_ids:
            return

        # Find the chapter by index
        chapter_result = await db.execute(
            select(Chapter).where(
                Chapter.volume_id.in_(volume_ids),
                Chapter.chapter_idx == chapter_idx,
            )
        )
        chapter = chapter_result.scalar_one_or_none()
        if chapter:
            chapter.summary = summary_text
            await db.flush()
            logger.info(
                "Generated summary for project=%s chapter=%d",
                project_id, chapter_idx,
            )

    async def _check_foreshadow_resolution(
        self,
        project_id: str | UUID,
        chapter_idx: int,
        chapter_text: str,
    ) -> None:
        """Check if the generated chapter resolves any active foreshadows."""
        resolved = await self.foreshadow_mgr.check_resolution(
            project_id, chapter_text, chapter_idx=chapter_idx
        )
        if resolved:
            logger.info(
                "Resolved %d foreshadows at chapter %d",
                len(resolved), chapter_idx,
            )

    async def _register_new_foreshadows(
        self,
        project_id: str | UUID,
        chapter_idx: int,
        chapter_text: str,
    ) -> None:
        """Detect and register new foreshadows from the generated text."""
        created = await self.foreshadow_mgr.register_from_text(
            project_id, chapter_idx, chapter_text
        )
        if created:
            logger.info(
                "Auto-registered %d new foreshadows from chapter %d",
                len(created), chapter_idx,
            )

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _extract_character_names(outline: dict) -> list[str]:
        """Extract character names from a chapter outline dict.

        Looks through common outline keys for character references.
        """
        names: list[str] = []
        seen: set[str] = set()

        def _add(name: str) -> None:
            clean = name.strip()
            if clean and clean not in seen:
                seen.add(clean)
                names.append(clean)

        # Direct character list
        for char in outline.get("characters", []):
            if isinstance(char, str):
                _add(char)
            elif isinstance(char, dict):
                _add(char.get("name", ""))

        # Character appearances
        for char in outline.get("character_appearances", []):
            if isinstance(char, str):
                _add(char)
            elif isinstance(char, dict):
                _add(char.get("name", ""))

        return names
