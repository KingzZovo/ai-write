"""
Three-Layer Context Pack Builder

Replaces the simple ContextAssembler with a structured 3-layer context pack system
designed for long-form Chinese web novel generation.

Layer 1: Proximity Layer (~40% tokens)
- Last 5 chapter summaries (not full text)
- Current chapter existing content (full)
- Current chapter outline
- Next 10 chapters outline direction

Layer 2: Fact Layer (~33% tokens) -- Unified Truth Source
- World rules (immutable)
- Active character cards (SCORE dynamic tracking):
  {name, location, power_level, relationships, mental_state, recent_actions}
- Foreshadow triplets CFPG: (Cause, Foreshadow, Payoff Goal)
- Timeline anchors (DOME simplified):
  {chapter_idx, key_time_event, causal_chain}
- Contradiction cache (known conflicts to avoid)

Layer 3: RAG Layer (~20% tokens)
- Keyword-triggered retrieval (CoKe pattern):
  Extract key entities from outline -> search Qdrant
- Key item/location description snippets
- Character dialogue style samples (2-3 typical lines per character)
- Style few-shot samples

+ Instructions/Style: ~7% tokens
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.project import (
    Chapter,
    Character,
    Foreshadow,
    Outline,
    Project,
    Volume,
    VolumeSummary,
    WorldRule,
)

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 1.5  # conservative estimate for Chinese text


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CharacterCard:
    """SCORE Dynamic State Tracking for a character.

    SCORE = State, Connections, Objectives, Reactions, Evolution
    """

    name: str
    location: str = ""
    power_level: str = ""
    relationships: dict[str, str] = field(default_factory=dict)
    mental_state: str = ""  # Theory of Mind (ToM)
    recent_actions: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        parts = [f"[{self.name}]"]
        if self.location:
            parts.append(f"位置:{self.location}")
        if self.power_level:
            parts.append(f"实力:{self.power_level}")
        if self.relationships:
            rels = ", ".join(f"{k}:{v}" for k, v in self.relationships.items())
            parts.append(f"关系:{rels}")
        if self.mental_state:
            parts.append(f"心理:{self.mental_state}")
        if self.recent_actions:
            parts.append(f"近期:{'; '.join(self.recent_actions[-3:])}")
        return " | ".join(parts)


@dataclass
class CFPGTriplet:
    """Foreshadow Triplet (Cause, Foreshadow, Payoff Goal).

    Tracks the full lifecycle of narrative foreshadowing with
    proximity-based status indicators.
    """

    cause: str
    foreshadow: str
    payoff_goal: str
    proximity: float = 0.0

    def to_prompt(self) -> str:
        if self.proximity > 0.7:
            status = "[!!!接近消解]"
        elif self.proximity > 0.3:
            status = "[~~发酵中]"
        else:
            status = "[--已埋设]"
        return f"{status} {self.foreshadow} (因:{self.cause} -> 目标:{self.payoff_goal})"


@dataclass
class TimeAnchor:
    """DOME Timeline Anchor.

    Simplified timeline tracking for maintaining temporal coherence
    across the novel.
    """

    chapter_idx: int
    event: str
    causal_chain: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        chain_str = " -> ".join(self.causal_chain) if self.causal_chain else ""
        parts = [f"第{self.chapter_idx}章: {self.event}"]
        if chain_str:
            parts.append(f"  因果链: {chain_str}")
        return "\n".join(parts)


@dataclass
class StrandTracker:
    """Three-strand weave pattern tracking.

    Monitors the balance of three narrative strands:
    - Quest: main plot advancement, battles, challenges
    - Fire: emotional/relationship developments
    - Constellation: worldbuilding, power system revelations

    Alerts when any strand has been dormant too long.
    """

    last_quest_chapter: int = 0
    last_fire_chapter: int = 0
    last_constellation_chapter: int = 0
    current_dominant: str = "quest"  # quest / fire / constellation

    def get_warnings(self, current_chapter: int) -> list[str]:
        warnings: list[str] = []
        quest_gap = current_chapter - self.last_quest_chapter
        fire_gap = current_chapter - self.last_fire_chapter
        constellation_gap = current_chapter - self.last_constellation_chapter

        if quest_gap > 5:
            warnings.append(
                f"[Quest线] 已{quest_gap}章未推进主线剧情，读者可能失去方向感"
            )
        if fire_gap > 10:
            warnings.append(
                f"[Fire线] 已{fire_gap}章未出现感情/情感戏，建议安排人物互动"
            )
        if constellation_gap > 15:
            warnings.append(
                f"[Constellation线] 已{constellation_gap}章未展示新世界观设定，建议揭示新设定"
            )
        return warnings

    def to_prompt(self) -> str:
        return (
            f"当前主导线: {self.current_dominant} | "
            f"Quest最后出现: 第{self.last_quest_chapter}章 | "
            f"Fire最后出现: 第{self.last_fire_chapter}章 | "
            f"Constellation最后出现: 第{self.last_constellation_chapter}章"
        )


# ---------------------------------------------------------------------------
# Context Pack
# ---------------------------------------------------------------------------


@dataclass
class ContextPack:
    """Complete context pack for chapter generation.

    Organizes all information into three layers with clear token budgets
    to maximize the LLM's context utilization efficiency.
    """

    # Layer 1: Proximity (~40%)
    recent_summaries: list[str] = field(default_factory=list)  # last 5 chapters
    current_content: str = ""
    current_outline: dict = field(default_factory=dict)
    future_outlines: list[str] = field(default_factory=list)  # next 10 chapters

    # Layer 2: Facts (~33%)
    world_rules: list[str] = field(default_factory=list)
    character_cards: list[CharacterCard] = field(default_factory=list)
    foreshadow_triplets: list[CFPGTriplet] = field(default_factory=list)
    timeline_anchors: list[TimeAnchor] = field(default_factory=list)
    contradiction_cache: list[str] = field(default_factory=list)
    strand_tracker: StrandTracker = field(default_factory=StrandTracker)
    # v0.8 ContextPack v3: 4th recall path, scoped to project.genre_profile.
    writing_rules: list[str] = field(default_factory=list)

    # Layer 3: RAG (~20%)
    rag_snippets: list[str] = field(default_factory=list)
    dialogue_samples: dict[str, list[str]] = field(default_factory=dict)
    style_samples: list[str] = field(default_factory=list)

    # Meta (~7%)
    writing_guidance: list[str] = field(default_factory=list)
    hook_suggestion: str = ""

    def _estimate_tokens(self, text: str) -> int:
        return int(len(text) / CHARS_PER_TOKEN)

    def _truncate_to_budget(self, text: str, token_budget: int) -> str:
        """Truncate text to fit within token budget, keeping the end."""
        char_limit = int(token_budget * CHARS_PER_TOKEN)
        if len(text) <= char_limit:
            return text
        return "...(前文已截断)...\n" + text[-char_limit:]

    def to_system_prompt(self, token_budget: int = 8000) -> str:
        """Build the system prompt from all layers with budget allocation.

        Budget distribution:
        - Layer 1 (Proximity): 40% = ~3200 tokens
        - Layer 2 (Facts):     33% = ~2640 tokens
        - Layer 3 (RAG):       20% = ~1600 tokens
        - Instructions:         7% = ~560 tokens
        """
        budget_l1 = int(token_budget * 0.40)
        budget_l2 = int(token_budget * 0.33)
        budget_l3 = int(token_budget * 0.20)
        budget_meta = int(token_budget * 0.07)

        sections: list[str] = []

        # ---- Layer 1: Proximity ----
        l1_parts: list[str] = []

        if self.recent_summaries:
            summaries_text = "\n".join(
                f"第{i}章前: {s}" for i, s in enumerate(self.recent_summaries, 1)
            )
            l1_parts.append(f"【近五章摘要】\n{summaries_text}")

        if self.current_content:
            l1_parts.append(f"【本章已有内容】\n{self.current_content}")

        if self.current_outline:
            outline_str = json.dumps(self.current_outline, ensure_ascii=False, indent=2)
            l1_parts.append(f"【本章大纲】\n{outline_str}")

        if self.future_outlines:
            future_text = "\n".join(
                f"后续第{i}章方向: {o}" for i, o in enumerate(self.future_outlines, 1)
            )
            l1_parts.append(f"【后续走向(参考)】\n{future_text}")

        l1_text = "\n\n".join(l1_parts)
        l1_text = self._truncate_to_budget(l1_text, budget_l1)
        if l1_text:
            sections.append(f"=== 叙事上下文 ===\n{l1_text}")

        # ---- Layer 2: Facts ----
        l2_parts: list[str] = []

        if self.world_rules:
            rules_text = "\n".join(f"- {r}" for r in self.world_rules)
            l2_parts.append(f"【世界规则(不可违反)】\n{rules_text}")

        if self.writing_rules:
            wr_text = "\n".join(f"- {r}" for r in self.writing_rules)
            l2_parts.append(f"【写作规则(必须遵守)】\n{wr_text}")

        if self.character_cards:
            cards_text = "\n".join(c.to_prompt() for c in self.character_cards)
            l2_parts.append(f"【活跃角色状态】\n{cards_text}")

        if self.foreshadow_triplets:
            fs_text = "\n".join(f.to_prompt() for f in self.foreshadow_triplets)
            l2_parts.append(f"【伏笔追踪】\n{fs_text}")

        if self.timeline_anchors:
            tl_text = "\n".join(a.to_prompt() for a in self.timeline_anchors[-10:])
            l2_parts.append(f"【时间线锚点】\n{tl_text}")

        if self.contradiction_cache:
            cc_text = "\n".join(f"- {c}" for c in self.contradiction_cache)
            l2_parts.append(f"【已知矛盾(务必避免)】\n{cc_text}")

        strand_warnings = self.strand_tracker.get_warnings(
            self.current_outline.get("chapter_idx", 0)
        )
        if strand_warnings:
            sw_text = "\n".join(strand_warnings)
            l2_parts.append(f"【线索平衡提醒】\n{sw_text}")

        l2_text = "\n\n".join(l2_parts)
        l2_text = self._truncate_to_budget(l2_text, budget_l2)
        if l2_text:
            sections.append(f"=== 事实约束 ===\n{l2_text}")

        # ---- Layer 3: RAG ----
        l3_parts: list[str] = []

        if self.rag_snippets:
            rag_text = "\n---\n".join(self.rag_snippets[:5])
            l3_parts.append(f"【相关片段】\n{rag_text}")

        if self.dialogue_samples:
            ds_parts: list[str] = []
            for char_name, lines in self.dialogue_samples.items():
                sample_lines = "\n".join(f'  "{line}"' for line in lines[:3])
                ds_parts.append(f"{char_name}:\n{sample_lines}")
            l3_parts.append(f"【角色对话样本】\n" + "\n".join(ds_parts))

        if self.style_samples:
            ss_text = "\n---\n".join(self.style_samples[:3])
            l3_parts.append(f"【风格参考】\n{ss_text}")

        l3_text = "\n\n".join(l3_parts)
        l3_text = self._truncate_to_budget(l3_text, budget_l3)
        if l3_text:
            sections.append(f"=== 细节参考 ===\n{l3_text}")

        # ---- Meta: Instructions ----
        meta_parts: list[str] = []

        if self.writing_guidance:
            wg_text = "\n".join(f"- {g}" for g in self.writing_guidance)
            meta_parts.append(f"【写作指导】\n{wg_text}")

        if self.hook_suggestion:
            meta_parts.append(f"【钩子建议】\n{self.hook_suggestion}")

        meta_text = "\n\n".join(meta_parts)
        meta_text = self._truncate_to_budget(meta_text, budget_meta)
        if meta_text:
            sections.append(f"=== 创作指令 ===\n{meta_text}")

        full_prompt = "\n\n".join(sections)

        estimated = self._estimate_tokens(full_prompt)
        logger.info(
            "ContextPack built: ~%d tokens (budget: %d), "
            "L1=%d chars, L2=%d chars, L3=%d chars, Meta=%d chars",
            estimated,
            token_budget,
            len(l1_text),
            len(l2_text),
            len(l3_text),
            len(meta_text),
        )
        return full_prompt

    def to_messages(self, user_instruction: str = "") -> list[dict]:
        """Convert context pack to LLM message list."""
        system_prompt = self.to_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]

        if user_instruction:
            messages.append({"role": "user", "content": user_instruction})
        else:
            messages.append({
                "role": "user",
                "content": (
                    "请根据以上设定和大纲，生成本章正文内容。"
                    "要求：内容完整连贯，人物言行符合人设，情节推进自然流畅。"
                ),
            })
        return messages


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class ContextPackBuilder:
    """Builds ContextPack from all data sources (PostgreSQL + Neo4j + Qdrant).

    Orchestrates data retrieval from multiple backends and assembles
    the three-layer context pack.
    """

    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db
        self._owns_db = False

    async def _get_db(self) -> AsyncSession:
        if self._db is not None:
            return self._db
        self._db = async_session_factory()
        self._owns_db = True
        return self._db

    async def close(self) -> None:
        if self._owns_db and self._db is not None:
            await self._db.close()
            self._db = None

    async def build(
        self,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession | None = None,
    ) -> ContextPack:
        """Assemble context pack from PostgreSQL + Neo4j + Qdrant.

        Args:
            project_id: The project to build context for.
            volume_id: The volume containing the target chapter.
            chapter_idx: The chapter index being generated.
            db: Optional database session override.

        Returns:
            A fully populated ContextPack.
        """
        if db is not None:
            self._db = db

        pack = ContextPack()

        # Layer 1: Proximity
        await self._build_proximity(pack, project_id, volume_id, chapter_idx)
        # Layer 2: Facts
        await self._build_facts(pack, project_id, chapter_idx)
        # Layer 3: RAG
        await self._build_rag(pack, project_id, chapter_idx)

        # Strand warnings
        warnings = pack.strand_tracker.get_warnings(chapter_idx)
        pack.writing_guidance.extend(warnings)

        return pack

    # ------------------------------------------------------------------
    # Layer 1: Proximity
    # ------------------------------------------------------------------

    async def _build_proximity(
        self,
        pack: ContextPack,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
    ) -> None:
        """Build Layer 1: recent summaries, current content, outlines."""
        db = await self._get_db()
        pid = str(project_id)
        vid = str(volume_id)

        try:
            # Get last 5 chapter summaries
            result = await db.execute(
                select(Chapter.summary, Chapter.chapter_idx)
                .where(
                    Chapter.volume_id == vid,
                    Chapter.chapter_idx < chapter_idx,
                    Chapter.summary.isnot(None),
                    Chapter.summary != "",
                )
                .order_by(Chapter.chapter_idx.desc())
                .limit(5)
            )
            rows = result.all()
            # Reverse to chronological order
            pack.recent_summaries = [
                f"[第{row.chapter_idx}章] {row.summary}"
                for row in reversed(rows)
            ]

            # Get current chapter content
            current_result = await db.execute(
                select(Chapter)
                .where(
                    Chapter.volume_id == vid,
                    Chapter.chapter_idx == chapter_idx,
                )
            )
            current_chapter = current_result.scalar_one_or_none()
            if current_chapter:
                pack.current_content = current_chapter.content_text or ""
                pack.current_outline = current_chapter.outline_json or {}

            # Get next 10 chapters outline direction
            future_result = await db.execute(
                select(Chapter.chapter_idx, Chapter.title, Chapter.outline_json)
                .where(
                    Chapter.volume_id == vid,
                    Chapter.chapter_idx > chapter_idx,
                )
                .order_by(Chapter.chapter_idx.asc())
                .limit(10)
            )
            for row in future_result.all():
                outline_summary = ""
                if row.outline_json:
                    # Extract key direction from outline
                    oj = row.outline_json
                    if isinstance(oj, dict):
                        outline_summary = oj.get("summary", "") or oj.get(
                            "main_plot", ""
                        )
                    elif isinstance(oj, str):
                        outline_summary = oj
                direction = f"第{row.chapter_idx}章《{row.title or ''}》: {outline_summary}"
                pack.future_outlines.append(direction)

            # Also try to get summaries from previous volumes if we're at
            # the start of a volume
            if chapter_idx <= 3:
                vol_summary_result = await db.execute(
                    select(VolumeSummary.summary_text)
                    .join(Volume, VolumeSummary.volume_id == Volume.id)
                    .where(
                        Volume.project_id == pid,
                        Volume.id != vid,
                    )
                    .order_by(Volume.volume_idx.desc())
                    .limit(2)
                )
                vol_summaries = vol_summary_result.scalars().all()
                for vs in reversed(list(vol_summaries)):
                    pack.recent_summaries.insert(0, f"[前卷摘要] {vs}")

        except Exception as e:
            logger.warning("Failed to build proximity layer: %s", e)

    # ------------------------------------------------------------------
    # Layer 2: Facts
    # ------------------------------------------------------------------

    async def _build_facts(
        self,
        pack: ContextPack,
        project_id: str | UUID,
        chapter_idx: int,
    ) -> None:
        """Build Layer 2: world rules, character cards, foreshadows, timeline."""
        db = await self._get_db()
        pid = str(project_id)

        # World rules from PostgreSQL
        try:
            rules_result = await db.execute(
                select(WorldRule.category, WorldRule.rule_text)
                .where(WorldRule.project_id == pid)
                .order_by(WorldRule.category)
            )
            for row in rules_result.all():
                pack.world_rules.append(f"[{row.category}] {row.rule_text}")
        except Exception as e:
            logger.warning("Failed to load world rules: %s", e)

        # Character cards from PostgreSQL characters + Neo4j state
        try:
            char_result = await db.execute(
                select(Character)
                .where(Character.project_id == pid)
            )
            characters = char_result.scalars().all()

            for char in characters:
                profile = char.profile_json or {}
                card = CharacterCard(
                    name=char.name,
                    location=profile.get("location", ""),
                    power_level=profile.get("power_level", ""),
                    mental_state=profile.get("mental_state", ""),
                )

                # Parse relationships from profile
                rels = profile.get("relationships", {})
                if isinstance(rels, dict):
                    card.relationships = rels
                elif isinstance(rels, list):
                    for r in rels:
                        if isinstance(r, dict):
                            target = r.get("target", r.get("name", ""))
                            rel_type = r.get("type", r.get("relation", ""))
                            if target and rel_type:
                                card.relationships[target] = rel_type

                # Recent actions from profile
                actions = profile.get("recent_actions", [])
                if isinstance(actions, list):
                    card.recent_actions = actions[-5:]

                pack.character_cards.append(card)

            # Enrich from Neo4j if available
            await self._enrich_characters_from_neo4j(
                pack, pid, chapter_idx
            )
        except Exception as e:
            logger.warning("Failed to load character cards: %s", e)

        # Foreshadow triplets
        try:
            fs_result = await db.execute(
                select(Foreshadow)
                .where(
                    Foreshadow.project_id == pid,
                    Foreshadow.status.in_(("planted", "ripening", "ready")),
                )
                .order_by(Foreshadow.narrative_proximity.desc())
            )
            for fs in fs_result.scalars().all():
                conditions = fs.resolve_conditions_json or []
                blueprint = fs.resolution_blueprint_json or {}
                triplet = CFPGTriplet(
                    cause=f"第{fs.planted_chapter}章: {fs.description}",
                    foreshadow=fs.description,
                    payoff_goal=blueprint.get("goal", "") or (
                        conditions[0] if conditions else "待定"
                    ),
                    proximity=fs.narrative_proximity or 0.0,
                )
                pack.foreshadow_triplets.append(triplet)
        except Exception as e:
            logger.warning("Failed to load foreshadow triplets: %s", e)

        # Timeline anchors from chapter summaries with key events
        try:
            timeline_result = await db.execute(
                select(Chapter.chapter_idx, Chapter.summary)
                .join(Volume, Chapter.volume_id == Volume.id)
                .where(
                    Volume.project_id == pid,
                    Chapter.summary.isnot(None),
                    Chapter.summary != "",
                    Chapter.chapter_idx <= chapter_idx,
                )
                .order_by(Chapter.chapter_idx.asc())
            )
            for row in timeline_result.all():
                if row.summary and len(row.summary) > 10:
                    anchor = TimeAnchor(
                        chapter_idx=row.chapter_idx,
                        event=row.summary[:100],
                    )
                    pack.timeline_anchors.append(anchor)

            # Keep only the most important anchors to save tokens
            if len(pack.timeline_anchors) > 15:
                # Keep first 3, last 5, and evenly sample the rest
                first = pack.timeline_anchors[:3]
                last = pack.timeline_anchors[-5:]
                middle = pack.timeline_anchors[3:-5]
                step = max(1, len(middle) // 7)
                sampled = middle[::step][:7]
                pack.timeline_anchors = first + sampled + last
        except Exception as e:
            logger.warning("Failed to load timeline anchors: %s", e)

        # Build strand tracker from recent chapters
        await self._build_strand_tracker(pack, pid, chapter_idx)

    async def _enrich_characters_from_neo4j(
        self,
        pack: ContextPack,
        project_id: str,
        chapter_idx: int,
    ) -> None:
        """Enrich character cards with state from Neo4j knowledge graph."""
        try:
            from app.db.neo4j import get_neo4j

            driver = None
            async for d in get_neo4j():
                driver = d
                break

            if driver is None:
                return

            from app.services.entity_timeline import EntityTimelineService

            ets = EntityTimelineService(driver)
            snapshots = await ets.get_active_characters_at(project_id, chapter_idx)
            relationships = await ets.get_relationships_at(project_id, chapter_idx)

            # Build relationship lookup
            rel_lookup: dict[str, dict[str, str]] = {}
            for rel in relationships:
                rel_lookup.setdefault(rel.source, {})[rel.target] = rel.rel_type

            # Merge Neo4j state into existing cards
            card_by_name = {c.name: c for c in pack.character_cards}

            for snap in snapshots:
                status = snap.status or {}
                if snap.name in card_by_name:
                    card = card_by_name[snap.name]
                    # Update with Neo4j data if PostgreSQL data is empty
                    if not card.location:
                        card.location = status.get("location", status.get("位置", ""))
                    if not card.power_level:
                        card.power_level = status.get(
                            "power_level",
                            status.get("能力等级", status.get("实力", "")),
                        )
                    if not card.mental_state:
                        card.mental_state = status.get(
                            "mental_state",
                            status.get("情绪", status.get("状态", "")),
                        )
                    # Merge relationships
                    if snap.name in rel_lookup:
                        for target, rtype in rel_lookup[snap.name].items():
                            if target not in card.relationships:
                                card.relationships[target] = rtype
                else:
                    # Create new card from Neo4j data
                    card = CharacterCard(
                        name=snap.name,
                        location=status.get("location", status.get("位置", "")),
                        power_level=status.get(
                            "power_level",
                            status.get("能力等级", status.get("实力", "")),
                        ),
                        mental_state=status.get(
                            "mental_state",
                            status.get("情绪", status.get("状态", "")),
                        ),
                        relationships=rel_lookup.get(snap.name, {}),
                    )
                    pack.character_cards.append(card)

        except (RuntimeError, ImportError):
            logger.debug("Neo4j not available, skipping character enrichment")
        except Exception as e:
            logger.warning("Failed to enrich characters from Neo4j: %s", e)

    async def _build_strand_tracker(
        self,
        pack: ContextPack,
        project_id: str,
        chapter_idx: int,
    ) -> None:
        """Analyze recent chapters to track Quest/Fire/Constellation strands."""
        try:
            from app.services.strand_tracker import StrandTrackerService

            tracker_svc = StrandTrackerService(db=await self._get_db())
            tracker = await tracker_svc.analyze_strands(project_id, chapter_idx)
            pack.strand_tracker = tracker
        except (ImportError, Exception) as e:
            logger.debug("Strand tracker not available: %s", e)

    # ------------------------------------------------------------------
    # Layer 3: RAG
    # ------------------------------------------------------------------

    async def _build_rag(
        self,
        pack: ContextPack,
        project_id: str | UUID,
        chapter_idx: int,
    ) -> None:
        """Build Layer 3: RAG retrieval, dialogue samples, style samples."""
        pid = str(project_id)

        # Extract key entities from outline for CoKe-pattern retrieval
        entities = self._extract_entities_from_outline(pack.current_outline)

        # Search Qdrant for relevant snippets
        if entities:
            await self._search_qdrant_snippets(pack, entities)

        # Get dialogue samples from PostgreSQL characters
        try:
            db = await self._get_db()
            char_result = await db.execute(
                select(Character)
                .where(Character.project_id == pid)
            )
            for char in char_result.scalars().all():
                profile = char.profile_json or {}
                samples = profile.get("dialogue_samples", [])
                if isinstance(samples, list) and samples:
                    pack.dialogue_samples[char.name] = samples[:3]
        except Exception as e:
            logger.warning("Failed to load dialogue samples: %s", e)

        # Style samples from Qdrant
        await self._load_style_samples(pack, pid)

    def _extract_entities_from_outline(self, outline: dict) -> list[str]:
        """Extract key entity names from the chapter outline for Qdrant search.

        Implements CoKe (Context-based Keyword Extraction) pattern:
        extracts character names, locations, items, and key concepts.
        """
        entities: list[str] = []
        if not outline:
            return entities

        # Extract from known outline fields
        text_fields = ["summary", "main_plot", "key_points", "events", "description"]
        combined_text = ""
        for field_name in text_fields:
            val = outline.get(field_name, "")
            if isinstance(val, str):
                combined_text += " " + val
            elif isinstance(val, list):
                combined_text += " " + " ".join(str(v) for v in val)

        # Extract characters from outline
        chars = outline.get("characters", [])
        if isinstance(chars, list):
            for c in chars:
                if isinstance(c, str):
                    entities.append(c)
                elif isinstance(c, dict):
                    name = c.get("name", "")
                    if name:
                        entities.append(name)

        # Extract location mentions
        locations = outline.get("locations", outline.get("setting", []))
        if isinstance(locations, str):
            entities.append(locations)
        elif isinstance(locations, list):
            entities.extend(str(loc) for loc in locations)

        # Extract items
        items = outline.get("items", outline.get("props", []))
        if isinstance(items, list):
            entities.extend(str(item) for item in items)

        # Extract key nouns from combined text (simple CJK extraction)
        if combined_text:
            # Extract quoted terms and proper nouns (Chinese patterns)
            quoted = re.findall(r'["""](.*?)["""]', combined_text)
            entities.extend(quoted)

            # Extract terms in book-title marks
            book_marks = re.findall(r'[《](.*?)[》]', combined_text)
            entities.extend(book_marks)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for e in entities:
            e = e.strip()
            if e and e not in seen:
                seen.add(e)
                unique.append(e)

        return unique[:20]  # Limit to avoid excessive searches

    async def _search_qdrant_snippets(
        self,
        pack: ContextPack,
        entities: list[str],
    ) -> None:
        """Search Qdrant for relevant content based on extracted entities."""
        try:
            from app.services.feature_extractor import generate_embedding

            # Create a combined query from entities
            query_text = " ".join(entities[:10])
            embedding = await generate_embedding(query_text)
            if not embedding:
                return

            from qdrant_client import AsyncQdrantClient
            from app.config import settings

            client = AsyncQdrantClient(
                host=getattr(settings, "QDRANT_HOST", "localhost"),
                port=getattr(settings, "QDRANT_PORT", 6333),
            )

            try:
                results = await client.search(
                    collection_name="chapter_summaries",
                    query_vector=embedding,
                    limit=5,
                    score_threshold=0.4,
                )
                for hit in results:
                    payload = hit.payload or {}
                    summary = payload.get("summary", payload.get("text", ""))
                    if summary:
                        pack.rag_snippets.append(summary)
                # v0.6 — ContextPack v2: three-way recall from decompile collections
                # Gated on CONTEXT_PACK_V2_ENABLED env flag. Safe no-op if disabled
                # or if the project has no bound reference book.
                import os

                if os.getenv("CONTEXT_PACK_V2_ENABLED", "false").lower() in ("1", "true", "yes"):
                    try:
                        await self._v2_three_way_recall(pack, embedding, client)
                    except Exception as v2_exc:
                        logger.debug("v2 three-way recall skipped: %s", v2_exc)
            except Exception:
                logger.debug("Qdrant search failed, collection may not exist")
            finally:
                await client.close()

        except (ImportError, Exception) as e:
            logger.debug("Qdrant RAG retrieval skipped: %s", e)

    async def _v2_three_way_recall(self, pack: ContextPack, embedding: list, client) -> None:
        """v0.6 ContextPack v2: pull style_profiles + beat_sheets + redacted samples
        from the reference book bound to this project."""
        from app.services.qdrant_store import QdrantStore

        # Resolve bound reference book via project settings
        project_id = getattr(pack, "project_id", None) or getattr(pack.meta, "project_id", None)
        ref_book_id: str | None = None
        if project_id:
            try:
                db = await self._get_db()
                project = await db.get(Project, project_id)
                if project:
                    ref = (project.settings_json or {}).get("style_reference", {})
                    ref_book_id = ref.get("reference_book_id") or ref.get("book_id")
            except Exception:
                ref_book_id = None

        store = QdrantStore(client)
        # style_profiles: structured style prompts (top 3)
        style_hits = await store.search_style_profiles(embedding, book_id=ref_book_id, top_k=3)
        for h in style_hits:
            prof = (h.get("payload") or {}).get("profile") or {}
            if prof:
                line = (
                    f"[风格] pov={prof.get('pov','?')} 节奏={prof.get('sentence_rhythm','?')} "
                    f"情感={prof.get('emotional_register','?')} "
                    f"词汇={','.join(prof.get('vocab_tone') or [])}"
                )
                pack.rag_snippets.append(line)
        # beat_sheets: entity-redacted plot scaffolds (top 2)
        beat_hits = await store.search_beat_sheets(embedding, book_id=ref_book_id, top_k=2)
        for h in beat_hits:
            beat = (h.get("payload") or {}).get("beat") or {}
            if beat:
                line = (
                    f"[骨架] {beat.get('scene_type','?')}: {beat.get('reusable_pattern','?')} "
                    f"→ {beat.get('outcome','?')}"
                )
                pack.rag_snippets.append(line)
        # style_samples_redacted: one redacted sample passage for few-shot
        sample_hits = await store.search_style_samples_redacted(
            embedding, book_id=ref_book_id, top_k=1
        )
        for h in sample_hits:
            redacted = (h.get("payload") or {}).get("redacted_text")
            if redacted:
                pack.style_samples.append(redacted)

    async def _load_style_samples(
        self,
        pack: ContextPack,
        project_id: str,
    ) -> None:
        """Load style few-shot samples from project or reference books."""
        try:
            db = await self._get_db()
            project = await db.get(Project, project_id)
            if not project:
                return

            settings_json = project.settings_json or {}
            style_ref = settings_json.get("style_reference", {})

            # Check for style profile
            style_profile_id = style_ref.get("profile_id")
            if style_profile_id:
                from app.models.project import StyleProfile

                profile = await db.get(StyleProfile, style_profile_id)
                if profile and profile.config_json:
                    config = profile.config_json
                    sample_ids = config.get("sample_block_ids", [])
                    if sample_ids:
                        from app.services.qdrant_store import QdrantStore
                        from qdrant_client import AsyncQdrantClient
                        from app.config import settings as app_settings

                        client = AsyncQdrantClient(
                            host=getattr(app_settings, "QDRANT_HOST", "localhost"),
                            port=getattr(app_settings, "QDRANT_PORT", 6333),
                        )
                        store = QdrantStore(client)
                        try:
                            texts = await store.get_sample_texts_for_style(
                                sample_ids[:3]
                            )
                            pack.style_samples.extend(texts)
                        finally:
                            await client.close()

        except Exception as e:
            logger.debug("Style sample loading skipped: %s", e)


# ---------------------------------------------------------------------------
# v0.8 — ContextPack v3 fourth recall path: writing_rules
# ---------------------------------------------------------------------------


async def fetch_writing_rules(
    db,
    project_id: str,
    *,
    top_k: int = 6,
) -> list[str]:
    """Return the top-K active writing_rules for a project.

    Resolution path:
    1. Look up ``project.genre_profile_code`` → ``genre_profiles`` row.
    2. If the profile has ``default_writing_rule_ids``, use those IDs.
    3. Otherwise fall back to active rules whose ``genre`` matches the code
       (or rules with an empty/global ``genre``).
    4. Sort by ``priority`` desc, return at most ``top_k`` rendered strings.

    Returns an empty list on any error so callers can degrade silently.
    """
    try:
        from sqlalchemy import select

        from app.models.project import Project
        from app.models.writing_engine import GenreProfile, WritingRule

        proj = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalars().first()
        if proj is None:
            return []
        code = getattr(proj, "genre_profile_code", None) or ""

        profile = None
        if code:
            profile = (
                await db.execute(select(GenreProfile).where(GenreProfile.code == code))
            ).scalars().first()

        rules: list[WritingRule] = []
        if profile and profile.default_writing_rule_ids:
            ids = [str(x) for x in (profile.default_writing_rule_ids or [])]
            if ids:
                q = (
                    await db.execute(
                        select(WritingRule).where(
                            WritingRule.id.in_(ids),
                            WritingRule.is_active.is_(True),
                        )
                    )
                )
                rules = list(q.scalars().all())

        if not rules:
            stmt = select(WritingRule).where(WritingRule.is_active.is_(True))
            if code:
                stmt = stmt.where((WritingRule.genre == code) | (WritingRule.genre == ""))
            else:
                stmt = stmt.where(WritingRule.genre == "")
            rules = list((await db.execute(stmt)).scalars().all())

        rules.sort(key=lambda r: (-(r.priority or 0), r.title or ""))
        return [f"{r.title}：{r.rule_text}".strip() for r in rules[:top_k]]
    except Exception as exc:
        logger.debug("fetch_writing_rules skipped: %s", exc)
        return []
