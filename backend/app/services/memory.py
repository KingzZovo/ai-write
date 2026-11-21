"""
Hierarchical Memory System

5-layer memory pyramid for ultra-long novels (200-500万字):

L1: World Rules (永不衰减) — from Neo4j WorldRule + PostgreSQL settings
L2: Long-term  (卷级摘要) — summaries from earlier volumes only
L3: Mid-term   (章级摘要) — current volume full + historical search via Qdrant
L4: Short-term (段落级)   — current + previous chapter full text
L5: Entity Timeline       — character/relationship state from Neo4j
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from neo4j import AsyncDriver
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import (
    Chapter,
    Project,
    Volume,
)
from app.services import qdrant_store as qs
from app.services.summary_provenance import current_summary_sql
from app.services.entity_timeline import EntityTimelineService
from app.services.feature_extractor import generate_embedding

logger = logging.getLogger(__name__)

# Chapter-summary collection naming is owned by services/qdrant_store
# (per-project shards with legacy-global read fallback).


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MemoryContext:
    """Assembled memory from all 5 layers for chapter generation context."""

    world_state: str = ""         # L1 — world rules + project settings
    volume_summaries: str = ""    # L2 — all volume summaries
    chapter_summaries: str = ""   # L3 — current volume full + related historical
    recent_text: str = ""         # L4 — previous + current chapter text
    entity_states: str = ""       # L5 — characters/relationships at this chapter

    def to_prompt_sections(self) -> list[str]:
        """Convert to labelled prompt sections (skip empty layers)."""
        sections: list[str] = []
        if self.world_state:
            sections.append(f"【L1 世界观设定】\n{self.world_state}")
        if self.volume_summaries:
            sections.append(f"【L2 卷级摘要】\n{self.volume_summaries}")
        if self.chapter_summaries:
            sections.append(f"【L3 章级摘要】\n{self.chapter_summaries}")
        if self.recent_text:
            sections.append(f"【L4 近文上下文】\n{self.recent_text}")
        if self.entity_states:
            sections.append(f"【L5 实体状态】\n{self.entity_states}")
        return sections

    def to_system_prompt(self) -> str:
        """Merge all layers into a single system prompt string."""
        return "\n\n".join(self.to_prompt_sections())


# ---------------------------------------------------------------------------
# Hierarchical memory service
# ---------------------------------------------------------------------------

class HierarchicalMemory:
    """Assembles memory from all 5 layers for chapter generation context."""

    def __init__(
        self,
        db: AsyncSession,
        neo4j_driver: AsyncDriver | None = None,
        qdrant_client: AsyncQdrantClient | None = None,
    ) -> None:
        self.db = db
        self.neo4j_driver = neo4j_driver
        self.qdrant = qdrant_client

    # ==================================================================
    # Public API
    # ==================================================================

    async def assemble(
        self,
        project_id: str,
        current_volume_id: str,
        current_chapter_idx: int,
        user_instruction: str = "",
    ) -> MemoryContext:
        """
        Gather all memory layers for the current generation point.

        Returns MemoryContext with:
        - world_state:       str (L1)
        - volume_summaries:  str (L2 - earlier volumes)
        - chapter_summaries: str (L3 - current volume full + related historical)
        - recent_text:       str (L4 - previous + current chapter)
        - entity_states:     str (L5 - characters in current context)
        """
        ctx = MemoryContext()

        # L1 — World rules (never decay)
        ctx.world_state = await self._gather_world_state(project_id)

        # L2 — Earlier volumes; current/future rollups can reveal later events.
        ctx.volume_summaries = await self._gather_volume_summaries(
            project_id, current_volume_id
        )

        # L3 — Chapter summaries (current volume full + vector-recalled historical)
        ctx.chapter_summaries = await self._gather_chapter_summaries(
            project_id, current_volume_id, current_chapter_idx
        )

        # L4 — Short-term window (previous + current chapter text)
        ctx.recent_text = await self._gather_recent_text(
            current_volume_id, current_chapter_idx
        )

        # L5 — Entity timeline from Neo4j. ``current_chapter_idx`` is
        # volume-LOCAL (every layer above filters by volume_id, where local
        # is correct), but Neo4j HAS_STATE / RELATES_TO / AT_LOCATION edges
        # are written on the book-GLOBAL axis (entity_tasks resolves
        # global_idx before calling extract_and_update), so the snapshot
        # lookup must be converted to the global axis first.
        global_idx = await self._resolve_global_idx(
            current_volume_id, current_chapter_idx
        )
        ctx.entity_states = await self._gather_entity_states(
            project_id, global_idx
        )

        logger.info(
            "Memory assembled for project=%s vol=%s ch=%d  "
            "L1=%d L2=%d L3=%d L4=%d L5=%d chars",
            project_id,
            current_volume_id,
            current_chapter_idx,
            len(ctx.world_state),
            len(ctx.volume_summaries),
            len(ctx.chapter_summaries),
            len(ctx.recent_text),
            len(ctx.entity_states),
        )
        return ctx

    async def _resolve_global_idx(
        self,
        volume_id: str,
        chapter_idx: int,
    ) -> int:
        """Convert a volume-local chapter_idx into the book-global index.

        Reads ``Chapter.global_idx`` (stamped by the before_insert listener,
        backfilled by migration a1001915). Falls back to the local value when
        the chapter is missing or predates the backfill (NULL global_idx) —
        exact for single-volume projects, where global == local.
        """
        try:
            result = await self.db.execute(
                select(Chapter.global_idx).where(
                    Chapter.volume_id == volume_id,
                    Chapter.chapter_idx == chapter_idx,
                )
            )
            global_idx = result.scalar_one_or_none()
            if global_idx:
                return int(global_idx)
        except Exception as e:
            logger.warning("Failed to resolve global chapter idx: %s", e)
        return int(chapter_idx)

    # ==================================================================
    # L1 — World Rules
    # ==================================================================

    async def _gather_world_state(self, project_id: str) -> str:
        """Project references plus complete current graph settings."""
        parts: list[str] = []

        try:
            # Project settings
            proj_result = await self.db.execute(
                select(Project).where(Project.id == project_id)
            )
            project = proj_result.scalar_one_or_none()
            if project:
                if project.premise:
                    parts.append(f"作品前提：{project.premise}")
                if project.genre:
                    parts.append(f"类型：{project.genre}")
            from app.services.world_rule_sources import read_world_rule_source
            from app.services.world_rule_material import render_world_rules
            parts.extend(render_world_rules(await read_world_rule_source(self.db, project_id), project_id))
        except Exception as e:
            from app.services.context_errors import RequiredContextUnavailable
            if isinstance(e, RequiredContextUnavailable):
                raise
            raise RequiredContextUnavailable('world_rules_context_unavailable') from e

        return "\n".join(parts)

    # ==================================================================
    # L2 — Volume Summaries
    # ==================================================================

    async def _gather_volume_summaries(self, project_id: str, current_volume_id: str) -> str:
        """Only completed earlier volumes may supply historical summaries."""
        parts: list[str] = []

        try:
            from app.services.aggregate_memory import current_volume_summaries
            target = (await self.db.execute(select(Volume.volume_idx).where(
                Volume.id == current_volume_id, Volume.project_id == project_id,
            ).execution_options(autoflush=False))).scalar_one_or_none()
            if target is None:
                return ""
            indices = (await self.db.execute(select(Chapter.global_idx).where(
                Chapter.volume_id == current_volume_id,
            ).execution_options(autoflush=False))).scalars().all()
            boundary = min(indices) if indices and all(isinstance(i, int) and i > 0 for i in indices) else 0
            rows = await current_volume_summaries(
                self.db, project_id, before_volume_idx=target, before_global_idx=boundary,
            )
            for row in rows:
                source = row['manifest']
                parts.append(f"[VOL-{source['volume_idx']}]《{source['title']}》摘要：\n{row['text']}")
        except Exception as e:
            logger.warning("Failed to gather volume summaries: %s", e)

        return "\n\n".join(parts)

    # ==================================================================
    # L3 — Chapter Summaries
    # ==================================================================

    async def _gather_chapter_summaries(
        self,
        project_id: str,
        volume_id: str,
        chapter_idx: int,
    ) -> str:
        """
        Current volume's chapter summaries (full injection) +
        vector search for related historical summaries via Qdrant.
        """
        parts: list[str] = []

        try:
            # Current volume: all chapter summaries up to current index
            result = await self.db.execute(
                select(Chapter, current_summary_sql().label("verified_summary"))
                .where(
                    Chapter.volume_id == volume_id,
                    Chapter.chapter_idx < chapter_idx,
                )
                .order_by(Chapter.chapter_idx)
            )
            current_chapters = result.all()

            if current_chapters:
                parts.append("=== 本卷章节摘要 ===")
                for ch, summary in current_chapters:
                    parts.append(
                        f"[CH-{ch.chapter_idx}; status={ch.status}]《{ch.title}》："
                        + ("[待审草稿参考，不视为既定事实] " if ch.status != "completed" else "")
                        + (summary or "摘要缺失或来源失效，不可推定情节已完成。")
                    )

            # Vector search for related historical summaries from other volumes
            if self.qdrant and current_chapters:
                from app.services.memory_chronology import resolve_history_boundary

                target_idx = await resolve_history_boundary(
                    self.db, project_id, volume_id, chapter_idx
                )
                related = await self._search_related_summaries(
                    project_id, volume_id, current_chapters[-1][1] or "",
                    before_global_idx=target_idx if qs.valid_global_idx(target_idx) else 0,
                )
                if related:
                    parts.append("\n=== 相关历史章节摘要 ===")
                    parts.extend(related)

        except Exception as e:
            logger.warning("Failed to gather chapter summaries: %s", e)

        return "\n".join(parts)

    async def _search_related_summaries(
        self,
        project_id: str,
        current_volume_id: str,
        query_text: str,
        top_k: int = 5,
        *,
        before_global_idx: int = 0,
    ) -> list[str]:
        """Search chapter-summary memory related to query_text.

        Two-tier recall via qdrant_store.search_project_summaries: the
        project's live shard (excluding compacted=true points, legacy-global
        fallback pre-migration) merged with the compacted tier (slight score
        penalty). The current volume is excluded — its summaries are already
        fully injected above.
        """
        if not self.qdrant or not query_text.strip() or not qs.valid_global_idx(before_global_idx):
            return []

        try:
            query_vector = await generate_embedding(query_text)

            hits = await qs.search_project_summaries(
                self.qdrant,
                project_id,
                query_vector,
                limit=top_k * 5,
                score_threshold=0.5,
                exclude_volume_id=current_volume_id,
                before_global_idx=before_global_idx,
            )

            from app.services.vector_provenance import filter_current_memory
            hits = await filter_current_memory(
                self.db, project_id, hits, before_global_idx=before_global_idx
            )
            summaries: list[str] = []
            for hit in hits[:top_k]:
                payload = hit.get("payload") or {}
                label = payload.get("label", "")
                if not label and hit.get("tier") == "compacted":
                    rng = payload.get("source_chapter_range") or []
                    if isinstance(rng, list) and len(rng) == 2:
                        label = f"[压缩记忆 CH-{rng[0]}~{rng[1]}]"
                    else:
                        label = "[压缩记忆]"
                text = payload.get("summary", "")
                if text:
                    summaries.append(f"[相关度{hit['score']:.2f}] {label}: {text}")
            return summaries

        except Exception as e:
            logger.warning("Qdrant search failed: %s", e)
            return []

    # ==================================================================
    # L4 — Short-term Window
    # ==================================================================

    async def _gather_recent_text(
        self,
        volume_id: str,
        chapter_idx: int,
    ) -> str:
        """Previous chapter + current chapter content."""
        parts: list[str] = []

        try:
            # Previous chapter
            if chapter_idx > 0:
                prev_result = await self.db.execute(
                    select(Chapter).where(
                        Chapter.volume_id == volume_id,
                        Chapter.chapter_idx == chapter_idx - 1,
                    )
                )
                prev_chapter = prev_result.scalar_one_or_none()
                if prev_chapter and prev_chapter.content_text:
                    parts.append(
                        f"【上一章 [CH-{prev_chapter.chapter_idx}]"
                        f"《{prev_chapter.title}》】\n{prev_chapter.content_text}"
                    )

            # Current chapter (existing content, if any)
            curr_result = await self.db.execute(
                select(Chapter).where(
                    Chapter.volume_id == volume_id,
                    Chapter.chapter_idx == chapter_idx,
                )
            )
            curr_chapter = curr_result.scalar_one_or_none()
            if curr_chapter and curr_chapter.content_text:
                parts.append(
                    f"【本章已有内容 [CH-{curr_chapter.chapter_idx}]"
                    f"《{curr_chapter.title}》】\n{curr_chapter.content_text}"
                )

        except Exception as e:
            logger.warning("Failed to gather recent text: %s", e)

        return "\n\n".join(parts)

    # ==================================================================
    # L5 — Entity Timeline (Neo4j)
    # ==================================================================

    async def _gather_entity_states(
        self,
        project_id: str,
        chapter_idx: int,
    ) -> str:
        """Query Neo4j for characters/relationships active at this chapter.

        ``chapter_idx`` must be BOOK-GLOBAL: the HAS_STATE / RELATES_TO /
        AT_LOCATION / MEMBER_OF edge windows queried by get_world_snapshot
        are written on the global axis by entity_tasks.
        """
        if not self.neo4j_driver:
            return ""

        try:
            from app.services.entity_context import verified_world_snapshot
            snapshot = await verified_world_snapshot(self.db, self.neo4j_driver, project_id, chapter_idx)

            parts: list[str] = []

            # Characters
            if snapshot.characters:
                parts.append("角色状态：")
                for char in snapshot.characters:
                    status_str = json.dumps(
                        char.status, ensure_ascii=False
                    ) if char.status else "无详细状态"
                    parts.append(f"  - {char.name}: {status_str}")

            # Relationships
            if snapshot.relationships:
                parts.append("角色关系：")
                from app.services.relationship_context import render_relationship_record
                for rel in snapshot.relationships:
                    parts.append('  ' + render_relationship_record(rel))

            # Locations
            if snapshot.locations:
                parts.append(f"活跃地点：{', '.join(snapshot.locations)}")

            # Organizations
            if snapshot.organizations:
                parts.append(f"活跃组织：{', '.join(snapshot.organizations)}")

            return "\n".join(parts)

        except Exception as e:
            logger.warning("Failed to gather entity states: %s", e)
            return ""

    # ==================================================================
    # Summary generation
    # ==================================================================

    async def generate_chapter_summary(
        self,
        chapter_text: str,
        chapter_idx: int,
        project_id: str | None = None,
        volume_id: str | None = None,
        chapter_title: str = "",
    ) -> str:
        """Use shared summary validation; identified sources commit before projection."""
        from app.services.chapter_summarizer import (
            summarize_chapter_text, summarize_scoped_chapter,
        )

        if project_id is not None or volume_id is not None:
            if project_id is None or volume_id is None:
                return ""
            _, summary = await summarize_scoped_chapter(
                db=self.db, project_id=project_id, volume_id=volume_id,
                chapter_idx=chapter_idx, chapter_text=chapter_text,
                project_vectors=self.qdrant is not None,
            )
            return summary
        return await summarize_chapter_text(
            title=chapter_title, content_text=chapter_text, db=self.db,
        )

    async def generate_volume_summary(self, volume_id: str) -> str:
        """Generate only from complete, verified chapter memory through Registry."""
        from app.services.aggregate_memory import generate_volume_memory
        try:
            _, summary = await generate_volume_memory(self.db, volume_id)
            return summary
        except Exception as exc:
            logger.warning("Volume summary failed: %s", exc)
            return ""


# ---------------------------------------------------------------------------
# Cross-volume memory backfill
# ---------------------------------------------------------------------------


async def backfill_prev_volume_summary(volume_id: str) -> bool:
    """Refresh the preceding volume and book memory from verified sources.

    Owns its session. Returns True only when a volume summary was written;
    a reused volume may still trigger repair of a missing book synopsis.
    """
    try:
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            volume = await db.get(Volume, str(volume_id))
            if volume is None:
                return False
            prev_result = await db.execute(
                select(Volume)
                .where(
                    Volume.project_id == volume.project_id,
                    Volume.volume_idx < volume.volume_idx,
                )
                .order_by(Volume.volume_idx.desc())
                .limit(1)
            )
            prev_volume = prev_result.scalar_one_or_none()
            if prev_volume is None:
                return False
            from app.services.aggregate_memory import generate_volume_memory
            project_id, previous_id = str(volume.project_id), str(prev_volume.id)
            written, summary = await generate_volume_memory(db, previous_id, persist=True)
            if summary:
                # Also repairs a missing synopsis after a prior partial failure.
                await regenerate_book_synopsis(project_id, db)
            return written
    except Exception as e:
        logger.warning(
            "backfill_prev_volume_summary failed (volume_id=%s): %s", volume_id, e
        )
        return False


# ---------------------------------------------------------------------------
# Hierarchical rollup — rolling book synopsis (全书至此梗概)
# ---------------------------------------------------------------------------

async def regenerate_book_synopsis(project_id: str, db: AsyncSession) -> str:
    """Refresh source-bound book memory; failure never breaks chapter persistence."""
    from app.services.aggregate_memory import generate_book_memory
    try:
        return await generate_book_memory(db, project_id)
    except Exception as exc:
        logger.warning("Book synopsis failed (project=%s): %s", project_id, exc)
        return ""
