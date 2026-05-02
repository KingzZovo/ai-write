"""
Entity Timeline Service

Manages the Neo4j knowledge graph for tracking entities across the novel.
Supports character states, relationships, locations, organizations, and
world rules — all versioned by chapter index for temporal queries.

Neo4j node types:
- (:Character {id, project_id, name})
- (:CharacterState {chapter_start, chapter_end, status_json})
- (:Location {id, project_id, name})
- (:Organization {id, project_id, name})
- (:WorldRule {id, project_id, category, text})

Relationship types:
- (:Character)-[:HAS_STATE]->(:CharacterState)
- (:Character)-[:RELATES_TO {type, chapter_start, chapter_end}]->(:Character)
- (:Character)-[:AT_LOCATION {chapter_start, chapter_end}]->(:Location)
- (:Character)-[:MEMBER_OF {chapter_start, chapter_end}]->(:Organization)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from neo4j import AsyncDriver

from app.services.model_router import get_model_router_async

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CharacterSnapshot:
    """A character's state at a specific chapter."""

    name: str
    status: dict[str, Any] = field(default_factory=dict)
    chapter_start: int = 0
    chapter_end: int | None = None


@dataclass
class RelationshipSnapshot:
    """A relationship between two characters at a specific chapter."""

    source: str
    target: str
    rel_type: str
    chapter_start: int = 0
    chapter_end: int | None = None


@dataclass
class WorldSnapshot:
    """Full world state at a specific chapter point."""

    characters: list[CharacterSnapshot] = field(default_factory=list)
    relationships: list[RelationshipSnapshot] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM extraction prompt
# ---------------------------------------------------------------------------

ENTITY_EXTRACTION_PROMPT = """\
你是一个小说实体提取助手。分析以下章节文本，提取实体信息变化。

输出纯 JSON 格式：
{
  "new_characters": [
    {"name": "角色名", "state": {"身份": "...", "状态": "...", "情绪": "...", "能力等级": "..."}}
  ],
  "state_changes": [
    {"name": "已有角色名", "changes": {"状态": "新状态", "情绪": "新情绪"}}
  ],
  "new_relationships": [
    {"char1": "角色A", "char2": "角色B", "type": "关系类型(如：师徒、敌对、恋人、同盟)"}
  ],
  "changed_relationships": [
    {"char1": "角色A", "char2": "角色B", "type": "新的关系类型"}
  ],
  "location_changes": [
    {"character": "角色名", "location": "新地点"}
  ]
}

如果没有相应变化，对应字段返回空数组。

章节文本：
"""


class EntityTimelineService:
    """Manages entity state tracking in Neo4j knowledge graph."""

    def __init__(self, driver: AsyncDriver) -> None:
        self.driver = driver

    # ------------------------------------------------------------------
    # Graph initialisation
    # ------------------------------------------------------------------

    async def initialize_graph(self, project_id: str) -> None:
        """Create constraints and indexes for the project graph."""
        try:
            async with self.driver.session() as session:
                # Uniqueness constraints
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR (c:Character) REQUIRE (c.project_id, c.name) IS UNIQUE"
                )
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR (l:Location) REQUIRE (l.project_id, l.name) IS UNIQUE"
                )
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR (o:Organization) REQUIRE (o.project_id, o.name) IS UNIQUE"
                )
                # Indexes for fast lookup
                await session.run(
                    "CREATE INDEX IF NOT EXISTS FOR (c:Character) ON (c.project_id)"
                )
                await session.run(
                    "CREATE INDEX IF NOT EXISTS FOR (cs:CharacterState) ON (cs.chapter_start)"
                )
                await session.run(
                    "CREATE INDEX IF NOT EXISTS FOR (w:WorldRule) ON (w.project_id)"
                )
                logger.info("Neo4j graph initialized for project %s", project_id)
        except Exception as e:
            logger.warning("Failed to initialize Neo4j graph: %s", e)

    # ------------------------------------------------------------------
    # Character CRUD
    # ------------------------------------------------------------------

    async def add_character(
        self,
        project_id: str,
        name: str,
        initial_state: dict[str, Any] | None = None,
    ) -> str | None:
        """Add a new character node and optional initial state. Returns node id."""
        try:
            async with self.driver.session() as session:
                node_id = str(uuid.uuid4())
                result = await session.run(
                    "MERGE (c:Character {project_id: $pid, name: $name}) "
                    "ON CREATE SET c.id = $id "
                    "RETURN c.id AS id",
                    pid=project_id,
                    name=name,
                    id=node_id,
                )
                record = await result.single()
                char_id = record["id"] if record else node_id

                if initial_state:
                    await session.run(
                        "MATCH (c:Character {project_id: $pid, name: $name}) "
                        "CREATE (c)-[:HAS_STATE]->(s:CharacterState {"
                        "  id: $sid, chapter_start: 0, chapter_end: null, "
                        "  status_json: $status"
                        "})",
                        pid=project_id,
                        name=name,
                        sid=str(uuid.uuid4()),
                        status=json.dumps(initial_state, ensure_ascii=False),
                    )

                return char_id
        except Exception as e:
            logger.warning("Failed to add character %s: %s", name, e)
            return None

    async def update_character_state(
        self,
        project_id: str,
        name: str,
        chapter_idx: int,
        state_json: dict[str, Any],
    ) -> None:
        """Create a new CharacterState and close the previous one's chapter_end."""
        try:
            async with self.driver.session() as session:
                # Close previous open state
                await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $name})"
                    "-[:HAS_STATE]->(s:CharacterState) "
                    "WHERE s.chapter_end IS NULL "
                    "SET s.chapter_end = $idx",
                    pid=project_id,
                    name=name,
                    idx=chapter_idx - 1,
                )
                # Create new state
                await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $name}) "
                    "CREATE (c)-[:HAS_STATE]->(s:CharacterState {"
                    "  id: $sid, chapter_start: $start, chapter_end: null, "
                    "  status_json: $status"
                    "})",
                    pid=project_id,
                    name=name,
                    sid=str(uuid.uuid4()),
                    start=chapter_idx,
                    status=json.dumps(state_json, ensure_ascii=False),
                )
        except Exception as e:
            logger.warning("Failed to update character state for %s: %s", name, e)

    # ------------------------------------------------------------------
    # Relationship CRUD
    # ------------------------------------------------------------------

    async def add_relationship(
        self,
        project_id: str,
        char1: str,
        char2: str,
        rel_type: str,
        chapter_idx: int,
    ) -> None:
        """Add a new relationship between two characters."""
        try:
            async with self.driver.session() as session:
                # Ensure both characters exist
                await session.run(
                    "MERGE (:Character {project_id: $pid, name: $c1})",
                    pid=project_id,
                    c1=char1,
                )
                await session.run(
                    "MERGE (:Character {project_id: $pid, name: $c2})",
                    pid=project_id,
                    c2=char2,
                )
                await session.run(
                    "MATCH (a:Character {project_id: $pid, name: $c1}), "
                    "      (b:Character {project_id: $pid, name: $c2}) "
                    "CREATE (a)-[:RELATES_TO {"
                    "  type: $rtype, chapter_start: $start, chapter_end: null"
                    "}]->(b)",
                    pid=project_id,
                    c1=char1,
                    c2=char2,
                    rtype=rel_type,
                    start=chapter_idx,
                )
        except Exception as e:
            logger.warning(
                "Failed to add relationship %s-%s (%s): %s",
                char1, char2, rel_type, e,
            )

    async def update_relationship(
        self,
        project_id: str,
        char1: str,
        char2: str,
        rel_type: str,
        chapter_idx: int,
    ) -> None:
        """Close existing open relationship and create a new one with updated type."""
        try:
            async with self.driver.session() as session:
                # Close existing open relationship
                await session.run(
                    "MATCH (a:Character {project_id: $pid, name: $c1})"
                    "-[r:RELATES_TO]->"
                    "(b:Character {project_id: $pid, name: $c2}) "
                    "WHERE r.chapter_end IS NULL "
                    "SET r.chapter_end = $idx",
                    pid=project_id,
                    c1=char1,
                    c2=char2,
                    idx=chapter_idx - 1,
                )
                # Create new relationship
                await session.run(
                    "MATCH (a:Character {project_id: $pid, name: $c1}), "
                    "      (b:Character {project_id: $pid, name: $c2}) "
                    "CREATE (a)-[:RELATES_TO {"
                    "  type: $rtype, chapter_start: $start, chapter_end: null"
                    "}]->(b)",
                    pid=project_id,
                    c1=char1,
                    c2=char2,
                    rtype=rel_type,
                    start=chapter_idx,
                )
        except Exception as e:
            logger.warning(
                "Failed to update relationship %s-%s: %s",
                char1, char2, e,
            )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_character_state_at(
        self,
        project_id: str,
        name: str,
        chapter_idx: int,
    ) -> CharacterSnapshot | None:
        """Get a character's state at a specific chapter."""
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $name})"
                    "-[:HAS_STATE]->(s:CharacterState) "
                    "WHERE s.chapter_start <= $idx "
                    "  AND (s.chapter_end IS NULL OR s.chapter_end >= $idx) "
                    "RETURN s.status_json AS status, "
                    "       s.chapter_start AS start, "
                    "       s.chapter_end AS end "
                    "ORDER BY s.chapter_start DESC LIMIT 1",
                    pid=project_id,
                    name=name,
                    idx=chapter_idx,
                )
                record = await result.single()
                if not record:
                    return None
                status = json.loads(record["status"]) if record["status"] else {}
                return CharacterSnapshot(
                    name=name,
                    status=status,
                    chapter_start=record["start"],
                    chapter_end=record["end"],
                )
        except Exception as e:
            logger.warning("Failed to get character state for %s: %s", name, e)
            return None

    async def get_active_characters_at(
        self,
        project_id: str,
        chapter_idx: int,
    ) -> list[CharacterSnapshot]:
        """Get all characters with active state at a specific chapter."""
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    "MATCH (c:Character {project_id: $pid})"
                    "-[:HAS_STATE]->(s:CharacterState) "
                    "WHERE s.chapter_start <= $idx "
                    "  AND (s.chapter_end IS NULL OR s.chapter_end >= $idx) "
                    "RETURN c.name AS name, "
                    "       s.status_json AS status, "
                    "       s.chapter_start AS start, "
                    "       s.chapter_end AS end",
                    pid=project_id,
                    idx=chapter_idx,
                )
                snapshots: list[CharacterSnapshot] = []
                async for record in result:
                    status = json.loads(record["status"]) if record["status"] else {}
                    snapshots.append(
                        CharacterSnapshot(
                            name=record["name"],
                            status=status,
                            chapter_start=record["start"],
                            chapter_end=record["end"],
                        )
                    )
                return snapshots
        except Exception as e:
            logger.warning("Failed to get active characters: %s", e)
            return []

    async def get_relationships_at(
        self,
        project_id: str,
        chapter_idx: int,
    ) -> list[RelationshipSnapshot]:
        """Get all relationships active at a specific chapter."""
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    "MATCH (a:Character {project_id: $pid})"
                    "-[r:RELATES_TO]->"
                    "(b:Character {project_id: $pid}) "
                    "WHERE r.chapter_start <= $idx "
                    "  AND (r.chapter_end IS NULL OR r.chapter_end >= $idx) "
                    "RETURN a.name AS source, b.name AS target, "
                    "       r.type AS rtype, "
                    "       r.chapter_start AS start, "
                    "       r.chapter_end AS end",
                    pid=project_id,
                    idx=chapter_idx,
                )
                snapshots: list[RelationshipSnapshot] = []
                async for record in result:
                    snapshots.append(
                        RelationshipSnapshot(
                            source=record["source"],
                            target=record["target"],
                            rel_type=record["rtype"],
                            chapter_start=record["start"],
                            chapter_end=record["end"],
                        )
                    )
                return snapshots
        except Exception as e:
            logger.warning("Failed to get relationships: %s", e)
            return []

    async def get_world_snapshot(
        self,
        project_id: str,
        chapter_idx: int,
    ) -> WorldSnapshot:
        """Get the full world state at a specific chapter point."""
        snapshot = WorldSnapshot()

        try:
            # Characters
            snapshot.characters = await self.get_active_characters_at(
                project_id, chapter_idx
            )

            # Relationships
            snapshot.relationships = await self.get_relationships_at(
                project_id, chapter_idx
            )

            async with self.driver.session() as session:
                # Active locations (characters currently at locations)
                loc_result = await session.run(
                    "MATCH (c:Character {project_id: $pid})"
                    "-[r:AT_LOCATION]->"
                    "(l:Location {project_id: $pid}) "
                    "WHERE r.chapter_start <= $idx "
                    "  AND (r.chapter_end IS NULL OR r.chapter_end >= $idx) "
                    "RETURN DISTINCT l.name AS name",
                    pid=project_id,
                    idx=chapter_idx,
                )
                async for record in loc_result:
                    snapshot.locations.append(record["name"])

                # Active organizations
                org_result = await session.run(
                    "MATCH (c:Character {project_id: $pid})"
                    "-[r:MEMBER_OF]->"
                    "(o:Organization {project_id: $pid}) "
                    "WHERE r.chapter_start <= $idx "
                    "  AND (r.chapter_end IS NULL OR r.chapter_end >= $idx) "
                    "RETURN DISTINCT o.name AS name",
                    pid=project_id,
                    idx=chapter_idx,
                )
                async for record in org_result:
                    snapshot.organizations.append(record["name"])

        except Exception as e:
            logger.warning("Failed to get world snapshot: %s", e)

        return snapshot

    # ------------------------------------------------------------------
    # Location & organisation helpers
    # ------------------------------------------------------------------

    async def _set_character_location(
        self,
        project_id: str,
        character_name: str,
        location_name: str,
        chapter_idx: int,
    ) -> None:
        """Move a character to a new location (close previous, open new)."""
        try:
            async with self.driver.session() as session:
                # Ensure location exists
                await session.run(
                    "MERGE (l:Location {project_id: $pid, name: $name}) "
                    "ON CREATE SET l.id = $id",
                    pid=project_id,
                    name=location_name,
                    id=str(uuid.uuid4()),
                )
                # Close previous location relationship
                await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $cname})"
                    "-[r:AT_LOCATION]->(:Location) "
                    "WHERE r.chapter_end IS NULL "
                    "SET r.chapter_end = $idx",
                    pid=project_id,
                    cname=character_name,
                    idx=chapter_idx - 1,
                )
                # Create new location relationship
                await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $cname}), "
                    "      (l:Location {project_id: $pid, name: $lname}) "
                    "CREATE (c)-[:AT_LOCATION {"
                    "  chapter_start: $start, chapter_end: null"
                    "}]->(l)",
                    pid=project_id,
                    cname=character_name,
                    lname=location_name,
                    start=chapter_idx,
                )
        except Exception as e:
            logger.warning(
                "Failed to set location for %s -> %s: %s",
                character_name, location_name, e,
            )

    # ------------------------------------------------------------------
    # LLM-driven extraction
    # ------------------------------------------------------------------

    async def extract_and_update(
        self,
        project_id: str,
        chapter_idx: int,
        chapter_text: str,
    ) -> None:
        """
        Use LLM to extract entities from generated text and update the graph.

        Extracts: new characters, state changes, new/changed relationships,
        location changes.
        """
        if not chapter_text.strip():
            return

        # B2' (v1.5.0): use async router + tier-aware fallback so this path
        # is celery-loop-safe and resilient to single-endpoint INTERNAL_ERROR
        # (mirrors the B1' evaluator/checker pattern). Each attempt is logged
        # to llm_call_logs with caller=EntityTimelineService.extract_and_update.
        try:
            router = await get_model_router_async()
        except Exception as e:
            logger.warning("entity extraction skipped: model router unavailable: %s", e)
            return

        try:
            result = await router.generate_with_tier_fallback(
                task_type="extraction",
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个小说实体提取助手，只输出 JSON。",
                    },
                    {
                        "role": "user",
                        "content": ENTITY_EXTRACTION_PROMPT + chapter_text[:6000],
                    },
                ],
                max_tokens=2048,
                _log_meta={
                    "caller": "EntityTimelineService.extract_and_update",
                    "project_id": str(project_id),
                    "chapter_idx": int(chapter_idx),
                },
            )

            data = _parse_json(result.text)
        except Exception as e:
            logger.warning("LLM entity extraction failed: %s", e)
            return

        # Process new characters
        for char_info in data.get("new_characters", []):
            name = char_info.get("name")
            if not name:
                continue
            await self.add_character(project_id, name, char_info.get("state"))
            state = char_info.get("state")
            if state:
                await self.update_character_state(
                    project_id, name, chapter_idx, state
                )

        # Process state changes
        for change in data.get("state_changes", []):
            name = change.get("name")
            changes = change.get("changes")
            if not name or not changes:
                continue
            # Merge with existing state
            existing = await self.get_character_state_at(
                project_id, name, chapter_idx
            )
            if existing:
                merged = {**existing.status, **changes}
            else:
                merged = changes
            await self.update_character_state(
                project_id, name, chapter_idx, merged
            )

        # Process new relationships
        for rel in data.get("new_relationships", []):
            c1 = rel.get("char1")
            c2 = rel.get("char2")
            rtype = rel.get("type")
            if c1 and c2 and rtype:
                await self.add_relationship(
                    project_id, c1, c2, rtype, chapter_idx
                )

        # Process changed relationships
        for rel in data.get("changed_relationships", []):
            c1 = rel.get("char1")
            c2 = rel.get("char2")
            rtype = rel.get("type")
            if c1 and c2 and rtype:
                await self.update_relationship(
                    project_id, c1, c2, rtype, chapter_idx
                )

        # Process location changes
        for loc in data.get("location_changes", []):
            character = loc.get("character")
            location = loc.get("location")
            if character and location:
                # Ensure character exists
                await self.add_character(project_id, character)
                await self._set_character_location(
                    project_id, character, location, chapter_idx
                )

        logger.info(
            "Entity extraction complete for project %s chapter %d",
            project_id, chapter_idx,
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _parse_json(text: str) -> dict[str, Any]:
    """Parse JSON from LLM output, handling markdown code blocks."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return json.loads(cleaned)
