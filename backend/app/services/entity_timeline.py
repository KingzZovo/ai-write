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
- (:Foreshadow {id, project_id, type, description, planted_chapter, resolve_conditions_json, resolution_blueprint_json, narrative_proximity, status, resolved_chapter})

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
    field_sources: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RelationshipSnapshot:
    """A relationship between two characters at a specific chapter."""

    source: str
    target: str
    rel_type: str
    chapter_start: int = 0
    chapter_end: int | None = None
    source_chapter_id: str | None = None
    source_hash: str | None = None
    event_kind: str | None = None
    raw_type: str = ''
    origin: str = 'prose'
    author_id: str | None = None
    author_edit_id: str | None = None
    author_updated_at: int | None = None
    label: str = ''
    note: str = ''
    sentiment: str = ''
    record_id: str | None = None
    evidence: str = ''
    replaces: list[str] | None = None
    unresolved_replaces: list[str] = field(default_factory=list)
    scope_pending: dict[str, Any] | None = None


@dataclass
class WorldSnapshot:
    """Full world state at a specific chapter point."""

    characters: list[CharacterSnapshot] = field(default_factory=list)
    relationships: list[RelationshipSnapshot] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    profiles: list[dict[str, Any]] = field(default_factory=list)
    location_records: list[dict[str, Any]] = field(default_factory=list)
    location_state_records: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Entity timeline graph operations
# ---------------------------------------------------------------------------


class EntityTimelineService:
    """Manages entity state tracking in Neo4j knowledge graph."""

    def __init__(self, driver: AsyncDriver, *, strict_writes: bool = False) -> None:
        self.driver = driver
        self.strict_writes = strict_writes

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
                # RELATES_TO uniqueness: (project_id, src, tgt, type, chapter_start)
                # NOTE: requires we always set these properties on the relationship.
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR ()-[r:RELATES_TO]-() REQUIRE (r.project_id, r.source_name, r.target_name, r.type, r.chapter_start) IS UNIQUE"
                )
                # AT_LOCATION uniqueness: one active location per character per chapter_start.
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR ()-[r:AT_LOCATION]-() REQUIRE (r.project_id, r.character_name, r.chapter_start) IS UNIQUE"
                )
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR (w:WorldRule) REQUIRE (w.project_id, w.category, w.text) IS UNIQUE"
                )
                # MEMBER_OF uniqueness: (project_id, character_name, org_name, chapter_start)
                # NOTE: requires we always set these properties on the relationship.
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR ()-[r:MEMBER_OF]-() REQUIRE (r.project_id, r.character_name, r.org_name, r.chapter_start) IS UNIQUE"
                )
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR (l:Location) REQUIRE (l.project_id, l.name) IS UNIQUE"
                )
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR (o:Organization) REQUIRE (o.project_id, o.name) IS UNIQUE"
                )
                await session.run(
                    "CREATE CONSTRAINT IF NOT EXISTS "
                    "FOR (f:Foreshadow) REQUIRE (f.project_id, f.id) IS UNIQUE"
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
            if self.strict_writes:
                raise
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
            if self.strict_writes:
                raise
            logger.warning("Failed to add character %s: %s", name, e)
            return None

    async def update_character_state(
        self,
        project_id: str,
        name: str,
        chapter_idx: int,
        state_json: dict[str, Any],
    ) -> None:
        """Create a new CharacterState if state changed; otherwise no-op.

        v1.7.2: Skip writes when the new state_json is byte-equivalent to the
        currently-open state. Prevents duplicate "状态变化" cards in the UI
        when LLM extraction echoes back unchanged status for chapters where the
        character did not actually change.
        """
        # Force chapter_idx >= 1; chapter 0 is reserved for genuine pre-story state.
        if chapter_idx is None or chapter_idx < 0:
            chapter_idx = 1
        new_status_str = json.dumps(state_json or {}, ensure_ascii=False, sort_keys=True)
        try:
            async with self.driver.session() as session:
                # Probe currently-open state for diff
                cur = await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $name})"
                    "-[:HAS_STATE]->(s:CharacterState) "
                    "WHERE s.chapter_end IS NULL "
                    "RETURN s.status_json AS status "
                    "ORDER BY s.chapter_start DESC LIMIT 1",
                    pid=project_id,
                    name=name,
                )
                rec = await cur.single()
                if rec is not None:
                    prev_raw = rec.get("status")
                    try:
                        prev_obj = json.loads(prev_raw) if isinstance(prev_raw, str) else (prev_raw or {})
                    except Exception:
                        prev_obj = {}
                    prev_str = json.dumps(prev_obj, ensure_ascii=False, sort_keys=True)
                    if prev_str == new_status_str:
                        # No-op: state unchanged. Don't close prev state, don't create new.
                        return
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
            if self.strict_writes:
                raise
            logger.warning("Failed to update character state for %s: %s", name, e)

    # ------------------------------------------------------------------
    # Relationship CRUD
    # ------------------------------------------------------------------

    async def _relationship_transaction(self, project_id, work):
        from app.services.relationship_graph_write import GUARD_CONSTRAINT, lock_relationship_graph

        async def locked(tx):
            await lock_relationship_graph(tx, project_id)
            return await work(tx)

        async with self.driver.session() as session:
            await (await session.run(GUARD_CONSTRAINT)).consume()
            return await session.execute_write(locked)

    async def _require_legacy_relationship_pair(self, tx, project_id, char1, char2):
        """Unsourced callers cannot modify a pair with source or author history."""
        from app.services.relationship_write_contract import RelationshipWriteConflict

        params = dict(pid=project_id, c1=char1, c2=char2)
        record = await (await tx.run(
            'MATCH (:Character {project_id:$pid,name:$c1})-[r:RELATES_TO]->'
            '(:Character {project_id:$pid,name:$c2}) '
            'WHERE r.origin="author" OR r.source_chapter_id IS NOT NULL OR r.source_outline_id IS NOT NULL '
            'RETURN elementId(r) AS owned LIMIT 1', **params)).single()
        suppressed = await (await tx.run(
            'MATCH (s:RelationshipSuppression {project_id:$pid,source_name:$c1,target_name:$c2}) '
            'RETURN s.key AS owned LIMIT 1', **params)).single()
        if record or suppressed:
            raise RelationshipWriteConflict('versioned_relationship_required')

    async def add_relationship(
        self, project_id: str, char1: str, char2: str, rel_type: str, chapter_idx: int,
        *, evidence: str = '',
    ) -> None:
        """Compatibility write for entirely unowned legacy pairs.

        Source-backed extraction uses VersionedTimeline instead. A legacy call
        cannot overwrite author settings or resurrect a deleted source record.
        """
        async def write(tx):
            await self._require_legacy_relationship_pair(tx, project_id, char1, char2)
            await tx.run("MERGE (:Character {project_id: $pid, name: $c1})", pid=project_id, c1=char1)
            await tx.run("MERGE (:Character {project_id: $pid, name: $c2})", pid=project_id, c2=char2)
            await tx.run(
                "MATCH (a:Character {project_id: $pid, name: $c1}), "
                "(b:Character {project_id: $pid, name: $c2}) "
                "MERGE (a)-[r:RELATES_TO {project_id: $pid, source_name: $c1, target_name: $c2, type: $rtype}]->(b) "
                "ON CREATE SET r.chapter_start = $start SET r.chapter_end = null, r.evidence=$evidence",
                pid=project_id, c1=char1, c2=char2, rtype=rel_type, start=chapter_idx, evidence=evidence)
        try:
            await self._relationship_transaction(project_id, write)
        except Exception as e:
            if self.strict_writes:
                raise
            logger.warning("Failed to add relationship %s-%s (%s): %s", char1, char2, rel_type, e)

    async def update_relationship(
        self, project_id: str, char1: str, char2: str, rel_type: str, chapter_idx: int,
        *, evidence: str = '', replaces: list[str] | None = None,
    ) -> None:
        """Legacy changes lack stable end targets; never close other roles."""
        if replaces:
            from app.services.relationship_write_contract import RelationshipWriteConflict
            raise RelationshipWriteConflict('versioned_relationship_required')
        async def write(tx):
            await self._require_legacy_relationship_pair(tx, project_id, char1, char2)
            await tx.run(
                "MATCH (a:Character {project_id: $pid, name: $c1})-[r:RELATES_TO]->"
                "(b:Character {project_id: $pid, name: $c2}) WHERE r.chapter_end IS NULL "
                "AND r.type=$rtype AND r.chapter_start<$start "
                "AND (CASE WHEN trim(coalesce(r.raw_type,''))='' THEN r.type ELSE r.raw_type END)=$rtype "
                "SET r.chapter_end = $idx", pid=project_id, c1=char1, c2=char2,
                rtype=rel_type, start=chapter_idx, idx=chapter_idx - 1)
            await tx.run(
                "MATCH (a:Character {project_id: $pid, name: $c1}), (b:Character {project_id: $pid, name: $c2}) "
                "MERGE (a)-[r:RELATES_TO {project_id: $pid, source_name: $c1, target_name: $c2, type: $rtype, chapter_start: $start}]->(b) "
                "SET r.chapter_end = null,r.event_kind='change',r.evidence=$evidence",
                pid=project_id, c1=char1, c2=char2, rtype=rel_type, start=chapter_idx, evidence=evidence)
        try:
            await self._relationship_transaction(project_id, write)
        except Exception as e:
            if self.strict_writes:
                raise
            logger.warning("Failed to update relationship %s-%s: %s", char1, char2, e)

    async def dedupe_relationships(self, project_id: str) -> int:
        """Clean only unsourced legacy duplicates; preserve all owned history."""
        async def write(tx):
            result = await tx.run(
                "MATCH (a:Character {project_id: $pid})-[r:RELATES_TO]->(b:Character {project_id: $pid}) "
                'WHERE coalesce(r.origin, "") <> "author" AND r.source_chapter_id IS NULL '
                'AND r.source_outline_id IS NULL '
                "WITH a, b, r ORDER BY coalesce(r.chapter_start, -1) DESC, elementId(r) DESC "
                "WITH a, b, r.type AS t, collect(r) AS rs "
                "WITH a, b, head(rs) AS keep, rs[1..] AS dupes "
                "SET keep.project_id = $pid, keep.source_name = a.name, keep.target_name = b.name "
                "WITH dupes UNWIND dupes AS d DELETE d RETURN count(d) AS deleted", pid=project_id)
            record = await result.single()
            return int(record["deleted"]) if record else 0
        deleted = await self._relationship_transaction(project_id, write)
        logger.info("dedupe_relationships: project=%s deleted=%d", project_id, deleted)
        return deleted

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
            if self.strict_writes:
                raise
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
            if self.strict_writes:
                raise
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
            if self.strict_writes:
                raise
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
            if self.strict_writes:
                raise
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
                # Close previous location relationship.
                # 不能关掉本章刚建立的那条:同章第二次调用会把它的 chapter_end
                # 设成 idx-1,形成结束早于开始的窗口。
                await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $cname})"
                    "-[r:AT_LOCATION]->(:Location) "
                    "WHERE r.chapter_end IS NULL AND r.chapter_start <> $idx "
                    "SET r.chapter_end = $idx",
                    pid=project_id,
                    cname=character_name,
                    idx=chapter_idx - 1,
                )
                # Create new location relationship.
                # AT_LOCATION 上有唯一约束 (project_id, character_name, chapter_start):
                # 同章第二个地点若照原样 MERGE,匹配不到已有边就会新建同键的边,
                # Neo4j 抛 ConstraintValidationFailed。已有地点时只记进
                # alternate_locations,不建边(实测 2026-11-14 连续 24 章)。
                await session.run(
                    "MATCH (c:Character {project_id: $pid, name: $cname}) "
                    "OPTIONAL MATCH (c)-[existing:AT_LOCATION "
                    "{project_id: $pid, character_name: $cname, chapter_start: $start}]->(:Location) "
                    "FOREACH (_ IN CASE WHEN existing IS NULL THEN [1] ELSE [] END | "
                    "  MERGE (l:Location {project_id: $pid, name: $lname}) "
                    "  ON CREATE SET l.id = $id "
                    "  MERGE (c)-[r:AT_LOCATION "
                    "{project_id: $pid, character_name: $cname, chapter_start: $start}]->(l) "
                    "  ON CREATE SET r.chapter_end = null "
                    "  SET r.location_name = $lname) "
                    "FOREACH (_ IN CASE WHEN existing IS NULL THEN [] ELSE [1] END | "
                    "  SET existing.alternate_locations = "
                    "coalesce(existing.alternate_locations, []) + [$lname])",
                    pid=project_id,
                    cname=character_name,
                    lname=location_name,
                    start=chapter_idx,
                    id=str(uuid.uuid4()),
                )
        except Exception as e:
            if self.strict_writes:
                raise
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
        """Extract a complete payload before starting any graph mutation."""
        from app.db.session import async_session_factory
        from app.services.chapter_entity_extraction import extract_entity_changes

        async with async_session_factory() as db:
            data = await extract_entity_changes(
                project_id=project_id, chapter_text=chapter_text, db=db,
            )
        await self.apply_entity_changes(project_id, chapter_idx, chapter_text, data)

    async def apply_entity_changes(
        self, project_id: str, chapter_idx: int, chapter_text: str, data: dict,
    ) -> None:
        """Apply a fully validated payload; versioned replay can use this boundary."""
        from app.services.chapter_entity_extraction import validate_entity_changes
        if not isinstance(chapter_idx, int) or isinstance(chapter_idx, bool) or chapter_idx <= 0:
            raise ValueError("实体变化必须绑定有效全书章号")
        if not chapter_text or not chapter_text.strip():
            raise ValueError("实体变化必须绑定非空正文")
        validate_entity_changes(data, chapter_text)

        # Process new characters
        for char_info in data.get("new_characters", []):
            name = char_info.get("name")
            if not name:
                continue
            await self.add_character(project_id, name)
            await self.update_character_state(
                project_id, name, chapter_idx, char_info["state"]
            )

        # Process state changes
        for change in data.get("state_changes", []):
            name = change.get("name")
            changes = change.get("changes")
            if not name or not changes:
                continue
            # A missed prior extraction must not make MATCH silently drop this change.
            await self.add_character(project_id, name)
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
                    project_id, c1, c2, rtype, chapter_idx, evidence=rel['evidence'],
                )

        # Process changed relationships
        for rel in data.get("changed_relationships", []):
            c1 = rel.get("char1")
            c2 = rel.get("char2")
            rtype = rel.get("type")
            if c1 and c2 and rtype:
                await self.update_relationship(
                    project_id, c1, c2, rtype, chapter_idx, evidence=rel['evidence'],
                    replaces=rel.get('replaces'),
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
