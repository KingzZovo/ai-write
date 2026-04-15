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
from dataclasses import dataclass, field
from typing import Any

from neo4j import AsyncDriver
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import (
    Chapter,
    Project,
    Volume,
    VolumeSummary,
    WorldRule,
)
from app.services.entity_timeline import EntityTimelineService
from app.services.feature_extractor import generate_embedding
from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

# Qdrant collection for chapter summary embeddings
CHAPTER_SUMMARY_COLLECTION = "chapter_summaries"
EMBEDDING_DIM = 1536  # text-embedding-3-small


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

章节编号：第{chapter_idx}章

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

        # L5 — Entity timeline from Neo4j
        ctx.entity_states = await self._gather_entity_states(
            project_id, current_chapter_idx
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
                header = f"第{vol_idx}卷《{vol_title}》摘要："
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
                        f"第{ch.chapter_idx}章《{ch.title}》：{ch.summary}"
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
        """Search Qdrant for chapter summaries related to query_text."""
        if not self.qdrant or not query_text.strip():
            return []

        try:
            # Ensure collection exists
            await self._ensure_qdrant_collection()

            query_vector = await generate_embedding(query_text)

            from qdrant_client.models import FieldCondition, Filter, MatchValue

            results = await self.qdrant.search(
                collection_name=CHAPTER_SUMMARY_COLLECTION,
                query_vector=query_vector,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="project_id",
                            match=MatchValue(value=project_id),
                        ),
                    ],
                    must_not=[
                        FieldCondition(
                            key="volume_id",
                            match=MatchValue(value=current_volume_id),
                        ),
                    ],
                ),
                limit=top_k,
                score_threshold=0.5,
            )

            summaries: list[str] = []
            for hit in results:
                payload = hit.payload or {}
                label = payload.get("label", "")
                text = payload.get("summary", "")
                if text:
                    summaries.append(f"[相关度{hit.score:.2f}] {label}: {text}")
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
                        f"【上一章 第{prev_chapter.chapter_idx}章"
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
                    f"【本章已有内容 第{curr_chapter.chapter_idx}章"
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
        """Query Neo4j for characters/relationships active at this chapter."""
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
                f"第{ch.chapter_idx}章《{ch.title}》：{ch.summary}"
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

    async def _ensure_qdrant_collection(self) -> None:
        """Create the chapter_summaries collection if it doesn't exist."""
        if not self.qdrant:
            return

        try:
            await self.qdrant.get_collection(CHAPTER_SUMMARY_COLLECTION)
        except (UnexpectedResponse, Exception):
            try:
                await self.qdrant.create_collection(
                    collection_name=CHAPTER_SUMMARY_COLLECTION,
                    vectors_config=VectorParams(
                        size=EMBEDDING_DIM,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info(
                    "Created Qdrant collection: %s", CHAPTER_SUMMARY_COLLECTION
                )
            except Exception as e:
                logger.warning("Failed to create Qdrant collection: %s", e)

    async def _store_chapter_summary_embedding(
        self,
        project_id: str,
        volume_id: str,
        chapter_idx: int,
        chapter_title: str,
        summary: str,
    ) -> None:
        """Store a chapter summary embedding in Qdrant."""
        if not self.qdrant:
            return

        try:
            await self._ensure_qdrant_collection()

            vector = await generate_embedding(summary)

            # Deterministic point ID based on volume + chapter
            import hashlib

            point_id_str = f"{volume_id}_{chapter_idx}"
            point_id_hash = hashlib.md5(
                point_id_str.encode()
            ).hexdigest()
            # Convert first 16 hex chars to int for Qdrant point ID
            point_id_int = int(point_id_hash[:16], 16)

            label = f"第{chapter_idx}章"
            if chapter_title:
                label += f"《{chapter_title}》"

            await self.qdrant.upsert(
                collection_name=CHAPTER_SUMMARY_COLLECTION,
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
