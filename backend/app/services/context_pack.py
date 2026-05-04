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
    CharacterLocation,
    Character,
    Foreshadow,
    Location,
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
    # v1.7.4 P0-1: book/volume outline injection (was previously missing)
    book_outline_excerpt: str = ""
    volume_outline: dict = field(default_factory=dict)

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

    def _render_chapter_outline_block(self, co: dict) -> str:
        """PR-OUTLINE-DEEPDIVE Phase 4: render chapter outline_json into a
        中文分段 prompt block instead of raw JSON dump.

        Schema (after PR-OUTLINE-DEEPDIVE Phase 1):
          chapter_idx, title, summary, key_events,
          prev_chapter_threads, state_changes, foreshadows_planted,
          foreshadows_resolved, next_chapter_hook

        能向后兼容旧 4 字段格式，缺失字段不输出该部分。
        """
        if not isinstance(co, dict) or not co:
            return ""
        parts: list[str] = []
        title = co.get("title") or ""
        cidx = co.get("chapter_idx")
        if title or cidx:
            parts.append(f"《第{cidx}章 {title}》".strip())
        if co.get("summary"):
            parts.append(f"棗概：{co['summary']}")
        ke = co.get("key_events") or []
        if isinstance(ke, list) and ke:
            parts.append("关键事件：")
            for i, e in enumerate(ke, 1):
                parts.append(f"  {i}. {e}")
        pct = co.get("prev_chapter_threads") or []
        if isinstance(pct, list) and pct:
            parts.append("本章需接住的上章余波：")
            for t in pct:
                parts.append(f"  - {t}")
        sc = co.get("state_changes") if isinstance(co.get("state_changes"), dict) else {}
        if sc:
            chs = sc.get("characters") or []
            if isinstance(chs, list) and chs:
                parts.append("本章末尾人物状态变化：")
                for c in chs:
                    if isinstance(c, dict):
                        parts.append(f"  - {c.get('name','')}：{c.get('change','')}")
            it = sc.get("items") or []
            if isinstance(it, list) and it:
                parts.append("本章末尾道具/物件状态变化：")
                for x in it:
                    if isinstance(x, dict):
                        parts.append(f"  - {x.get('name','')}：{x.get('change','')}")
            rels = sc.get("relationships") or []
            if isinstance(rels, list) and rels:
                parts.append("本章末尾关系变化：")
                for r in rels:
                    if isinstance(r, dict):
                        parts.append(
                            f"  - {r.get('from','')} → {r.get('to','')}：{r.get('change','')}"
                        )
        fp = co.get("foreshadows_planted") or []
        if isinstance(fp, list) and fp:
            parts.append("本章需埋下的伏笔（必须体现）：")
            for f in fp:
                if isinstance(f, dict):
                    parts.append(
                        f"  - {f.get('description','')} 【兑现条件：{f.get('resolve_conditions','')}】"
                    )
                elif isinstance(f, str):
                    parts.append(f"  - {f}")
        fr = co.get("foreshadows_resolved") or []
        if isinstance(fr, list) and fr:
            parts.append("本章可兑现之前伏笔：")
            for f in fr:
                parts.append(f"  - {f}")
        nch = co.get("next_chapter_hook")
        if nch:
            parts.append(f"本章末尾交接下章（必须交付）：{nch}")
        if not parts:
            # 降级：没有能识别的字段，还有 dict 内容 → fallback dump
            return json.dumps(co, ensure_ascii=False, indent=2)
        return "\n".join(parts)

    def _render_volume_outline_block(self) -> str:
        """Render volume_outline dict into a readable block for the prompt.

        Volume outline schema (from outline_generator):
          title, volume_idx, core_conflict, emotional_arc,
          new_characters[{name,role,identity}], turning_points[str],
          foreshadows{planted:[{description,resolve_conditions}], resolved:[str]},
          chapter_summaries[{title,summary,key_events,chapter_idx}],
          transition_to_next, departing_characters
        """
        vo = self.volume_outline or {}
        if not vo:
            return ""
        parts: list[str] = []
        title = vo.get("title") or ""
        vidx = vo.get("volume_idx")
        if title or vidx:
            parts.append(f"《第{vidx}卷 {title}》".strip())
        if vo.get("core_conflict"):
            parts.append(f"核心冲突：{vo['core_conflict']}")
        if vo.get("emotional_arc"):
            parts.append(f"情感弧线：{vo['emotional_arc']}")
        new_chars = vo.get("new_characters") or []
        if isinstance(new_chars, list) and new_chars:
            cs = []
            for c in new_chars[:8]:
                if isinstance(c, dict):
                    nm = c.get("name", "")
                    idn = c.get("identity", "")
                    rl = c.get("role", "")
                    line = f"- {nm}（{idn}）：{rl}" if (idn or rl) else f"- {nm}"
                    cs.append(line)
            if cs:
                parts.append("新登场角色：\n" + "\n".join(cs))
        tps = vo.get("turning_points") or []
        if isinstance(tps, list) and tps:
            parts.append("转折点：\n" + "\n".join(f"- {t}" for t in tps[:6]))
        fs = vo.get("foreshadows") or {}
        if isinstance(fs, dict):
            planted = fs.get("planted") or []
            if isinstance(planted, list) and planted:
                fs_lines = []
                for f in planted[:8]:
                    if isinstance(f, dict):
                        desc = f.get("description", "")
                        conds = f.get("resolve_conditions") or []
                        cond_text = ("→ " + "；".join(conds[:2])) if isinstance(conds, list) and conds else ""
                        fs_lines.append(f"- {desc} {cond_text}".rstrip())
                if fs_lines:
                    parts.append("已埋伏笔：\n" + "\n".join(fs_lines))
        if vo.get("transition_to_next"):
            parts.append(f"卷末过渡：{vo['transition_to_next']}")
        return "\n\n".join(parts)

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

        # v1.7.4 P0-1: inject book/volume outline at the TOP of L1 so the
        # generator sees the global picture, not just the current-chapter beat.
        if self.book_outline_excerpt:
            l1_parts.append(f"【全书大纲(节选)】\n{self.book_outline_excerpt}")

        if self.volume_outline:
            vo_text = self._render_volume_outline_block()
            if vo_text:
                l1_parts.append(f"【本卷大纲】\n{vo_text}")

        if self.recent_summaries:
            summaries_text = "\n".join(
                f"第{i}章前: {s}" for i, s in enumerate(self.recent_summaries, 1)
            )
            l1_parts.append(f"【近五章摘要】\n{summaries_text}")

        if self.current_content:
            l1_parts.append(f"【本章已有内容】\n{self.current_content}")

        if self.current_outline:
            outline_str = self._render_chapter_outline_block(self.current_outline)
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

        # v0.9: check ctxpack invalidation flag. When settings (characters /
        # world_rules / relationships) change, ``services.change_log`` writes
        # a Redis flag ``ctxpack:invalid:{project_id}`` so any cached or
        # memoised context pack is bypassed. Logged for observability; the
        # flag is cleared after a successful rebuild below.
        cache_was_invalidated = False
        try:
            from app.services import ctxpack_cache

            cache_was_invalidated = await ctxpack_cache.is_invalid(project_id)
            if cache_was_invalidated:
                logger.info(
                    "ContextPack rebuild forced by invalidation flag (project_id=%s)",
                    project_id,
                )
        except Exception as exc:  # never block generation on cache check
            logger.debug("ctxpack invalidation check failed: %s", exc)

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

        # PR-AI1: inject naming/glossary directive so chapter generation
        # is told upfront what is and is not allowed when coining items
        # / techniques / titles. Cheap, fail-safe, no DB hits.
        try:
            from app.services.checkers.anti_ai_checker import NAMING_DIRECTIVE
            pack.writing_guidance.append(NAMING_DIRECTIVE)
        except Exception as _ai1_err:
            logger.debug("PR-AI1 naming directive injection skipped: %s", _ai1_err)

        # PR-STY1: style v9 节奏/留白/信息密度 directives.
        try:
            from app.services.checkers.anti_ai_checker import STYLE_V9_DIRECTIVES
            pack.writing_guidance.extend(STYLE_V9_DIRECTIVES)
        except Exception as _sty1_err:
            logger.debug("PR-STY1 style v9 directive injection skipped: %s", _sty1_err)

        # v0.9: clear the invalidation flag after a successful rebuild so
        # subsequent builds can hit any downstream cache again.
        if cache_was_invalidated:
            try:
                from app.services import ctxpack_cache

                await ctxpack_cache.clear(project_id)
            except Exception as exc:
                logger.debug("ctxpack invalidation clear failed: %s", exc)

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

            # v1.7.4 P0-1: load book + volume outline so chapter generation
            # sees the global picture (was missing in v1.7.3 and earlier).
            try:
                book_outline_q = await db.execute(
                    select(Outline.content_json)
                    .where(Outline.project_id == pid, Outline.level == "book")
                    .order_by(Outline.version.desc())
                    .limit(1)
                )
                bo = book_outline_q.scalar_one_or_none()
                if isinstance(bo, dict):
                    raw = bo.get("raw_text") or bo.get("summary") or ""
                    if isinstance(raw, str) and raw.strip():
                        # keep head 1500 + tail 500 chars to give both setup and
                        # endgame anchors without blowing budget.
                        if len(raw) > 2200:
                            pack.book_outline_excerpt = (
                                raw[:1500].rstrip()
                                + "\n\n…(中部省略)…\n\n"
                                + raw[-500:].lstrip()
                            )
                        else:
                            pack.book_outline_excerpt = raw
            except Exception as e:
                logger.warning("Failed to load book outline: %s", e)

            try:
                vol_outline_q = await db.execute(
                    select(Outline.content_json)
                    .where(
                        Outline.project_id == pid,
                        Outline.level == "volume",
                    )
                    .order_by(Outline.version.desc())
                )
                for vo_row in vol_outline_q.scalars().all():
                    if isinstance(vo_row, dict) and (
                        vo_row.get("volume_idx") is None
                        or str(vo_row.get("volume_id", vid)) == vid
                    ):
                        # match by volume_id when present, otherwise take first
                        pack.volume_outline = vo_row
                        break
                if not pack.volume_outline:
                    # fallback: take the most recent volume outline
                    fallback = await db.execute(
                        select(Outline.content_json)
                        .where(
                            Outline.project_id == pid,
                            Outline.level == "volume",
                        )
                        .order_by(Outline.version.desc())
                        .limit(1)
                    )
                    fb = fallback.scalar_one_or_none()
                    if isinstance(fb, dict):
                        pack.volume_outline = fb
            except Exception as e:
                logger.warning("Failed to load volume outline: %s", e)

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
            # v1.9+: Prefer Postgres projection (character_locations) for character current location.
            # This keeps ContextPack stable even when Neo4j is temporarily unavailable.
            loc_by_char_id: dict[UUID, str] = {}
            try:
                loc_rows = await db.execute(
                    select(
                        CharacterLocation.character_id,
                        Location.name,
                    )
                    .join(Location, Location.id == CharacterLocation.location_id)
                    .where(
                        CharacterLocation.project_id == pid,
                        CharacterLocation.chapter_start <= chapter_idx,
                    )
                    .order_by(
                        CharacterLocation.character_id.asc(),
                        CharacterLocation.chapter_start.desc(),
                    )
                )
                for character_id, loc_name in loc_rows.all():
                    if character_id not in loc_by_char_id:
                        loc_by_char_id[character_id] = loc_name
            except Exception as e:
                logger.debug("Failed to load character_locations projection: %s", e)

            char_result = await db.execute(
                select(Character)
                .where(Character.project_id == pid)
            )
            characters = char_result.scalars().all()

            for char in characters:
                profile = char.profile_json or {}
                card = CharacterCard(
                    name=char.name,
                    location=(loc_by_char_id.get(char.id) or profile.get("location", "")),
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
            # v1.4 — optional LLM query rewrite before embedding (gated, safe no-op).
            query_text = await self._maybe_rewrite_query(query_text)
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

    async def _maybe_rewrite_query(self, query_text: str) -> str:
        """v1.4 — optional LLM rewrite of the Qdrant query (task_type=rag_query_rewrite).

        Gated on ``RAG_QUERY_REWRITE_ENABLED`` (default off). On any failure the
        original ``query_text`` is returned unchanged. Accepts either a plain
        string or a ``{"query": "..."}`` JSON object as the LLM output.
        """
        import os

        raw = os.getenv("RAG_QUERY_REWRITE_ENABLED", "0").strip().lower()
        if raw not in ("1", "true", "yes", "on"):
            return query_text
        if not query_text.strip():
            return query_text
        try:
            from app.services.prompt_registry import run_structured_prompt

            db = await self._get_db()
            out = await run_structured_prompt(
                "rag_query_rewrite",
                f"<query>\n{query_text}\n</query>\n\n请返回用于向量检索的改写查询字符串。",
                db,
            )
        except Exception as exc:
            logger.debug("rag_query_rewrite skipped: %s", exc)
            return query_text
        if isinstance(out, dict):
            rewrote = out.get("query") or out.get("rewrite") or out.get("text")
            if isinstance(rewrote, str) and rewrote.strip():
                return rewrote.strip()
        if isinstance(out, str) and out.strip():
            return out.strip()
        return query_text

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
        """Load style samples from a StyleProfile or StyleProfileCard fallback.

        v1.7.4 P1-alpha — fixes three bugs simultaneously:
          1. settings_json key mismatch: support both `style_reference.profile_id`
             and the legacy `default_style_profile_id`.
          2. Removes broken `profile.config_json` access (this column does not
             exist on StyleProfile; real columns are rules_json / anti_ai_rules /
             tone_keywords / sample_passages).
          3. Adds a StyleProfileCard fallback: when no aggregated profile is
             bound, sample top-K cards from the project's reference book and
             aggregate the 9-dim profile_json into a consensus style sheet.

        Resolution order:
          A) StyleProfile via settings.style_reference.profile_id
             OR settings.default_style_profile_id
          B) StyleProfileCard sampling via settings.style_reference.reference_book_id
             OR settings.reference_book_id  (or .style_reference.book_id)
        """
        try:
            db = await self._get_db()
            project = await db.get(Project, project_id)
            if not project:
                return

            settings_json = project.settings_json or {}
            style_ref = settings_json.get("style_reference", {}) or {}

            # ---- Path A: aggregated StyleProfile ----
            style_profile_id = (
                style_ref.get("profile_id")
                or settings_json.get("default_style_profile_id")
            )
            if style_profile_id:
                from app.models.project import StyleProfile
                try:
                    profile = await db.get(StyleProfile, style_profile_id)
                except Exception:
                    profile = None
                if profile is not None:
                    rendered = self._render_style_profile(profile)
                    if rendered:
                        pack.style_samples.extend(rendered)
                        return  # Path A succeeded -> skip fallback

            # ---- Path B: StyleProfileCard fallback (raw cards from reference book) ----
            ref_book_id = (
                style_ref.get("reference_book_id")
                or style_ref.get("book_id")
                or settings_json.get("reference_book_id")
            )
            if ref_book_id:
                rendered = await self._aggregate_style_cards(db, str(ref_book_id), top_k=12)
                if rendered:
                    pack.style_samples.extend(rendered)

        except Exception as e:
            logger.debug("Style sample loading skipped: %s", e)

    def _render_style_profile(self, profile) -> list[str]:
        """Render a StyleProfile ORM row into Layer-3 style_samples text blocks.

        Reads the real columns: rules_json, anti_ai_rules, tone_keywords, sample_passages.
        Returns 0..N text blocks (each block joins via '---' in the final prompt).
        """
        parts: list[str] = []
        book_label = (
            getattr(profile, "source_book", None)
            or getattr(profile, "name", None)
            or "未命名风格"
        )

        rules = getattr(profile, "rules_json", None) or []
        rule_lines: list[str] = []
        for r in rules[:10]:
            if isinstance(r, dict):
                txt = r.get("rule") or r.get("text")
                if txt:
                    rule_lines.append(f"- {txt}")
            elif isinstance(r, str) and r.strip():
                rule_lines.append(f"- {r}")
        if rule_lines:
            parts.append("【风格规则 — " + str(book_label) + "】\n" + "\n".join(rule_lines))

        anti = getattr(profile, "anti_ai_rules", None) or []
        anti_lines: list[str] = []
        for a in anti[:10]:
            if isinstance(a, dict):
                pat = a.get("pattern") or a.get("rule")
                if pat:
                    anti_lines.append(f"- 禁用: {pat}")
            elif isinstance(a, str) and a.strip():
                anti_lines.append(f"- 禁用: {a}")
        if anti_lines:
            parts.append("【反 AI 硬约束】\n" + "\n".join(anti_lines))

        tone = getattr(profile, "tone_keywords", None) or []
        if tone:
            tone_str = " / ".join(str(t) for t in tone[:12] if t)
            if tone_str:
                parts.append("【语气词汇】" + tone_str)

        samples = getattr(profile, "sample_passages", None) or []
        sample_texts: list[str] = []
        for s in samples[:3]:
            if isinstance(s, dict):
                txt = s.get("text") or s.get("passage")
                if txt:
                    sample_texts.append(str(txt)[:300])
            elif isinstance(s, str) and s.strip():
                sample_texts.append(s[:300])
        if sample_texts:
            parts.append("【示例段落】\n" + "\n---\n".join(sample_texts))

        # v8: 渲染 config_json 里的剂量画像（dosage_profile）
        config = getattr(profile, "config_json", None) or {}
        if isinstance(config, dict) and isinstance(config.get("dosage_profile"), dict):
            d = config["dosage_profile"]
            try:
                dlg = d.get("dialogue", {}) or {}
                met = d.get("metaphor", {}) or {}
                psy = d.get("psychology", {}) or {}
                snt = d.get("sentence", {}) or {}
                par_ = d.get("paragraph", {}) or {}
                col = d.get("colloquial", {}) or {}
                dr = float(dlg.get("ratio", 0) or 0)
                mt = float(met.get("total_per_kchar", 0) or 0)
                ms = float(met.get("sentence_end_per_kchar", 0) or 0)
                py = float(psy.get("pattern_total_per_kchar", 0) or 0)
                pyc = float(psy.get("pattern_per_chapter_7k", 0) or 0)
                pyn = float(psy.get("neutral_words_per_kchar", 0) or 0)
                slm = float(snt.get("mean_chars", 0) or 0)
                plm = float(par_.get("mean_chars", 0) or 0)
                cl = float(col.get("particles_per_kchar", 0) or 0)
                src_name = d.get("source", "参考书")
                dosage_lines = [
                    "【剂量画像 — 仿写参考密度（按一章 7000 字换算）】",
                    f"· 对话占比 ≈ {dr*100:.0f}%，对话轮均长约 27 字（自然为主，不为凑量而造对话）。",
                    f"· 比喻总量 ≈ {mt:.1f}/千字（一章约 {mt*7:.0f} 次），其中句尾比喻 ≈ {ms:.1f}/千字（约 {ms*7:.0f} 次）。句尾比喻是江南特色，多用但每个都要独特、不重复。",
                    f"· 心理戏套语（心里一沉/眼皮一跳/喉咙发紧/头皮发麻/握紧拳等 13 类）总量 ≈ {py:.3f}/千字，一章约 {pyc:.1f} 次。硬上限：每章不得超过 2 次，同一套语不得重复。",
                    f"· 心理中性词（想 / 觉得 / 感到 / 猛然 / 突然 / 仿佛）≈ {pyn:.1f}/千字（一章约 {pyn*7:.0f} 次）。这是「正常心理描写」，不是黑名单。",
                    f"· 句长均 {slm:.0f} 字 / 段长均 {plm:.0f} 字。短长结合，不打碎句、不堆长句。",
                    f"· 口语助词（呀 / 哦 / 哈 / 嘴 / 老子 / 个屁）≈ {cl:.2f}/千字（一章约 {cl*7:.0f} 次）。吝槽口吻误论年轻人主语，能用口语骂人就别用「面色凝重」。",
                    "· prompt 自指语严禁出现：「以下是 / 根据您 / 以上便是 / prompt / 黑名单 / 护城词 / 伏笔 / 钩子」等。",
                    f"提示：以上重点为《{src_name}》原作采样基线，仿写不必精确达标，但严禁批量超标——尤其是心理戏套语和句尾比喻不得重复。",
                ]
                parts.append("\n".join(dosage_lines))
            except Exception as e:
                logger.warning("render dosage_profile failed: %s", e)

        return parts

    async def _aggregate_style_cards(
        self,
        db: AsyncSession,
        book_id: str,
        top_k: int = 12,
    ) -> list[str]:
        """Aggregate top-K StyleProfileCard.profile_json entries from a reference book
        into Layer-3 style_samples text blocks.

        9 profile_json dims: pov, tense, sentence_rhythm, dialogue_style, sensory_mix,
        pacing, emotional_register, vocab_tone, forbidden_tells, signature_moves.

        Aggregation strategy: vote pov/tense; pick richest (longest) text for
        rhythm/dialogue/pacing/emotion; average sensory_mix; union vocab/forbidden/signature.
        """
        try:
            from app.models.decompile import StyleProfileCard

            stmt = (
                select(StyleProfileCard)
                .where(StyleProfileCard.book_id == str(book_id))
                .order_by(StyleProfileCard.created_at.asc())
                .limit(top_k)
            )
            result = await db.execute(stmt)
            cards = list(result.scalars().all())
            if not cards:
                return []

            povs: list[str] = []
            tenses: list[str] = []
            rhythms: list[str] = []
            dialogues: list[str] = []
            sensory_sums: dict[str, float] = {}
            sensory_n = 0
            pacings: list[str] = []
            emotions: list[str] = []
            vocab_set: list[str] = []
            forbidden_set: list[str] = []
            signature_set: list[str] = []

            def _add_unique(lst: list[str], item: str) -> None:
                if item and item not in lst:
                    lst.append(item)

            for c in cards:
                pj = c.profile_json or {}
                if not isinstance(pj, dict):
                    continue
                if pj.get("pov"):
                    povs.append(str(pj["pov"]))
                if pj.get("tense"):
                    tenses.append(str(pj["tense"]))
                if pj.get("sentence_rhythm"):
                    rhythms.append(str(pj["sentence_rhythm"]))
                if pj.get("dialogue_style"):
                    dialogues.append(str(pj["dialogue_style"]))
                sm = pj.get("sensory_mix") or {}
                if isinstance(sm, dict) and sm:
                    for k, v in sm.items():
                        try:
                            sensory_sums[k] = sensory_sums.get(k, 0.0) + float(v)
                        except (TypeError, ValueError):
                            continue
                    sensory_n += 1
                if pj.get("pacing"):
                    pacings.append(str(pj["pacing"]))
                if pj.get("emotional_register"):
                    emotions.append(str(pj["emotional_register"]))
                for k in (pj.get("vocab_tone") or []):
                    if isinstance(k, str):
                        _add_unique(vocab_set, k)
                for k in (pj.get("forbidden_tells") or []):
                    if isinstance(k, str):
                        _add_unique(forbidden_set, k)
                for k in (pj.get("signature_moves") or []):
                    if isinstance(k, str):
                        _add_unique(signature_set, k)

            def _pick_longest(lst: list[str]) -> str:
                return max(lst, key=len) if lst else ""

            def _vote(lst: list[str]) -> str:
                return max(set(lst), key=lst.count) if lst else ""

            pov_str = _vote(povs)
            tense_str = _vote(tenses)
            rhythm_str = _pick_longest(rhythms)
            dialogue_str = _pick_longest(dialogues)
            pacing_str = _pick_longest(pacings)
            emotion_str = _pick_longest(emotions)

            sensory_str = ""
            if sensory_n > 0 and sensory_sums:
                avg = {k: v / sensory_n for k, v in sensory_sums.items()}
                top_sens = sorted(avg.items(), key=lambda x: -x[1])[:5]
                sensory_str = " / ".join(
                    f"{k} {round(v * 100)}%" for k, v in top_sens if v > 0.001
                )

            parts: list[str] = []

            block1_lines: list[str] = []
            pov_tense = " ".join(s for s in [pov_str, tense_str] if s).strip()
            if pov_tense:
                block1_lines.append(f"- 视角/时态: {pov_tense}")
            if rhythm_str:
                block1_lines.append(f"- 句式节奏: {rhythm_str}")
            if dialogue_str:
                block1_lines.append(f"- 对话风格: {dialogue_str}")
            if sensory_str:
                block1_lines.append(f"- 感官分布: {sensory_str}")
            if pacing_str:
                block1_lines.append(f"- 节奏: {pacing_str}")
            if emotion_str:
                block1_lines.append(f"- 情绪基调: {emotion_str}")
            if vocab_set:
                block1_lines.append("- 词汇调性: " + " / ".join(vocab_set[:10]))
            if block1_lines:
                parts.append("【参考风格档案 (基于参考书切片聚合)】\n" + "\n".join(block1_lines))

            block2_lines: list[str] = []
            for t in forbidden_set[:8]:
                block2_lines.append(f"- 禁忌: {t}")
            for s in signature_set[:6]:
                block2_lines.append(f"- 招牌: {s}")
            if block2_lines:
                parts.append("【风格禁忌与招牌】\n" + "\n".join(block2_lines))

            return parts
        except Exception as e:
            logger.debug("Style cards aggregation failed: %s", e)
            return []


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
