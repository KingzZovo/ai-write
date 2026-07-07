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
from app.services.narrative_contract import WORLD_LOGIC_CONTRACT
from app.services.fact_contract import build_fact_contract

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
    authoritative_fact_contract: str = ""
    world_rules: list[str] = field(default_factory=list)
    character_cards: list[CharacterCard] = field(default_factory=list)
    foreshadow_triplets: list[CFPGTriplet] = field(default_factory=list)
    # Q4 v1.9.2: foreshadow debt alert (QMAI-adapted). Pre-rendered by
    # foreshadow_manager.render_debt_warning; "" when health score >= 60.
    foreshadow_debt_warning: str = ""
    timeline_anchors: list[TimeAnchor] = field(default_factory=list)
    contradiction_cache: list[str] = field(default_factory=list)
    strand_tracker: StrandTracker = field(default_factory=StrandTracker)
    # v0.8 ContextPack v3: 4th recall path, scoped to project.genre_profile.
    writing_rules: list[str] = field(default_factory=list)
    # Q3 v1.9.1: serialized character cognition ledger (who knows what /
    # reader-only facts). Pre-rendered by character_cognition.serialize_for_prompt.
    cognition_boundaries: str = ""
    # C2/F1 v1.9.2: book-level style-tic mirror (dampen your own high-frequency
    # phrasings). Pre-rendered by style_stat.render_style_mirror_block.
    style_tic_mirror: str = ""
    # C3/F4 v1.9.2: deterministic related-chapter recall + secondary-cast roster.
    # Pre-rendered by related_chapters/character_roster render functions.
    related_chapter_recall: str = ""
    # C4/F3 v1.9.2: narrative compass direction anchor (ending + active threads
    # + scale range). Pre-rendered by compass_service.render_compass_anchor.
    compass_anchor: str = ""

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
            parts.append(f"梗概：{co['summary']}")
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

        contract_labels = {
            "start_state": "开场状态",
            "end_state": "收束状态",
            "time_delta": "时间消耗",
            "location_path": "空间路径",
            "entity_transfers": "实体转移",
            "information_state": "信息状态",
            "power_resource_map": "权力/资源图",
            "mechanism_limits": "机制边界",
            "result_strength": "结果强度",
            "action_budget": "动作预算",
            "inference_ledger": "推理台账",
            "handoff_to_next": "交接下章",
            "transition_bridge": "过桥约束",
        }
        contract_lines: list[str] = []
        for key, label in contract_labels.items():
            val = co.get(key)
            if isinstance(val, str) and val.strip():
                contract_lines.append(f"- {label}：{val.strip()}")
        if contract_lines:
            parts.append("本章世界逻辑合同：\n" + "\n".join(contract_lines))

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

        volume_contract_labels = {
            "volume_start_state": "卷初状态",
            "volume_end_state": "卷末状态",
            "volume_power_resource_map": "本卷权力/资源图",
            "volume_information_map": "本卷信息图",
            "volume_mechanism_limits": "本卷机制边界",
            "volume_result_strength_ladder": "本卷结果强度阶梯",
            "foreshadow_progression": "伏笔推进",
        }
        vc_lines: list[str] = []
        for key, label in volume_contract_labels.items():
            val = vo.get(key)
            if isinstance(val, str) and val.strip():
                vc_lines.append(f"- {label}：{val.strip()}")
            elif isinstance(val, (list, dict)) and val:
                vc_lines.append(f"- {label}：{json.dumps(val, ensure_ascii=False)}")
        if vc_lines:
            parts.append("本卷世界逻辑合同：\n" + "\n".join(vc_lines))
        return "\n\n".join(parts)

    def to_system_prompt(self, token_budget: int = 9500) -> str:
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

        # C3/F4: deterministic related-chapter recall + cast roster at the L1
        # tail. C4/F3: the compass direction anchor goes *after* it (most
        # tail-ward) so under keep-tail truncation the higher-priority direction
        # anchor survives first; the token_budget bump protects the L1 head.
        if self.related_chapter_recall:
            l1_parts.append(self.related_chapter_recall)
        if self.compass_anchor:
            l1_parts.append(self.compass_anchor)

        l1_text = "\n\n".join(l1_parts)
        l1_text = self._truncate_to_budget(l1_text, budget_l1)
        if l1_text:
            sections.append(f"=== 叙事上下文 ===\n{l1_text}")

        # ---- Layer 2: Facts ----
        l2_parts: list[str] = []

        if self.authoritative_fact_contract:
            l2_parts.append(
                "【权威事实契约(高于向量检索/角色卡/大纲节选)】\n"
                f"{self.authoritative_fact_contract}"
            )

        if self.world_rules:
            rules_text = "\n".join(f"- {r}" for r in self.world_rules)
            l2_parts.append(f"【世界规则(不可违反)】\n{rules_text}")

        if self.writing_rules:
            wr_text = "\n".join(f"- {r}" for r in self.writing_rules)
            l2_parts.append(f"【写作规则(必须遵守)】\n{wr_text}")

        if self.character_cards:
            cards_text = "\n".join(c.to_prompt() for c in self.character_cards)
            l2_parts.append(f"【活跃角色状态】\n{cards_text}")

        if self.cognition_boundaries:
            l2_parts.append(
                "【人物认知边界】\n"
                "角色只能依据其「知道」列表行动；写作时不得让角色说出/利用其"
                "「不知道」列表中的信息（除非本章安排了明确的获知路径）。\n"
                f"{self.cognition_boundaries}"
            )

        if self.foreshadow_triplets or self.foreshadow_debt_warning:
            fs_lines = [f.to_prompt() for f in self.foreshadow_triplets]
            if self.foreshadow_debt_warning:
                fs_lines.append(self.foreshadow_debt_warning)
            l2_parts.append("【伏笔追踪】\n" + "\n".join(fs_lines))

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

        # C2/F1: book-level style-tic mirror at the L2 tail. _truncate_to_budget
        # keeps the *tail*, so a tail block survives; the token_budget bump
        # (8000->9500) is what keeps the L2 *head* (world rules) from being
        # dropped when the fact-constraint block is large. The mirror is a
        # "dampen your tics" hint, safe to lose under extreme budget pressure.
        if self.style_tic_mirror:
            l2_parts.append(self.style_tic_mirror)

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
        """Release a self-created session (no-op if caller injected ``db``).

        v1.10: rollback any pending transaction before closing so the
        connection does not end up stuck in ``idle in transaction`` state
        when callers forget to commit (e.g. analytics-only reads).
        """
        if self._owns_db and self._db is not None:
            try:
                await self._db.rollback()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None
            self._owns_db = False

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

        # Task A2: ``chapter_idx`` is the volume-local DB ``Chapter.chapter_idx``
        # (all build() callers pass it straight from the Chapter row), while
        # ``Foreshadow.planted_chapter`` is book-global. Convert once here so
        # the foreshadow debt computation compares like with like; from
        # volume 2 onward the local value made ages negative and silently
        # suppressed every debt alert. Proximity windows / golden-three-chapter
        # logic intentionally stay in the volume-local domain. Fail-safe:
        # fall back to the local value rather than blocking generation
        # (debt may under-report, never crash).
        try:
            global_chapter_idx = await self._resolve_global_chapter_idx(
                project_id, volume_id, chapter_idx
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Global chapter idx resolution failed, falling back to local %s: %s",
                chapter_idx, exc,
            )
            global_chapter_idx = int(chapter_idx)

        # Lazy imports to avoid circular dependency (layers import dataclasses from this module)
        from app.services.context_layers import build_proximity_layer, build_facts_layer, build_rag_layer

        # Layer 1: Proximity
        await build_proximity_layer(pack, project_id, volume_id, chapter_idx, await self._get_db())
        # Layer 2: Facts
        await build_facts_layer(
            pack, project_id, chapter_idx, await self._get_db(),
            global_chapter_idx=global_chapter_idx,
        )
        try:
            contract = await build_fact_contract(await self._get_db(), str(project_id))
            pack.authoritative_fact_contract = contract.to_prompt()
        except Exception as contract_err:
            logger.warning("Failed to build authoritative fact contract: %s", contract_err)
        # Layer 3: RAG
        await build_rag_layer(pack, project_id, chapter_idx, await self._get_db())

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

        # PR-DAMP: dialogue damping / plain register / focal measure guidance.
        try:
            from app.services.checkers.anti_ai_checker import DIALOGUE_DAMPING_DIRECTIVES
            pack.writing_guidance.extend(DIALOGUE_DAMPING_DIRECTIVES)
        except Exception as _damp_err:
            logger.debug("PR-DAMP directive injection skipped: %s", _damp_err)

        # v0.9: clear the invalidation flag after a successful rebuild so
        # subsequent builds can hit any downstream cache again.
        if cache_was_invalidated:
            try:
                from app.services import ctxpack_cache

                await ctxpack_cache.clear(project_id)
            except Exception as exc:
                logger.debug("ctxpack invalidation clear failed: %s", exc)

        return pack

    async def _resolve_global_chapter_idx(
        self,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
    ) -> int:
        """Convert the volume-local ``chapter_idx`` into a book-global index.

        Convention, verified against the write side (Task A2): the
        ``foreshadows.planted_chapter`` column is populated through
        ``foreshadow_lifecycle.chapter_global_idx(db, pid, vol.volume_idx,
        chapter.chapter_idx)`` — see api/generate.py (chapter content save)
        and chapter_outline_expander.py L238 — where ``chapter.chapter_idx``
        is the DB value materialized 1-based per volume (api/volumes.py
        uses ``i + 1``; the lifecycle docstrings say "0-based" but the code
        never re-bases). ``build()`` receives that same volume-local DB
        value, so applying the *identical* conversion (chapter-count offset
        of earlier volumes + local idx) lands in the same domain as
        ``planted_chapter`` regardless of the 0/1-base of the local index.
        Example: vol 1 has 10 chapters -> vol 2 ch 1 (local) -> global 11.

        Fail-safe: returns ``chapter_idx`` unchanged on any error (debt may
        under-report, but context building never breaks).
        """
        try:
            db = await self._get_db()
            vol_idx = (
                await db.execute(
                    select(Volume.volume_idx).where(Volume.id == str(volume_id))
                )
            ).scalar_one_or_none()
            if vol_idx is None:
                return int(chapter_idx)
            from app.services.foreshadow_lifecycle import chapter_global_idx

            return int(
                await chapter_global_idx(
                    db, str(project_id), int(vol_idx), int(chapter_idx)
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to resolve global chapter idx (volume_id=%s, local=%s): %s",
                volume_id, chapter_idx, exc,
            )
            return int(chapter_idx)

    # ------------------------------------------------------------------
    # Delegate methods (backward compat for tests / external callers)
    # ------------------------------------------------------------------

    async def _build_proximity(
        self, pack: ContextPack, project_id, volume_id, chapter_idx
    ) -> None:
        from app.services.context_layers.proximity import build_proximity_layer
        await build_proximity_layer(pack, project_id, volume_id, chapter_idx, await self._get_db())

    async def _build_facts(
        self, pack: ContextPack, project_id, chapter_idx, global_chapter_idx=None
    ) -> None:
        from app.services.context_layers.facts import build_facts_layer
        await build_facts_layer(pack, project_id, chapter_idx, await self._get_db(), global_chapter_idx=global_chapter_idx)

    async def _build_rag(
        self, pack: ContextPack, project_id, chapter_idx
    ) -> None:
        from app.services.context_layers.rag import build_rag_layer
        await build_rag_layer(pack, project_id, chapter_idx, await self._get_db())

    async def _load_style_samples(self, pack: ContextPack, project_id: str) -> None:
        try:
            db = await self._get_db()
            project = await db.get(Project, project_id)
            if not project:
                return

            settings_json = project.settings_json or {}
            style_ref = settings_json.get("style_reference", {}) or {}

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
                        return

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
        from app.services.context_layers.rag import render_style_profile
        return render_style_profile(profile)

    async def _aggregate_style_cards(self, db, book_id: str, top_k: int = 12) -> list[str]:
        from app.services.context_layers.rag import aggregate_style_cards
        return await aggregate_style_cards(db, book_id, top_k=top_k)


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
