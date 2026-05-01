"""
Condition-triggered foreshadow management system.

Foreshadows do NOT have hard chapter deadlines. Instead they use:
- resolve_conditions: descriptions of WHEN the foreshadow should naturally resolve
- narrative_proximity: a 0.0-1.0 score measuring how close the current plot is
  to triggering resolution, computed via LLM comparison

Lifecycle: planted -> ripening -> ready -> resolved
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from neo4j import AsyncDriver

from app.db.neo4j import init_neo4j
from app.db import neo4j as _neo4j_mod
from app.db.session import async_session_factory
from app.models.project import Foreshadow
from app.tasks.entity_tasks import _materialize_entities_to_postgres
from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

# Proximity thresholds for status transitions
PROXIMITY_RIPENING = 0.7
PROXIMITY_READY = 0.9

# Statuses considered "active" (not yet resolved)
ACTIVE_STATUSES = ("planted", "ripening", "ready")


class ForeshadowManager:
    """
    Manages novel foreshadowing with condition-triggered resolution.

    Foreshadow lifecycle: planted -> ripening -> ready -> resolved

    Key insight: NO hard chapter deadlines. Instead, resolve_conditions
    describe WHAT narrative situation would make resolution natural.
    narrative_proximity is computed by comparing current plot context
    against resolve_conditions using vector similarity.
    """

    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db

    async def _get_db(self) -> AsyncSession:
        """Return the injected session or create a new one."""
        if self._db is not None:
            return self._db
        return async_session_factory()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        project_id: str | UUID,
        description: str,
        type: str,
        planted_chapter: int,
        resolve_conditions: list[str],
        resolution_blueprint: dict | None = None,
    ) -> Foreshadow:
        """Create a new foreshadow entry.

        Args:
            project_id: The owning project.
            description: What the foreshadow is about.
            type: Category -- e.g. "plot", "character", "worldbuilding".
            planted_chapter: The chapter index where it was planted.
            resolve_conditions: List of narrative conditions that would
                make resolution natural.
            resolution_blueprint: Optional guidance on *how* to resolve.

        Returns:
            The newly created Foreshadow ORM instance.
        """
        # v1.9+ architecture: Neo4j is the source of truth; Postgres is a read model.
        # So creation must write to Neo4j, then materialize back to Postgres.
        db = await self._get_db()

        await init_neo4j()
        driver: AsyncDriver | None = _neo4j_mod._driver
        if driver is None:
            raise RuntimeError("Neo4j driver has not been initialized")

        import uuid

        fid = str(uuid.uuid4())
        try:
            async with driver.session() as session:
                r = await session.run(
                    "MERGE (f:Foreshadow {project_id: $pid, id: $id}) "
                    "SET f.type = $type, "
                    "    f.description = $desc, "
                    "    f.planted_chapter = $planted, "
                    "    f.resolve_conditions_json = $conds, "
                    "    f.resolution_blueprint_json = $blueprint, "
                    "    f.narrative_proximity = $prox, "
                    "    f.status = $status, "
                    "    f.resolved_chapter = $resolved "
                    "RETURN f.id AS id",
                    pid=str(project_id),
                    id=fid,
                    type=str(type).strip(),
                    desc=str(description).strip(),
                    planted=int(planted_chapter),
                    conds=json.dumps(list(resolve_conditions or []), ensure_ascii=False),
                    blueprint=json.dumps(resolution_blueprint or {}, ensure_ascii=False),
                    prox=0.0,
                    status="planted",
                    resolved=None,
                )
                await r.consume()
        except Exception as e:
            raise RuntimeError(f"neo4j_write_failed: {e}")

        await _materialize_entities_to_postgres(
            project_id=str(project_id),
            chapter_idx=int(planted_chapter),
            caller="ForeshadowManager.create",
        )

        # Return the Postgres read-model row.
        row = await db.execute(
            select(Foreshadow).where(
                Foreshadow.project_id == str(project_id),
                Foreshadow.id == fid,
            )
        )
        foreshadow = row.scalar_one_or_none()
        if foreshadow is None:
            raise RuntimeError("pg_materialize_missing")

        logger.info(
            "Created foreshadow %s (type=%s) for project %s at chapter %d",
            foreshadow.id, type, project_id, planted_chapter,
        )
        return foreshadow

    async def get_active(self, project_id: str | UUID) -> list[Foreshadow]:
        """Get all non-resolved foreshadows for a project."""
        db = await self._get_db()
        result = await db.execute(
            select(Foreshadow)
            .where(
                Foreshadow.project_id == str(project_id),
                Foreshadow.status.in_(ACTIVE_STATUSES),
            )
            .order_by(Foreshadow.planted_chapter)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Proximity computation
    # ------------------------------------------------------------------

    async def update_proximity(
        self,
        project_id: str | UUID,
        current_chapter_idx: int,
        current_context: str,
    ) -> list[Foreshadow]:
        """Compute narrative_proximity for all active foreshadows.

        Uses LLM to compare the current narrative context against each
        foreshadow's resolve_conditions and returns a 0.0-1.0 score.

        Status transitions:
            proximity > 0.7 -> ripening
            proximity > 0.9 -> ready

        Args:
            project_id: The project to update.
            current_chapter_idx: The chapter currently being written.
            current_context: Recent narrative text / plot summary.

        Returns:
            List of updated foreshadow objects.
        """
        active = await self.get_active(project_id)
        if not active:
            return []

        router = get_model_router()
        db = await self._get_db()
        updated: list[Foreshadow] = []

        for fs in active:
            conditions = fs.resolve_conditions_json or []
            if not conditions:
                continue

            conditions_text = "\n".join(
                f"- {c}" for c in conditions
            )

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a narrative analysis assistant. Your task is to "
                        "evaluate how close the current story context is to "
                        "triggering the resolution of a foreshadow.\n\n"
                        "Return ONLY a JSON object with a single key "
                        '"proximity" whose value is a float between 0.0 and 1.0.\n'
                        "0.0 = the conditions are completely unmet\n"
                        "0.5 = partial progress toward the conditions\n"
                        "1.0 = the conditions are fully met\n\n"
                        "Example: {\"proximity\": 0.73}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Foreshadow description: {fs.description}\n\n"
                        f"Resolution conditions:\n{conditions_text}\n\n"
                        f"Current chapter index: {current_chapter_idx}\n"
                        f"Chapter where foreshadow was planted: {fs.planted_chapter}\n\n"
                        f"Current narrative context:\n{current_context}"
                    ),
                },
            ]

            try:
                result = await router.generate(
                    task_type="extraction",
                    messages=messages,
                    temperature=0.2,
                    max_tokens=128,
                )
                proximity = self._parse_proximity(result.text)
            except Exception:
                logger.exception(
                    "Failed to compute proximity for foreshadow %s", fs.id
                )
                continue

            # Determine new status
            new_status = fs.status
            if proximity >= PROXIMITY_READY:
                new_status = "ready"
            elif proximity >= PROXIMITY_RIPENING:
                new_status = "ripening"

            fs.narrative_proximity = proximity
            fs.status = new_status
            updated.append(fs)

            logger.debug(
                "Foreshadow %s: proximity=%.2f status=%s",
                fs.id, proximity, new_status,
            )

        await db.flush()
        return updated

    # ------------------------------------------------------------------
    # Resolution checking
    # ------------------------------------------------------------------

    async def check_resolution(
        self,
        project_id: str | UUID,
        chapter_text: str,
        chapter_idx: int | None = None,
    ) -> list[Foreshadow]:
        """Check if newly generated chapter text resolves any foreshadows.

        Uses LLM to determine whether the chapter text actually resolves
        ready/ripening foreshadows.

        Args:
            project_id: The project to check.
            chapter_text: The full text of the newly generated chapter.
            chapter_idx: Optional chapter index for bookkeeping.

        Returns:
            List of foreshadows that were marked as resolved.
        """
        db = await self._get_db()
        result = await db.execute(
            select(Foreshadow).where(
                Foreshadow.project_id == str(project_id),
                Foreshadow.status.in_(("ripening", "ready")),
            )
        )
        candidates = list(result.scalars().all())
        if not candidates:
            return []

        router = get_model_router()
        resolved: list[Foreshadow] = []

        for fs in candidates:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a narrative analysis assistant. Determine "
                        "whether the given chapter text resolves the described "
                        "foreshadow.\n\n"
                        "Return ONLY a JSON object: "
                        '{\"resolved\": true} or {\"resolved\": false}\n'
                        "A foreshadow is resolved when the text explicitly "
                        "addresses or fulfills the planted narrative thread."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Foreshadow description: {fs.description}\n\n"
                        f"Resolution conditions:\n"
                        + "\n".join(f"- {c}" for c in (fs.resolve_conditions_json or []))
                        + f"\n\nChapter text:\n{chapter_text[:3000]}"
                    ),
                },
            ]

            try:
                gen_result = await router.generate(
                    task_type="extraction",
                    messages=messages,
                    temperature=0.2,
                    max_tokens=64,
                )
                is_resolved = self._parse_resolved(gen_result.text)
            except Exception:
                logger.exception(
                    "Failed to check resolution for foreshadow %s", fs.id
                )
                continue

            if is_resolved:
                fs.status = "resolved"
                fs.resolved_chapter = chapter_idx
                fs.narrative_proximity = 1.0
                resolved.append(fs)
                logger.info("Foreshadow %s resolved at chapter %s", fs.id, chapter_idx)

        await db.flush()
        return resolved

    # ------------------------------------------------------------------
    # Prompt generation for chapter context
    # ------------------------------------------------------------------

    async def get_prompts_for_generation(
        self,
        project_id: str | UUID,
        current_chapter_idx: int,
    ) -> str:
        """Generate prompt text about active foreshadows for injection
        into the chapter generation context.

        For *ripening* foreshadows: gentle suggestion.
        For *ready* foreshadows: stronger prompt urging resolution.

        Args:
            project_id: The project.
            current_chapter_idx: The chapter about to be generated.

        Returns:
            A formatted string to inject into the generation system prompt.
            Empty string if there are no relevant foreshadows.
        """
        active = await self.get_active(project_id)
        if not active:
            return ""

        parts: list[str] = []

        ripening = [f for f in active if f.status == "ripening"]
        ready = [f for f in active if f.status == "ready"]

        if ripening:
            parts.append("【伏笔推进提示 - 可适当推进以下伏笔】")
            for fs in ripening:
                blueprint = ""
                if fs.resolution_blueprint_json:
                    blueprint = f" (参考方案: {json.dumps(fs.resolution_blueprint_json, ensure_ascii=False)})"
                parts.append(
                    f"  - [渐近] {fs.description} "
                    f"(接近度: {fs.narrative_proximity:.0%}){blueprint}"
                )

        if ready:
            parts.append("【伏笔解决提示 - 以下伏笔已成熟，应在本章或近章解决】")
            for fs in ready:
                conditions_text = ", ".join(fs.resolve_conditions_json or [])
                blueprint = ""
                if fs.resolution_blueprint_json:
                    blueprint = json.dumps(
                        fs.resolution_blueprint_json, ensure_ascii=False
                    )
                parts.append(
                    f"  - [待解决] {fs.description}\n"
                    f"    触发条件: {conditions_text}\n"
                    f"    解决方案: {blueprint or '由AI自行安排'}"
                )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Auto-registration from generated text
    # ------------------------------------------------------------------

    async def register_from_text(
        self,
        project_id: str | UUID,
        chapter_idx: int,
        chapter_text: str,
    ) -> list[Foreshadow]:
        """Use LLM to detect if new foreshadows were planted in generated
        text, and auto-register them.

        Args:
            project_id: The project.
            chapter_idx: The chapter index of the generated text.
            chapter_text: The full generated chapter text.

        Returns:
            List of newly created Foreshadow objects.
        """
        router = get_model_router()

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a literary analyst. Identify any foreshadowing "
                    "elements planted in the given chapter text.\n\n"
                    "For each foreshadow found, return a JSON object with:\n"
                    '- "description": what was foreshadowed\n'
                    '- "type": one of "plot", "character", "worldbuilding", "mystery"\n'
                    '- "resolve_conditions": list of narrative conditions '
                    "that would trigger its resolution\n\n"
                    "Return a JSON array. If no foreshadowing is found, "
                    "return an empty array [].\n\n"
                    "Example:\n"
                    '[{"description": "...", "type": "plot", '
                    '"resolve_conditions": ["...", "..."]}]'
                ),
            },
            {
                "role": "user",
                "content": f"Chapter text:\n{chapter_text[:4000]}",
            },
        ]

        try:
            result = await router.generate(
                task_type="extraction",
                messages=messages,
                temperature=0.3,
                max_tokens=1024,
            )
            foreshadows_data = self._parse_foreshadow_list(result.text)
        except Exception:
            logger.exception(
                "Failed to detect foreshadows in chapter %d of project %s",
                chapter_idx, project_id,
            )
            return []

        created: list[Foreshadow] = []
        for item in foreshadows_data:
            try:
                fs = await self.create(
                    project_id=project_id,
                    description=item.get("description", ""),
                    type=item.get("type", "plot"),
                    planted_chapter=chapter_idx,
                    resolve_conditions=item.get("resolve_conditions", []),
                )
                created.append(fs)
            except Exception:
                logger.exception("Failed to register foreshadow: %s", item)

        logger.info(
            "Auto-registered %d foreshadows from chapter %d",
            len(created), chapter_idx,
        )
        return created

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_proximity(text: str) -> float:
        """Extract proximity float from LLM response."""
        try:
            data = json.loads(text.strip())
            value = float(data.get("proximity", 0.0))
            return max(0.0, min(1.0, value))
        except (json.JSONDecodeError, ValueError, TypeError):
            # Try to extract a number from the text
            import re
            match = re.search(r"(\d+\.?\d*)", text)
            if match:
                value = float(match.group(1))
                if value > 1.0:
                    value = value / 100.0  # e.g. "73" -> 0.73
                return max(0.0, min(1.0, value))
            logger.warning("Could not parse proximity from: %s", text)
            return 0.0

    @staticmethod
    def _parse_resolved(text: str) -> bool:
        """Extract resolved boolean from LLM response."""
        try:
            data = json.loads(text.strip())
            return bool(data.get("resolved", False))
        except (json.JSONDecodeError, ValueError, TypeError):
            lower = text.lower()
            return "true" in lower and "false" not in lower

    @staticmethod
    def _parse_foreshadow_list(text: str) -> list[dict]:
        """Extract list of foreshadow dicts from LLM response."""
        text = text.strip()

        # Try direct JSON parse
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in the text
        import re
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        logger.warning("Could not parse foreshadow list from: %s", text[:200])
        return []
