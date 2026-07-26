"""
Hierarchical Memory System

5-layer memory pyramid for ultra-long novels (200-500万字):

L1: World Rules (永不衰减) — from Neo4j WorldRule + PostgreSQL settings
L2: Long-term  (卷级摘要) — full injection of all volume summaries
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
from qdrant_client.models import PointStruct
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import (
    Chapter,
    Project,
    Volume,
    VolumeSummary,
    WorldRule,
)
from app.services import qdrant_store as qs
from app.services.entity_timeline import EntityTimelineService
from app.services.feature_extractor import generate_embedding
from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

# Chapter-summary collection naming is owned by services/qdrant_store
# (per-project shards with legacy-global read fallback).
# Dimension of the configured embedding model. The live collections are 2048
# (nvidia/llama-nemotron-embed-vl-1b-v2); the old 1536 (text-embedding-3-small)
# value would create an incompatible collection on a fresh install.
EMBEDDING_DIM = 2048

# projects.settings_json key holding the rolling 全书至此梗概.
BOOK_SYNOPSIS_KEY = "book_synopsis"
BOOK_SYNOPSIS_MAX_CHARS = 800


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
# LLM summary prompts
# ---------------------------------------------------------------------------

CHAPTER_SUMMARY_PROMPT = """\
你是一个小说分析助手。请为以下章节内容生成一段简洁的摘要（150-300字），包含：
1. 主要情节发展
2. 出场人物及状态变化
3. 重要伏笔或转折

章节编号：[CH-{chapter_idx}]

章节内容：
{chapter_text}

请直接输出摘要文本，不要添加标题或格式标记。"""

VOLUME_SUMMARY_PROMPT = """\
你是一个小说分析助手。请为以下卷的所有章节摘要生成一段卷级综合摘要（300-500字），包含：
1. 本卷核心剧情主线
2. 主要角色成长与关系变化
3. 重要世界观/设定揭示
4. 为后续留下的伏笔

各章摘要：
{chapter_summaries}

请直接输出摘要文本。"""


# ---------------------------------------------------------------------------
# Main service
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
        - volume_summaries:  str (L2 - all volumes)
        - chapter_summaries: str (L3 - current volume full + related historical)
        - recent_text:       str (L4 - previous + current chapter)
        - entity_states:     str (L5 - characters in current context)
        """
        ctx = MemoryContext()

        # L1 — World rules (never decay)
        ctx.world_state = await self._gather_world_state(project_id)

        # L2 — Volume summaries (all volumes)
        ctx.volume_summaries = await self._gather_volume_summaries(project_id)

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
        """Query PostgreSQL for world_rules + project settings."""
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
                if project.settings_json:
                    parts.append(
                        "项目设定：\n"
                        + json.dumps(project.settings_json, ensure_ascii=False, indent=2)
                    )

            # World rules
            rules_result = await self.db.execute(
                select(WorldRule).where(WorldRule.project_id == project_id)
            )
            rules = rules_result.scalars().all()
            for rule in rules:
                parts.append(f"[{rule.category}] {rule.rule_text}")
        except Exception as e:
            logger.warning("Failed to gather world state: %s", e)

        return "\n".join(parts)

    # ==================================================================
    # L2 — Volume Summaries
    # ==================================================================

    async def _gather_volume_summaries(self, project_id: str) -> str:
        """Get all VolumeSummary records for the project."""
        parts: list[str] = []

        try:
            # Join Volume to filter by project
            result = await self.db.execute(
                select(VolumeSummary, Volume.title, Volume.volume_idx)
                .join(Volume, VolumeSummary.volume_id == Volume.id)
                .where(Volume.project_id == project_id)
                .order_by(Volume.volume_idx)
            )
            rows = result.all()
            for vs, vol_title, vol_idx in rows:
                header = f"[VOL-{vol_idx}]《{vol_title}》摘要："
                parts.append(f"{header}\n{vs.summary_text}")
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
                select(Chapter)
                .where(
                    Chapter.volume_id == volume_id,
                    Chapter.chapter_idx < chapter_idx,
                    Chapter.summary.isnot(None),
                )
                .order_by(Chapter.chapter_idx)
            )
            current_chapters = result.scalars().all()

            if current_chapters:
                parts.append("=== 本卷章节摘要 ===")
                for ch in current_chapters:
                    parts.append(
                        f"[CH-{ch.chapter_idx}]《{ch.title}》：{ch.summary}"
                    )

            # Vector search for related historical summaries from other volumes
            if self.qdrant and current_chapters:
                related = await self._search_related_summaries(
                    project_id, volume_id, current_chapters[-1].summary or ""
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
    ) -> list[str]:
        """Search chapter-summary memory related to query_text.

        Two-tier recall via qdrant_store.search_project_summaries: the
        project's live shard (excluding compacted=true points, legacy-global
        fallback pre-migration) merged with the compacted tier (slight score
        penalty). The current volume is excluded — its summaries are already
        fully injected above.
        """
        if not self.qdrant or not query_text.strip():
            return []

        try:
            query_vector = await generate_embedding(query_text)

            hits = await qs.search_project_summaries(
                self.qdrant,
                project_id,
                query_vector,
                limit=top_k,
                score_threshold=0.5,
                exclude_volume_id=current_volume_id,
            )

            summaries: list[str] = []
            for hit in hits:
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
            ets = EntityTimelineService(self.neo4j_driver)
            snapshot = await ets.get_world_snapshot(project_id, chapter_idx)

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
                for rel in snapshot.relationships:
                    parts.append(
                        f"  - {rel.source} <-> {rel.target}: {rel.rel_type}"
                    )

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
        """
        Use LLM to generate a chapter summary after generation.

        Optionally stores the embedding in Qdrant for future vector search.
        """
        if not chapter_text.strip():
            return ""

        router = get_model_router()
        prompt = CHAPTER_SUMMARY_PROMPT.format(
            chapter_idx=chapter_idx,
            chapter_text=chapter_text[:4000],
        )

        try:
            result = await router.generate(
                task_type="summary",
                messages=[
                    {"role": "system", "content": "你是一个小说分析助手。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=512,
            )
            summary = result.text.strip()
        except Exception as e:
            logger.warning("Failed to generate chapter summary: %s", e)
            # Fallback: use first 200 chars
            summary = chapter_text[:200] + "..."

        # Store embedding in Qdrant for future vector search
        if self.qdrant and project_id and volume_id:
            await self._store_chapter_summary_embedding(
                project_id=project_id,
                volume_id=volume_id,
                chapter_idx=chapter_idx,
                chapter_title=chapter_title,
                summary=summary,
            )

        return summary

    async def generate_volume_summary(self, volume_id: str) -> str:
        """Use LLM to generate a volume summary when a volume ends."""
        try:
            result = await self.db.execute(
                select(Chapter)
                .where(
                    Chapter.volume_id == volume_id,
                    Chapter.summary.isnot(None),
                )
                .order_by(Chapter.chapter_idx)
            )
            chapters = result.scalars().all()

            if not chapters:
                return ""

            chapter_summaries_text = "\n".join(
                f"[CH-{ch.chapter_idx}]《{ch.title}》：{ch.summary}"
                for ch in chapters
                if ch.summary
            )

            if not chapter_summaries_text.strip():
                return ""

            router = get_model_router()
            prompt = VOLUME_SUMMARY_PROMPT.format(
                chapter_summaries=chapter_summaries_text
            )

            gen_result = await router.generate(
                task_type="summary",
                messages=[
                    {"role": "system", "content": "你是一个小说分析助手。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1024,
            )
            return gen_result.text.strip()

        except Exception as e:
            logger.warning("Failed to generate volume summary: %s", e)
            return ""

    # ==================================================================
    # Qdrant helpers
    # ==================================================================

    async def _store_chapter_summary_embedding(
        self,
        project_id: str,
        volume_id: str,
        chapter_idx: int,
        chapter_title: str,
        summary: str,
    ) -> None:
        """Store a chapter summary embedding in the project's summary shard."""
        if not self.qdrant:
            return

        try:
            vector = await generate_embedding(summary)
            collection = await qs.ensure_summary_shard(
                self.qdrant, project_id, len(vector) if vector else EMBEDDING_DIM
            )

            # Deterministic point ID based on volume + chapter
            import hashlib

            point_id_str = f"{volume_id}_{chapter_idx}"
            point_id_hash = hashlib.md5(
                point_id_str.encode()
            ).hexdigest()
            # Convert first 16 hex chars to int for Qdrant point ID
            point_id_int = int(point_id_hash[:16], 16)

            label = f"[CH-{chapter_idx}]"
            if chapter_title:
                label += f"《{chapter_title}》"

            await self.qdrant.upsert(
                collection_name=collection,
                points=[
                    PointStruct(
                        id=point_id_int,
                        vector=vector,
                        payload={
                            "project_id": project_id,
                            "volume_id": volume_id,
                            "chapter_idx": chapter_idx,
                            "chapter_title": chapter_title,
                            "summary": summary,
                            "label": label,
                        },
                    )
                ],
            )
        except Exception as e:
            logger.warning("Failed to store chapter summary embedding: %s", e)


# ---------------------------------------------------------------------------
# Cross-volume memory backfill
# ---------------------------------------------------------------------------


async def backfill_prev_volume_summary(volume_id: str) -> bool:
    """Ensure the volume BEFORE ``volume_id`` has a VolumeSummary row.

    Cross-volume memory fix: VolumeSummary rows previously had zero writers, so
    the L2 bridge in context_pack (prev-volume summaries injected for chapters
    1-3 of each volume) was always empty. Called fire-and-forget after each
    chapter save; a no-op unless the previous volume exists, has chapter
    summaries, and lacks a VolumeSummary row — so the LLM call fires at most
    once per volume transition.

    Opens its own DB session (safe to run detached from the save transaction).
    Never raises. Returns True only when a new row was written.
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
            existing = await db.execute(
                select(VolumeSummary.id)
                .where(VolumeSummary.volume_id == prev_volume.id)
                .limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                return False

            memory = HierarchicalMemory(db)
            summary_text = await memory.generate_volume_summary(str(prev_volume.id))
            if not summary_text.strip():
                return False

            # Re-check before insert: a concurrent save may have backfilled
            # while the LLM call was in flight.
            recheck = await db.execute(
                select(VolumeSummary.id)
                .where(VolumeSummary.volume_id == prev_volume.id)
                .limit(1)
            )
            if recheck.scalar_one_or_none() is not None:
                return False

            db.add(VolumeSummary(volume_id=prev_volume.id, summary_text=summary_text))
            await db.commit()
            logger.info(
                "Backfilled VolumeSummary for volume %s (project %s)",
                prev_volume.id, volume.project_id,
            )
            # Hierarchical rollup: a new VolumeSummary exists — refresh the
            # rolling 全书至此梗概. Failure-tolerant (returns "" on error);
            # the backfill above is already committed either way.
            await regenerate_book_synopsis(str(volume.project_id), db)
            return True
    except Exception as e:
        logger.warning(
            "backfill_prev_volume_summary failed (volume_id=%s): %s", volume_id, e
        )
        return False


# ---------------------------------------------------------------------------
# Hierarchical rollup — rolling book synopsis (全书至此梗概)
# ---------------------------------------------------------------------------

_BOOK_SYNOPSIS_PROMPT = """\
请将以下各卷梗概压缩成一段「全书至此梗概」（不超过 {max_chars} 个中文字）。
要求：
1. 按时间顺序覆盖全部卷的主线推进，重点保留仍影响后续剧情的事件与人物关系。
2. 只写事实，不评价、不总结主题，不用“本书讲述了”这类套话。
3. 直接输出梗概正文，不加标题或格式标记。

各卷梗概：
{volume_summaries}"""


async def regenerate_book_synopsis(project_id: str, db: AsyncSession) -> str:
    """Regenerate the rolling 全书至此梗概 from all VolumeSummary rows.

    One LLM call (prompt-registry task_type="summary", same as the chapter
    summarizer) over every volume summary, truncated to
    ``BOOK_SYNOPSIS_MAX_CHARS``.

    Storage: ``projects.settings_json["book_synopsis"]`` =
    ``{"text", "source_volumes", "updated_at"}``. Chosen over an Outline
    content_json namespace because Outline rows are versioned, level-keyed
    and regenerated wholesale by outline_generator (which owns their
    schema) — a synopsis stashed there could be silently dropped on outline
    regeneration. settings_json already carries project-scoped derived
    state (style_reference etc.) and ContextPack reads it in one Project
    fetch. No new tables.

    Never raises; returns the new synopsis text or "" on any failure.
    """
    try:
        rows = (
            await db.execute(
                select(
                    VolumeSummary.summary_text, Volume.volume_idx, Volume.title
                )
                .join(Volume, VolumeSummary.volume_id == Volume.id)
                .where(Volume.project_id == project_id)
                .order_by(Volume.volume_idx)
            )
        ).all()
        parts = [
            f"[VOL-{vol_idx}]《{vol_title or ''}》：{text}"
            for text, vol_idx, vol_title in rows
            if text and text.strip()
        ]
        if not parts:
            return ""

        from app.services.prompt_registry import run_text_prompt

        result = await run_text_prompt(
            task_type="summary",
            user_content=_BOOK_SYNOPSIS_PROMPT.format(
                max_chars=BOOK_SYNOPSIS_MAX_CHARS,
                volume_summaries="\n\n".join(parts),
            ),
            db=db,
            project_id=project_id,
        )
        text = (result.text or "").strip()
        if not text:
            return ""
        if len(text) > BOOK_SYNOPSIS_MAX_CHARS:
            text = text[:BOOK_SYNOPSIS_MAX_CHARS].rstrip() + "…"

        project = await db.get(Project, str(project_id))
        if project is None:
            return ""
        from datetime import datetime, timezone

        settings_json = dict(project.settings_json or {})
        settings_json[BOOK_SYNOPSIS_KEY] = {
            "text": text,
            "source_volumes": len(parts),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        project.settings_json = settings_json
        await db.commit()
        logger.info(
            "Regenerated book synopsis for project %s (%d volumes, %d chars)",
            project_id, len(parts), len(text),
        )
        return text
    except Exception as e:
        logger.warning(
            "regenerate_book_synopsis failed (project_id=%s): %s", project_id, e
        )
        return ""
