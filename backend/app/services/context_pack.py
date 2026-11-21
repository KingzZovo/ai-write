"""
Three-Layer Context Pack Builder

Replaces the simple ContextAssembler with a structured 3-layer context pack system
designed for long-form Chinese web novel generation.

Layer 1: Proximity Layer (~40% tokens)
- Last 5 chapter summaries plus bounded current prose from the nearest chapter
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
from app.services.outline_authority import outline_for_generation

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.db.session import async_session_factory
from app.services.summary_provenance import current_summary_sql
from app.models.project import (
    Chapter,
    Character,
    Outline,
    Project,
    Volume,
)
from app.services.narrative_contract import WORLD_LOGIC_CONTRACT
from app.services.context_budget import ContextLayer, allocate_context
from app.services.context_errors import RequiredContextUnavailable
from app.services.cognition_retrieval import CognitionPrompt
from app.services.outline_contract import CHAPTER_CONTRACT_FIELDS, render_contract_value
from app.services.fact_contract import build_fact_contract
from app.services.character_context import CharacterCard, registered_alias_candidates
from app.services.entity_timeline import RelationshipSnapshot
from app.services.relationship_context import render_relationship_aliases, render_relationship_record

logger = logging.getLogger(__name__)

# 2026-11-16 实测(网关 http://127.0.0.1:8317,deepseek-chat,真实章节正文):
# 1654 字→1116 token、4321 字→2953、8093 字→5583,即 **1.45~1.48 汉字/token**,
# 与这里的 1.5 吻合(略偏乐观)。此前报告里"4.00 汉字/token"来自另一组测法,
# 与本口径不符,已作废;不要据此调大本常量,否则预算会实际超发约 2.7 倍。
CHARS_PER_TOKEN = 1.5

# _render_chapter_outline_block 已经逐段渲染过的字段名。标题之外一个都没渲染到时,
# 会把剩下的字段补出来,这里列的就是"不要重复补"的那些。
_RENDERED_OUTLINE_KEYS = frozenset({
    "title", "chapter_idx", "chapter_id", "volume_id", "volume_idx",
    "chapter_count", "word_count_target", "target_word_count", "target_words",
    "summary", "key_events", "prev_chapter_threads", "state_changes",
    "foreshadows_planted", "foreshadows_resolved", "next_chapter_hook",
    "scene_beats", "scenes", "raw_text",
    "_chapter_id", "_order_unavailable", "_author_text_revision",
}) | frozenset(CHAPTER_CONTRACT_FIELDS)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CFPGTriplet:
    """Foreshadow Triplet (Cause, Foreshadow, Payoff Goal).

    Keep plans and registered clues distinct; legacy scores do not set pacing.
    """

    cause: str
    foreshadow: str
    payoff_goal: str
    proximity: float = 0.0
    planned: bool = False
    evidence: str = ""

    def to_prompt(self) -> str:
        if self.planned:
            status = "[计划·尚待正文落实，不代表已经埋设]"
        else:
            status = "[已登记埋设·是否回应需核对正文]"
        evidence = f"\n  保存稿原句：{self.evidence}" if self.evidence else ""
        return f"{status} {self.foreshadow} (来源:{self.cause} -> 可选回应计划:{self.payoff_goal})" + evidence


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
        parts = [f"[CH-{self.chapter_idx}] {self.event}"]
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
            f"Quest最后出现: [CH-{self.last_quest_chapter}] | "
            f"Fire最后出现: [CH-{self.last_fire_chapter}] | "
            f"Constellation最后出现: [CH-{self.last_constellation_chapter}]"
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

    # Book-global index of the chapter being generated (0 = unknown). Set by
    # ``ContextPackBuilder.build``; strand-gap arithmetic and every other
    # book-global comparison must use this, never the volume-local
    # ``current_outline["chapter_idx"]``.
    global_chapter_idx: int = 0
    # Same persisted projection boundary, also passed to RAG/aggregate readers.
    history_before_global_idx: int = 0
    target_chapter_id: str | None = None

    # Layer 1: Proximity (~40%)
    recent_summaries: list[str] = field(default_factory=list)  # last 5 chapters
    previous_chapter_text: str = ""  # current source, independent of summary validity
    current_content: str = ""
    current_outline: dict = field(default_factory=dict)
    future_outlines: list[str] = field(default_factory=list)  # next 10 chapters
    # v1.7.4 P0-1: book/volume outline injection (was previously missing)
    book_outline_excerpt: str = ""
    volume_outline: dict = field(default_factory=dict)
    arc_author_guidance: str = ""
    arc_replaces_volume_outline: bool = False
    # Hierarchical rollup: source-bound 全书至此梗概 (regenerated from
    # VolumeSummary rows on volume transitions; stored in
    # projects.settings_json["book_synopsis"]). Injected only for chapters
    # beyond volume 1; empty ⇒ silently absent.
    book_synopsis: str = ""

    # Layer 2: Facts (~33%)
    authoritative_fact_contract: str = ""
    world_rules: list[str] = field(default_factory=list)
    character_cards: list[CharacterCard] = field(default_factory=list)
    relationship_records: list[RelationshipSnapshot] = field(default_factory=list)
    # Exact profile aliases used by relationship endpoints. Keep every owner;
    # this is a recall aid, not permission to resolve or rename a prose fact.
    relationship_aliases: dict[str, list[str]] = field(default_factory=dict)
    # W5: chapter-relevant characters that did not fit within the full-card
    # cap (CONTEXT_PACK_MAX_CHARACTER_CARDS). Listed name-only in the
    # Layer-0 roster so name fidelity is preserved without card bloat.
    roster_extra_names: list[str] = field(default_factory=list)
    foreshadow_triplets: list[CFPGTriplet] = field(default_factory=list)
    # Optional older-record reminder; bookkeeping scores never impose plot deadlines.
    # Legacy externally supplied note; generation no longer computes debt.
    foreshadow_debt_warning: str = ""
    timeline_anchors: list[TimeAnchor] = field(default_factory=list)
    contradiction_cache: list[str] = field(default_factory=list)
    strand_tracker: StrandTracker = field(default_factory=StrandTracker)
    # v0.8 ContextPack v3: 4th recall path, scoped to project.genre_profile.
    writing_rules: list[str] = field(default_factory=list)
    # Source-bound cognition; oversized history stays deferred until the
    # caller closes the read transaction and awaits resolve_cognition().
    cognition_boundaries: str | CognitionPrompt = ""
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
    # Reference-book style/beat recall lines (three-way recall). Rendered in
    # the L3 style block (budget shared with style_samples) instead of
    # competing with chapter summaries for the rag_snippets[:5] slots.
    style_recall: list[str] = field(default_factory=list)
    # Tier-4 (自由检索层): verbatim chapter-chunk recall lines (each <=300
    # chars, current chapter excluded). Rendered at the L3 HEAD so under
    # keep-tail truncation they are the first L3 material to degrade.
    chunk_recall: list[str] = field(default_factory=list)
    # Tier-3 (细读层): pre-rendered 【旧人重现·原文片段】 block for
    # long-absent returning characters (memory-card excerpts). Rendered at
    # the L3 tail (after the style block) so it survives truncation longest.
    returning_character_drilldown: str = ""

    # Meta (~7%)
    writing_guidance: list[str] = field(default_factory=list)
    hook_suggestion: str = ""

    budget_trace: list[dict] = field(default_factory=list, init=False, repr=False)

    def _estimate_tokens(self, text: str) -> int:
        return int(len(text) / CHARS_PER_TOKEN)

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
        co = outline_for_generation(co)
        if isinstance(co.get("raw_text"), str) and co["raw_text"].strip() and not any(co.get(key) for key in ("summary", "key_events", "scene_beats", "scenes", "state_changes")):
            return "作者当前细纲：\n" + co["raw_text"]
        parts: list[str] = []
        title = co.get("title") or ""
        cidx = co.get("chapter_idx")
        if title or cidx:
            parts.append(f"《[CH-{cidx}] {title}》".strip())
        # 标题行不算正文内容:判断"这一章的大纲到底给出什么"时要以它之后的内容为准。
        header_lines = len(parts)
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

        contract_lines: list[str] = []
        for key, label in CHAPTER_CONTRACT_FIELDS.items():
            value = render_contract_value(co.get(key))
            if value:
                contract_lines.append(f"- {label}：{value}")
        if contract_lines:
            parts.append("本章世界逻辑合同：\n" + "\n".join(contract_lines))

        scenes = render_contract_value(co.get("scene_beats") or co.get("scenes"))
        if scenes:
            parts.append(f"场景推进（依照先后顺序展开）：\n{scenes}")

        if len(parts) <= header_lines:
            # 标题之外没有任何可识别内容:不能只交出一个标题。旧大纲/导入大纲
            # 可能把内容放在 brief、key_action、pov 这类字段名下,旧实现会静默丢掉,
            # 整章等于没给大纲(第 2 章起整章脱纲)。这里把还没用过的字段补出来。
            for key, value in co.items():
                if key in _RENDERED_OUTLINE_KEYS:
                    continue
                text = render_contract_value(value)
                if text:
                    parts.append(f"{key}:{text}")
        if not parts:
            # 降级:没有能识别的字段,还有 dict 内容 → fallback dump
            return json.dumps(co, ensure_ascii=False, indent=2)
        return "\n".join(parts)
        return "\n".join(parts)

    def numeric_pin_lines(self) -> str:
        """大纲里已经写明的金额清单(供写手 user content 直接使用)。"""
        return self._numeric_pin_lines()

    def _numeric_pin_lines(self) -> str:
        """把大纲里已经写明的金额收成一张短清单,交给写手当硬约束。

        实测(2026-11-14 连续 24 章):第 7 章大纲让角色"算出卖这批活能挣多少",
        模型自己写了 880;第 8 章大纲钉的是 820,两章对不上,而且没有任何机制会拦。
        这里把本卷各章梗概与后续走向里带金额的片段按章列出——只列**已经写明的数**,
        不判断谁对谁错,也不改大纲;片段刻意取短,避免把后文剧情提前说给写手。
        """
        from app.services.numeric_pins import outline_pin_blocks, pin_block

        current = self.current_outline if isinstance(self.current_outline, dict) else {}
        money = current.get("money_totals")
        money = money.strip() if isinstance(money, str) else ""
        # 本章计划里已定的金额排在最前:它是"本轮该写多少"的第一手依据,
        # 而卷纲/后章金额只是参照(pin_block 有条数上限,靠后就会被挤掉)。
        extra = [("本章", money)] if money else None
        return pin_block(outline_pin_blocks(self.volume_outline, self.future_outlines, extra=extra))

    def _render_volume_outline_block(self) -> str:
        """Render volume_outline dict into a readable block for the prompt.

        Volume outline schema (from outline_generator):
          title, volume_idx, core_conflict, emotional_arc,
          new_characters[{name,role,identity}], turning_points[str],
          foreshadows{planted:[{description,resolve_conditions}], resolved:[str]},
          chapter_summaries[{title,summary,key_events,chapter_idx}],
          transition_to_next, departing_characters
        """
        vo = outline_for_generation(self.volume_outline or {})
        if not vo:
            return ""
        if isinstance(vo.get("raw_text"), str) and vo["raw_text"].strip() and not any(vo.get(key) for key in ("core_conflict", "turning_points", "chapter_summaries", "emotional_arc")):
            return "作者当前卷纲：\n" + vo["raw_text"]
        parts: list[str] = []
        title = vo.get("title") or ""
        vidx = vo.get("volume_idx")
        if title or vidx:
            parts.append(f"《[VOL-{vidx}] {title}》".strip())
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
                for entry in planted:
                    # Preserve the complete planned entry, including optional
                    # placement and conditions. It is not evidence of prose.
                    if isinstance(entry, str):
                        fs_lines.append('- ' + entry)
                    elif isinstance(entry, dict):
                        fs_lines.append('- ' + json.dumps(entry, ensure_ascii=False))
                if fs_lines:
                    parts.append("卷纲埋设计划（尚待正文落实）：\n" + "\n".join(fs_lines))
            resolved = fs.get('resolved')
            if isinstance(resolved, list) and resolved:
                parts.append('卷纲回应计划（不代表已经兑现）：\n' + json.dumps(resolved, ensure_ascii=False))
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

    async def resolve_cognition(self, *, db=None, user_instruction='') -> None:
        """Finish semantic recall after the owner ends the read transaction."""
        from app.services.cognition_retrieval import resolve_prompt_context
        self.cognition_boundaries = await resolve_prompt_context(
            self.cognition_boundaries, db=db, extra_context=user_instruction,
        )

    def to_system_prompt(self, token_budget: int | None = None) -> str:
        """Render source-labelled blocks with a bounded character estimate.

        Required chapter tasks, facts, rules and cognition are indivisible and
        may borrow across layers. Optional material uses weighted remaining
        slots. budget_trace records omissions; mandatory overflow is explicit.
        """
        if not isinstance(self.cognition_boundaries, str):
            raise RequiredContextUnavailable('character_cognition_retrieval_pending')
        if self.arc_replaces_volume_outline and not self.arc_author_guidance:
            raise RequiredContextUnavailable('arc_author_material_missing')
        if token_budget is None:
            try:
                from app.config import settings

                token_budget = int(
                    getattr(settings, "CONTEXT_PACK_TOKEN_BUDGET", 9500)
                )
            except Exception:  # noqa: BLE001
                token_budget = 9500
        self.budget_trace = []
        sections = ContextLayer("roster")
        if self.character_cards or self.roster_extra_names or self.relationship_records:
            names = [c.name for c in self.character_cards]
            for relationship in self.relationship_records:
                for endpoint in (relationship.source, relationship.target):
                    for name in self.relationship_aliases.get(endpoint, [endpoint]):
                        if name not in names:
                            names.append(name)
            for n in self.roster_extra_names:
                if n and n not in names:
                    names.append(n)
            roster = (
                "【角色名册(不可截断·最高优先)】\n"
                f"本次上下文涉及的已登记角色名称（并非都需出场）：{'、'.join(names)}\n"
                "可依角色档案或作者明确要求使用别名与称谓；同一称呼可能指向不同人物，须结合场景关系判断，不得擅自改名或混同身份。"
            )
            sections.add('roster', roster, required=True, priority=100)

        l1_parts = ContextLayer("proximity")

        if self.book_outline_excerpt:
            l1_parts.add('book_outline', f"【全书大纲(节选)】\n{self.book_outline_excerpt}", required=False, priority=30)

        if self.volume_outline and not self.arc_replaces_volume_outline:
            vo_text = self._render_volume_outline_block()
            if vo_text:
                l1_parts.add('volume_outline', f"【本卷大纲】\n{vo_text}", required=False, priority=60)

        if self.arc_author_guidance:
            l1_parts.add('arc_author_guidance', self.arc_author_guidance, required=True, priority=100)

        if self.book_synopsis:
            l1_parts.add('book_synopsis', f"【全书至此梗概】\n{self.book_synopsis}", required=False, priority=40)

        for i, summary in enumerate(self.recent_summaries):
            latest = i == len(self.recent_summaries) - 1
            l1_parts.add(f'recent_summaries[{i}]', f"【近五章摘要】\n[前文-{i + 1}] {summary}",
                         required=latest, priority=90 + i)

        if self.previous_chapter_text:
            l1_parts.add('previous_chapter_text', self.previous_chapter_text,
                         required=True, priority=100)

        if self.current_content:
            l1_parts.add('current_content', f"【本章已有内容】\n{self.current_content}", required=False, priority=100, keep_tail=True)

        if self.current_outline:
            outline_str = self._render_chapter_outline_block(self.current_outline)
            l1_parts.add('current_outline', f"【本章大纲】\n{outline_str}", required=True, priority=100)

        if self.future_outlines:
            future_text = "\n".join(
                f"[后续-{i}] {o}" for i, o in enumerate(self.future_outlines, 1)
            )
            # 2026-11-16 真实 24 章项目实测:预算 9500 时这一块会被**整块省略**
            # (ch7 丢 3177 字、ch18 丢 1086 字)—— L1 只分到剩余额度的 40%,
            # 而本块优先级最低(20)。这里给块本身加上限:列表按由近到远排列,
            # 因此截尾保近 —— 宁可少给最远的几章,也不要整块丢掉。
            # 注意只截"渲染出来的块",不动 self.future_outlines:
            # _numeric_pin_lines 仍要从完整列表里取金额。
            try:
                from app.config import settings
                cap = int(getattr(settings, "CONTEXT_FUTURE_OUTLINE_MAX_CHARS", 3000))
            except Exception:  # noqa: BLE001
                cap = 3000
            if len(future_text) > cap:
                clipped = future_text[:cap].rsplit("\n", 1)[0] or future_text[:cap]
                future_text = clipped + "\n[更远的后章走向因篇幅省略]"
            l1_parts.add('future_outlines', f"【后续走向(参考)】\n{future_text}", required=False, priority=20)

        numeric_pins = self._numeric_pin_lines()
        if numeric_pins:
            l1_parts.add('numeric_pins',
                         f"【大纲已定的金额(本章写到的金额必须与这里一致)】\n{numeric_pins}",
                         required=False, priority=75)

        if self.related_chapter_recall:
            l1_parts.add('related_chapter_recall', self.related_chapter_recall, required=False, priority=50)
        if self.compass_anchor:
            l1_parts.add('compass_anchor', self.compass_anchor, required=True, priority=100)

        l2_parts = ContextLayer("facts")

        if self.authoritative_fact_contract:
            l2_parts.add('authoritative_fact_contract', "【角色身份记录（不替代当前状态）】\n"
                f"{self.authoritative_fact_contract}", required=True, priority=100)

        if self.world_rules:
            rules_text = "\n".join(f"- {r}" for r in self.world_rules)
            l2_parts.add('world_rules', f"【世界规则(不可违反)】\n{rules_text}", required=True, priority=100)

        if self.writing_rules:
            wr_text = "\n".join(f"- {r}" for r in self.writing_rules)
            l2_parts.add('writing_rules', f"【写作规则(必须遵守)】\n{wr_text}", required=True, priority=100)

        if self.character_cards:
            cards_text = "\n".join(c.to_prompt() for c in self.character_cards)
            l2_parts.add('character_cards', "【活跃角色状态】\n"
                "档案项是设定参考，不证明事件已经发生。正文状态逐字段保留来源章，"
                "同一事项以后章明确变化为准；伤势、身份与情绪分别理解。"
                "null表示该项未明确，不用旧值补齐；未列出的属性不等于不存在。\n"
                f"{cards_text}", required=True, priority=90)

        if self.cognition_boundaries:
            l2_parts.add('cognition_boundaries', "【人物认知边界】\n"
                "按获知时间、原文限定和叙述视角使用以下记录；节选不是人物的全部知识。"
                "不得把读者已知、旁人猜测或后续计划当成角色已经知情，"
                "也不据缺少记录断言角色不知道。\n"
                f"{self.cognition_boundaries}", required=True, priority=100)

        if self.relationship_records:
            prose_relations = [rel for rel in self.relationship_records if rel.origin != 'author']
            author_relations = [rel for rel in self.relationship_records if rel.origin == 'author']
            blocks = []
            if prose_relations:
                blocks.append('【前文关系记录】\n'
                    '按来源章理解关系进展，多项关系分别保留；档案参考不能把正文已发生的变化重置。'
                    '标为待核对的旧关系不保证仍然成立，不能擅自补写关系结束或恢复的经过。'
                    '箭头保留登记方向，不据此反向推定知情、信任或人物身份；具体含义仍须结合原文和作者指令。\n'
                    + '\n'.join(render_relationship_record(rel) for rel in prose_relations))
            if author_relations:
                blocks.append('【作者关系设定】\n'
                    '以下是作者登记或修订的设定，不证明关系已在正文成立；与前文不一致时须核对，不能编造过渡经历。\n'
                    + '\n'.join(render_relationship_record(rel) for rel in author_relations))
            relationships_text = '\n'.join(blocks)
            alias_notes = render_relationship_aliases(self.relationship_aliases)
            if alias_notes:
                relationships_text += '\n' + alias_notes
            l2_parts.add('relationship_records', relationships_text, required=True, priority=95)

        if self.foreshadow_triplets or self.foreshadow_debt_warning:
            fs_lines = [f.to_prompt() for f in self.foreshadow_triplets]
            if self.foreshadow_debt_warning:
                fs_lines.append(self.foreshadow_debt_warning)
            l2_parts.add('foreshadows', "【伏笔追踪】\n" + "\n".join(fs_lines), required=False, priority=80)

        if self.timeline_anchors:
            tl_text = "\n".join(a.to_prompt() for a in self.timeline_anchors[-10:])
            l2_parts.add('timeline_anchors', f"【时间线锚点】\n{tl_text}", required=False, priority=70)

        if self.contradiction_cache:
            cc_text = "\n".join(f"- {c}" for c in self.contradiction_cache)
            l2_parts.add('contradiction_cache', f"【已知矛盾(务必避免)】\n{cc_text}", required=True, priority=100)

        strand_warnings = self.strand_tracker.get_warnings(
            self.global_chapter_idx
            or self.current_outline.get("chapter_idx", 0)
        )
        if strand_warnings:
            sw_text = "\n".join(strand_warnings)
            l2_parts.add('strand_warnings', f"【线索平衡提醒】\n{sw_text}", required=False, priority=40)

        if self.style_tic_mirror:
            l2_parts.add('style_tic_mirror', self.style_tic_mirror, required=False, priority=10)

        l3_parts = ContextLayer("reference")

        if self.chunk_recall:
            chunk_text = "\n---\n".join(self.chunk_recall[:3])
            l3_parts.add('chunk_recall', f"【原文回读】\n{chunk_text}", required=False, priority=10)

        if self.rag_snippets:
            rag_text = "\n---\n".join(self.rag_snippets[:5])
            l3_parts.add('rag_snippets', f"【相关片段】\n{rag_text}", required=False, priority=20)

        if self.dialogue_samples:
            ds_parts: list[str] = []
            for char_name, lines in self.dialogue_samples.items():
                sample_lines = "\n".join(f'  "{line}"' for line in lines[:3])
                ds_parts.append(f"{char_name}:\n{sample_lines}")
            l3_parts.add('dialogue_samples', f"【角色对话样本】\n" + "\n".join(ds_parts), required=False, priority=30)

        style_block_parts: list[str] = []
        if self.style_recall:
            style_block_parts.append("\n".join(self.style_recall[:6]))
        if self.style_samples:
            style_block_parts.append("\n---\n".join(self.style_samples[:3]))
        if style_block_parts:
            l3_parts.add('style_reference', "【风格参考】\n" + "\n---\n".join(style_block_parts), required=False, priority=40)

        if self.returning_character_drilldown:
            l3_parts.add('returning_character_drilldown', self.returning_character_drilldown, required=False, priority=50)

        meta_parts = ContextLayer("instructions")

        if self.writing_guidance:
            wg_text = "\n".join(f"- {g}" for g in self.writing_guidance)
            meta_parts.add('writing_guidance', f"【写作指导】\n{wg_text}", required=False, priority=80)

        if self.hook_suggestion:
            meta_parts.add('hook_suggestion', f"【钩子建议】\n{self.hook_suggestion}", required=False, priority=20)

        blocks = sections.blocks + l1_parts.blocks + l2_parts.blocks + l3_parts.blocks + meta_parts.blocks
        full_prompt, self.budget_trace = allocate_context(blocks, int(token_budget * CHARS_PER_TOKEN))
        logger.info("ContextPack budget=%d estimated_tokens=%d trace=%s", token_budget,
                    self._estimate_tokens(full_prompt), self.budget_trace)
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
# Tier-3 (细读层): returning-character drill-down helpers (pure, no LLM)
# ---------------------------------------------------------------------------


def is_returning_character(
    last_seen: int, current_idx: int, gap_chapters: int
) -> bool:
    """Deterministic drill-down trigger: absent for MORE than the gap.

    A character last seen exactly ``gap_chapters`` ago does NOT trigger;
    ``gap_chapters + 1`` does.
    """
    return (int(current_idx) - int(last_seen)) > int(gap_chapters)


def render_drilldown_block(
    entries: list[dict],
    current_idx: int,
    *,
    max_chars: int = 1200,
) -> str:
    """Render the 【旧人重现·原文片段】 L3 block from memory-card entries.

    ``entries`` is ``[{"name", "last_seen", "cards": [(global_idx,
    card_type, excerpt), ...]}]`` — cards already selected/ordered by the
    caller (one early verified anchor, then recent verified excerpts). Total
    output is capped at ``max_chars``: whole per-character sections are
    dropped tail-first once the cap is reached. Empty input ⇒ "" (silent
    absence).
    """
    if not entries:
        return ""
    header = (
        "【旧人重现·原文片段】若本章涉及以下人物，请回读原文核对口吻与状态。"
        "名字被提及不等于人物在场或知情；片段也不证明其首次登场："
    )
    sections: list[str] = []
    for e in entries:
        name = e.get("name") or ""
        last_seen = e.get("last_seen")
        cards = e.get("cards") or []
        if not name or last_seen is None or not cards:
            continue
        gap = int(current_idx) - int(last_seen)
        label = "最近核实片段" if e.get("verified_anchor") else "历史片段"
        lines = [f"▍{name}（{label} [CH-{last_seen}]，已隔{gap}章）"]
        for gidx, card_type, excerpt in cards:
            if not excerpt:
                continue
            tag = "早期片段" if card_type == "first_appearance" else "片段"
            lines.append(f"[CH-{gidx}·{tag}] {excerpt}")
        if len(lines) > 1:
            sections.append("\n".join(lines))

    out: list[str] = []
    used = len(header)
    for sec in sections:
        cost = len(sec) + 1
        if used + cost > max_chars:
            break
        out.append(sec)
        used += cost
    if not out:
        return ""
    return "\n".join([header, *out])


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
            A collected ContextPack. After ending the read transaction, await
            pack.resolve_cognition() before rendering or calling a model.
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

        # Every reader uses the persisted chapter position. Unknown history
        # cannot be replaced with a new count of chapters in earlier volumes.
        try:
            pack.history_before_global_idx = await self._resolve_global_chapter_idx(
                project_id, volume_id, chapter_idx
            )
        except RequiredContextUnavailable:
            raise
        except Exception as exc:
            raise RequiredContextUnavailable("persisted_chapter_chronology_unavailable") from exc
        global_chapter_idx = pack.history_before_global_idx
        pack.global_chapter_idx = global_chapter_idx

        # Layer 1: Proximity
        await self._build_proximity(pack, project_id, volume_id, chapter_idx)
        # Layer 2: Facts
        await self._build_facts(
            pack, project_id, chapter_idx, global_chapter_idx=global_chapter_idx,
        )
        try:
            contract = await build_fact_contract(await self._get_db(), str(project_id))
            pack.authoritative_fact_contract = contract.to_prompt()
        except Exception as contract_err:
            logger.warning("Failed to build authoritative fact contract: %s", type(contract_err).__name__)
            raise RequiredContextUnavailable("character_identity_context_unavailable") from contract_err
        # Layer 3: RAG
        await self._build_rag(pack, project_id, chapter_idx)

        # Strand warnings (strand tracker lives on the book-global axis)
        warnings = pack.strand_tracker.get_warnings(global_chapter_idx)
        pack.writing_guidance.extend(warnings)

        # PR-AI1: inject naming/glossary directive so chapter generation
        # is told upfront what is and is not allowed when coining items
        # / techniques / titles. Cheap, fail-safe, no DB hits.
        try:
            from app.services.checkers.anti_ai_checker import NAMING_DIRECTIVE
            pack.writing_guidance.append(NAMING_DIRECTIVE)
        except Exception as _ai1_err:
            logger.debug("PR-AI1 naming directive injection skipped: %s", _ai1_err)

        # Author-selected style and the shared writer contract own prose guidance.
        # Do not inject checker heuristics as paragraph/dialogue quotas or force
        # indirect motives: ordinary explanation and cooperation can be intended.

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
        """Resolve the persisted, project-scoped position used by memory."""
        from app.services.memory_chronology import resolve_history_boundary
        try:
            value = await resolve_history_boundary(
                await self._get_db(), str(project_id), str(volume_id), chapter_idx
            )
            if type(value) is not int or value <= 0:
                raise RequiredContextUnavailable("persisted_chapter_chronology_unavailable")
            return value
        except RequiredContextUnavailable:
            raise
        except Exception as exc:
            raise RequiredContextUnavailable("persisted_chapter_chronology_unavailable") from exc

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
        try:  # 与本文件其他位置一致:配置不可用时退回内置默认值。
            from app.config import settings
        except Exception:  # noqa: BLE001
            settings = None
        vid = str(volume_id)

        # 2026-11-16:窗口改为可配置。旧实现把这些数字写死在 SQL/切片里,长书
        # 里"近 5 章摘要 / 后 10 章大纲 / 全书大纲只留头 1500 + 尾 500 字"先于
        # 预算生效,中段上下文进不了提示。默认值与旧行为一致或只放大非必要块:
        # 前章正文是 required 块,放大它会把 ContextBudgetExceeded 变成真实故障,
        # 所以默认值保持 3500,由运维在加预算后再上调。
        prev_chars = int(getattr(settings, "CONTEXT_PREV_CHAPTER_TAIL_CHARS", 3500))
        recent_n = int(getattr(settings, "CONTEXT_RECENT_SUMMARY_CHAPTERS", 8))
        future_n = int(getattr(settings, "CONTEXT_FUTURE_OUTLINE_CHAPTERS", 20))
        book_head = int(getattr(settings, "CONTEXT_BOOK_OUTLINE_HEAD_CHARS", 4000))
        book_tail = int(getattr(settings, "CONTEXT_BOOK_OUTLINE_TAIL_CHARS", 2000))

        # Validate the project/volume boundary before any optional fallback.
        volume = await db.scalar(select(Volume).where(Volume.id == vid, Volume.project_id == pid))
        if volume is None:
            raise ValueError("上下文目标卷不存在或不属于当前作品")
        matches = (await db.scalars(select(Chapter).where(
            Chapter.volume_id == vid, Chapter.chapter_idx == chapter_idx).limit(2))).all()
        if len(matches) != 1:
            raise ValueError("上下文目标章节不存在或卷内章号重复，无法唯一定位")
        current_chapter = matches[0]
        pack.target_chapter_id = str(current_chapter.id)
        position = tuple_(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
        cursor = (volume.volume_idx, volume.id, current_chapter.chapter_idx, current_chapter.id)
        try:
            if pack.history_before_global_idx and current_chapter.global_idx != pack.history_before_global_idx:
                raise RequiredContextUnavailable('persisted_chapter_chronology_unavailable')
            # Bounded windows across the whole book. Previous prose is sliced
            # in SQL; never materialize full neighbouring chapters of a long book.
            base = select(Chapter, Volume.volume_idx, current_summary_sql().label("verified_summary")).join(Volume, Chapter.volume_id == Volume.id).where(
                Volume.project_id == pid).options(load_only(
                    Chapter.id, Chapter.chapter_idx, Chapter.title, Chapter.status, Chapter.outline_json))
            previous_base = select(
                Chapter.id, Chapter.chapter_idx, Chapter.status, Volume.volume_idx,
                current_summary_sql().label("verified_summary"),
                func.right(Chapter.content_text, prev_chars).label("prose_tail"),
                func.length(Chapter.content_text).label("prose_length"),
            ).join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == pid)
            previous = (await db.execute(previous_base.where(
                position < cursor
            ).order_by(Volume.volume_idx.desc(), Volume.id.desc(), Chapter.chapter_idx.desc(), Chapter.id.desc()).limit(recent_n))).all()
            future = (await db.execute(base.where(position > cursor).order_by(
                Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id).limit(future_n))).all()
            pack.recent_summaries = []
            for row in reversed(previous):
                boundary = "" if row.status == "completed" else "[待审草稿参考，不视为既定事实] "
                text = row.verified_summary or "摘要缺失或与当前稿件不匹配；须核对正文，不可推定情节已完成。"
                pack.recent_summaries.append(
                    f"[VOL-{row.volume_idx}/CH-{row.chapter_idx}; id={row.id}] {boundary}{text}"
                )
            pack.previous_chapter_text = ""
            if previous:
                row = previous[0]
                tail, length = row.prose_tail, row.prose_length
                label = f"[VOL-{row.volume_idx}/CH-{row.chapter_idx}; id={row.id}; status={row.status}]"
                boundary = ("当前已写正文用于衔接；规划和旧摘要不能覆盖这里的实际内容。"
                            if row.status == "completed" else
                            "待审草稿参考，不视为既定事实；不能因它有正文就假定已获作者认可。")
                ambiguous = len(previous) > 1 and (
                    previous[1].volume_idx, previous[1].chapter_idx
                ) == (row.volume_idx, row.chapter_idx)
                if ambiguous:
                    material = "紧邻前文章号重复，无法唯一定位正文；请先整理章节顺序。"
                elif not tail or not tail.strip():
                    material = "紧邻前章尚无正文；不能跳过它假定衔接事件已经发生。"
                elif (length or 0) <= prev_chars:
                    material = "正文全文：\n" + tail
                else:
                    # Do not present a cut-off opening as a complete statement.
                    # Prefer the next paragraph; retain a labelled raw tail if
                    # the entire window is one long paragraph.
                    split = tail.find("\n")
                    if split >= 0 and tail[split + 1:].strip():
                        tail = tail[split + 1:]
                        note = "正文结尾节选（前部省略，不代表全章）："
                    else:
                        note = "正文结尾节选（前部省略，开头可能位于句中，不可据残句推断完整事实）："
                    material = note + "\n" + tail
                pack.previous_chapter_text = "【紧邻前章当前正文】\n" + label + "\n" + boundary + "\n" + material

            pack.current_content = current_chapter.content_text or ""
            from app.services.outline_order import bind_chapter_content, volume_condition
            pack.current_outline = outline_for_generation(bind_chapter_content(current_chapter.outline_json or {}, current_chapter))
            pack.future_outlines = []
            for ch, vindex, _summary in future:
                oj = outline_for_generation(ch.outline_json)
                summary = (oj.get("summary") or oj.get("main_plot") or "") if isinstance(oj, dict) else oj or ""
                from app.services.numeric_pins import chapter_outline_line
                pack.future_outlines.append(chapter_outline_line(
                    volume_idx=vindex, chapter_idx=ch.chapter_idx, chapter_id=ch.id,
                    title=ch.title or "", summary=summary))

            # v1.7.4 P0-1: load book + volume outline in one query.
            selected_volume = None
            try:
                book = await db.scalar(select(Outline).where(
                    Outline.project_id == pid, Outline.level == "book"
                ).order_by(Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1))
                if book and isinstance(book.content_json, dict):
                    raw = book.content_json.get("raw_text") or book.content_json.get("summary") or ""
                    if isinstance(raw, str) and raw.strip():
                        pack.book_outline_excerpt = (raw[:book_head].rstrip() + "\n\n...(中部省略)...\n\n" + raw[-book_tail:].lstrip()) if len(raw) > book_head + book_tail else raw
                # Explicit stable ID wins; index is only a legacy fallback
                # when the payload does not declare a volume ID at all.
                selected_volume = await db.scalar(select(Outline).where(
                    Outline.project_id == pid, Outline.level == "volume",
                    volume_condition(vid, volume.volume_idx)
                ).order_by(Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1))
                pack.volume_outline = outline_for_generation(selected_volume.content_json if selected_volume else {})
                if pack.volume_outline:
                    pack.volume_outline['volume_idx'] = volume.volume_idx
            except Exception as e:
                logger.warning("Failed to load outlines (project=%s, ch=%d): %s", pid, chapter_idx, e)

            from app.services.arc_progress_material import load_arc_writing_context
            arc_material = await load_arc_writing_context(
                db, pid, vid, current_chapter.id, selected_volume,
            )
            pack.arc_author_guidance = arc_material.text
            pack.arc_replaces_volume_outline = arc_material.replaces_volume_outline

            # Aggregates must match current sources and precede this chapter.
            from app.services.aggregate_memory import current_book_synopsis, current_volume_summaries
            pack.book_synopsis = await current_book_synopsis(
                db, pid, before_volume_idx=volume.volume_idx,
                before_global_idx=pack.history_before_global_idx,
            )
            if chapter_idx <= 3:
                rows = await current_volume_summaries(
                    db, pid, before_volume_idx=volume.volume_idx,
                    before_global_idx=pack.history_before_global_idx,
                )
                pack.recent_summaries = [f"[前卷摘要] {row['text']}" for row in rows[-2:]] + pack.recent_summaries

        except SQLAlchemyError:
            raise
        except Exception as e:
            logger.warning("Failed to build proximity layer (project=%s, ch=%d): %s", str(project_id), chapter_idx, type(e).__name__)
            raise RequiredContextUnavailable("chapter_continuity_context_unavailable") from e

    # ------------------------------------------------------------------
    # Layer 2: Facts
    # ------------------------------------------------------------------

    async def _build_world_rules(self, pack: ContextPack, project_id) -> None:
        from app.services.world_rule_sources import read_world_rule_source
        from app.services.world_rule_material import render_world_rules
        source = await read_world_rule_source(await self._get_db(), project_id)
        pack.world_rules = [WORLD_LOGIC_CONTRACT, *render_world_rules(source, project_id)]

    async def _build_facts(
        self,
        pack: ContextPack,
        project_id: str | UUID,
        chapter_idx: int,
        global_chapter_idx: int | None = None,
    ) -> None:
        """Build Layer 2: world rules, character cards, foreshadows, timeline.

        ``chapter_idx`` is volume-local (DB ``Chapter.chapter_idx``).
        ``global_chapter_idx`` is the persisted Chapter.global_idx and drives
        comparisons with memory projection sources: character_locations,
        character_states, cognition, the
        timeline anchors (``Chapter.global_idx``), Neo4j character-state
        lookups, related-chapter recall and the strand tracker. Legacy direct
        callers may omit it only where local and persisted numbering coincide;
        production build() always supplies it. Foreshadow references additionally
        validate their current source and actual volume/chapter order.
        """
        db = await self._get_db()
        pid = str(project_id)
        gidx = (
            int(global_chapter_idx)
            if global_chapter_idx is not None
            else int(chapter_idx)
        )

        # Read current registered graph settings; stale PG copies cannot
        # override an author's edit or revive a deleted rule.
        try:
            await self._build_world_rules(pack, pid)
        except (SQLAlchemyError, RequiredContextUnavailable):
            raise
        except Exception as e:
            logger.warning("Failed to load world rules (project=%s): %s", pid, type(e).__name__)
            raise RequiredContextUnavailable("world_rules_context_unavailable") from e

        # Character cards from PostgreSQL characters + Neo4j state
        try:
            # Read both projections independently. A derived end date cannot
            # hide an earlier place before its closing source is verified.
            loc_by_char_id: dict[UUID, list[dict]] = {}
            try:
                from app.services.character_locations import verified_character_locations
                for row in await verified_character_locations(db, pid, gidx):
                    loc_by_char_id.setdefault(row['character_id'], []).append(row)
            except SQLAlchemyError:
                raise
            except Exception as e:
                logger.warning("Failed to load character locations: %s", type(e).__name__)
                raise RequiredContextUnavailable("character_locations_context_unavailable") from e

            char_result = await db.execute(
                select(Character)
                .where(Character.project_id == pid)
                .execution_options(autoflush=False)
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
                card.apply_locations(loc_by_char_id.get(char.id, []))

                aliases = profile.get("aliases")
                if isinstance(aliases, list):
                    card.aliases = list(dict.fromkeys(
                        alias.strip() for alias in aliases
                        if isinstance(alias, str) and alias.strip() and alias.strip() != card.name
                    ))

                # Parse relationships from profile
                rels = profile.get("relationships", {})
                if isinstance(rels, dict):
                    card.relationships = dict(rels)
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

            # Graph relationships are required; chapter_start edges are
            # book-global, so query at the global idx.
            await self._enrich_characters_from_neo4j(
                pack, pid, gidx
            )
        except (SQLAlchemyError, RequiredContextUnavailable):
            raise
        except Exception as e:
            logger.warning("Failed to load character cards (project=%s, ch=%d): %s", pid, chapter_idx, type(e).__name__)
            raise RequiredContextUnavailable("character_cards_context_unavailable") from e

        # Retain every source-verified state field on the global axis.
        await self._overlay_character_states(pack, pid, gidx)

        # W5: bound the full-card render to chapter-relevant characters so
        # large casts cannot truncate away the L2 head (fact contract +
        # world rules) or bloat the Layer-0 roster.
        from app.services.character_roster import load_historical_roster
        historical_roster = await load_historical_roster(db, pid, before_global_idx=gidx)
        await self._cap_character_cards(pack, pid, historical_roster=historical_roster)

        # Plans remain in current outlines; accepted prose clues carry exact
        # source evidence. Never label unverified legacy numbers as CH indices.
        try:
            from app.services.foreshadow_lifecycle import load_active_foreshadows_for_context
            references = await load_active_foreshadows_for_context(db, pid, gidx, limit=None)
            triplets = []
            for item in references:
                response_plan = []
                if item.get("hint"):
                    response_plan.append("待核对条件：" + item["hint"])
                blueprint = item.get("blueprint") or {}
                if isinstance(blueprint, dict) and blueprint:
                    response_plan.append(json.dumps(blueprint, ensure_ascii=False, allow_nan=False))
                triplets.append(CFPGTriplet(
                    cause=item["source_label"], foreshadow=item["description"],
                    payoff_goal="；".join(response_plan) or "未指定",
                    planned=item["status"] == "planned", evidence=item.get("source_evidence") or "",
                ))
            pack.foreshadow_triplets = triplets
            pack.foreshadow_debt_warning = ""
        except Exception as exc:
            logger.warning("Foreshadow source context unavailable (project=%s): %s", pid, type(exc).__name__)
            raise RequiredContextUnavailable("foreshadow_context_unavailable") from exc

        # Timeline anchors from chapter summaries with key events.
        # Ordered/filtered on the book-global index: the volume-local idx
        # interleaved every volume's chapters into a fake chronology from
        # volume 2 onward. Rows with NULL global_idx (pre-backfill) are
        # excluded rather than mis-ordered.
        try:
            timeline_result = await db.execute(
                select(Chapter.global_idx, current_summary_sql().label("summary"))
                .join(Volume, Chapter.volume_id == Volume.id)
                .where(
                    Volume.project_id == pid,
                    current_summary_sql().is_not(None),
                    Chapter.status == "completed",
                    Chapter.global_idx.isnot(None),
                    Chapter.global_idx < pack.history_before_global_idx,
                )
                .order_by(Chapter.global_idx.asc())
            )
            for row in timeline_result.all():
                if row.summary and len(row.summary) > 10:
                    anchor = TimeAnchor(
                        chapter_idx=row.global_idx,
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

        # Q3 v1.9.1: character cognition ledger -> 人物认知边界 section.
        try:
            from app.services import character_cognition as _cognition
            from app.config import settings as _settings

            pack.cognition_boundaries = await _cognition.load_prompt_context(
                db, pid, before_global_idx=gidx, before_chapter_id=pack.target_chapter_id,
                chapter_outline=pack.current_outline,
                chapter_text=pack.current_content, previous_text=pack.previous_chapter_text,
                priority_names=[name for card in pack.character_cards for name in (card.name, *card.aliases)],
                max_chars=int(getattr(_settings, "COGNITION_HISTORY_MAX_CHARS", 3000) or 3000),
                defer_retrieval=True,
            )
        except SQLAlchemyError:
            raise
        except RequiredContextUnavailable:
            raise
        except Exception as e:
            logger.warning("Failed to load character cognition ledger: %s", type(e).__name__)
            raise RequiredContextUnavailable("character_cognition_context_unavailable") from e

        # C2/F1 v1.9.2: whole-book style-tic mirror -> 文风镜像 section (L2 tail).
        try:
            from sqlalchemy import select as _select

            from app.models.project import StyleStat
            from app.services.style_stat import render_style_mirror_block

            row = (
                await db.execute(
                    _select(StyleStat.stats_json).where(StyleStat.project_id == pid)
                )
            ).first()
            if row and row[0]:
                pack.style_tic_mirror = render_style_mirror_block(row[0], max_chars=800)
        except Exception as e:
            logger.warning("Failed to load style stats: %s", e)

        # C3/F4: deterministic related-chapter recall + secondary-cast roster.
        try:
            from app.services.character_roster import render_roster_block
            from app.services.related_chapters import (
                find_related_chapters,
                render_recall_block,
            )

            related = await find_related_chapters(
                db, pid, None, gidx, pack.current_outline or {},
                historical_roster=historical_roster, foreshadow_references=references,
            )
            recall = render_recall_block(related, max_chars=600)

            roster_block = ""
            try:
                roster_block = render_roster_block(
                    historical_roster, gidx, max_chars=600
                )
            except Exception as e:
                logger.warning("Failed to render cast roster: %s", e)

            pack.related_chapter_recall = "\n\n".join(
                b for b in (recall, roster_block) if b
            )
        except Exception as e:
            logger.warning("Failed to build related-chapter recall: %s", e)

        # C4/F3: narrative compass direction anchor.
        try:
            from app.services.compass_service import load_compass, render_compass_anchor

            compass = await load_compass(db, pid)
            pack.compass_anchor = render_compass_anchor(
                compass, (global_chapter_idx or chapter_idx), max_chars=400
            )
        except Exception as e:
            logger.warning("Failed to load narrative compass: %s", e)

        # Tier-3 (细读层): returning-character drill-down. Deterministic
        # trigger over the capped card list — no LLM, silent absence.
        try:
            await self._build_returning_drilldown(pack, pid, gidx, historical_roster=historical_roster)
        except Exception as e:
            logger.warning("Failed to build returning-character drilldown: %s", e)

        # Build strand tracker from recent chapters (book-global window)
        await self._build_strand_tracker(pack, pid, gidx)

    async def _enrich_characters_from_neo4j(
        self,
        pack: ContextPack,
        project_id: str,
        chapter_idx: int,
    ) -> None:
        """Load verified graph state and independent relationship records.

        ``chapter_idx`` must be BOOK-GLOBAL: Neo4j chapter_start/chapter_end
        edges are written on the global axis by entity_tasks.
        Relationships have no source-bound PG projection fallback, so graph
        failure must stop generation rather than leave only old profiles.
        """
        try:
            from app.db.neo4j import get_neo4j

            driver = None
            async for d in get_neo4j():
                driver = d
                break

            if driver is None:
                raise RequiredContextUnavailable('character_graph_context_unavailable')

            from app.services.entity_context import verified_world_snapshot
            snapshot = await verified_world_snapshot(await self._get_db(), driver, project_id, chapter_idx)
            snapshots, relationships = snapshot.characters, snapshot.relationships

            # Stage graph assembly so a malformed graph record
            # cannot leave an apparently complete, partially mutated pack.
            from copy import deepcopy
            staged_cards = deepcopy(pack.character_cards)
            from app.services.character_locations import LOCATION_FIELDS

            def _has_known_position(card) -> bool:
                """这张卡是否带着任何位置观察(地点记录 / 状态观察 / 状态字段)。"""
                return bool(card.chapter_locations or card.chapter_location_states
                            or any(key in card.chapter_state for key in LOCATION_FIELDS))

            # 同名多卡 + 只有"状态里的位置":按名字分配状态会把两张卡混成一个人,
            # 这里必须报错。判定要直接查状态,不能只看已装配的卡
            #(状态叠加发生在装配之后)。
            from collections import Counter

            duplicate_names = sorted(name for name, count in
                                     Counter(card.name for card in staged_cards).items()
                                     if count > 1)
            if duplicate_names:
                from app.services.entity_context import verified_character_states

                duplicate_states = await verified_character_states(
                    await self._get_db(), project_id, chapter_idx, duplicate_names)
                for name, state in duplicate_states.items():
                    if any(key in (state.get("status") or {}) for key in LOCATION_FIELDS):
                        raise ValueError('ambiguous_character_location_owner')

            located_names = {card.name for card in staged_cards if _has_known_position(card)}
            located_names.update(row['name'] for row in
                                 (getattr(snapshot, "location_state_records", None) or []))
            seen_names = set()
            for card in staged_cards:
                # 同名两张卡、其中至少一张带着位置:无法判定位置属于谁 → 必须报错,
                # 不能把两张卡合并、也不能挑一个位置写进人物卡。
                if card.name in seen_names and card.name in located_names:
                    raise ValueError('ambiguous_character_location_owner')
                seen_names.add(card.name)
            card_by_name = {c.name: c for c in staged_cards}
            aliases = registered_alias_candidates(staged_cards)
            relationship_aliases = {}

            for snap in snapshots:
                card = card_by_name.setdefault(snap.name, CharacterCard(name=snap.name))
                card.apply_chapter_state(snap.status or {}, snap.field_sources)
            locations_by_name = {}
            for location in snapshot.location_records:
                locations_by_name.setdefault(location['name'], []).append(location)
            for name, locations in locations_by_name.items():
                card = card_by_name.setdefault(name, CharacterCard(name=name))
                card.apply_locations(locations)
            # 同章同字段的第二条位置观察(PG 与图谱各给一个、或图谱有两条)
            # 一直没被接进来:上面只 apply_locations(地点记录),
            # 而 location_state_records(状态里的"位置"字段观察)从未应用,
            # 于是同一章的两个地点会被静默丢掉一个。这里补上。
            state_locations_by_name: dict[str, list] = {}
            for row in getattr(snapshot, "location_state_records", None) or []:
                state_locations_by_name.setdefault(row.get("name"), []).append(row)
            for name, rows in state_locations_by_name.items():
                card = card_by_name.setdefault(name, CharacterCard(name=name))
                card.apply_location_states(rows)
            for rel in relationships:
                # A relationship can be the only observation about either
                # participant; it must not depend on a CharacterState row.
                render_relationship_record(rel)
                for name in (rel.source, rel.target):
                    if name in aliases:
                        relationship_aliases[name] = aliases[name]
                    else:
                        card_by_name.setdefault(name, CharacterCard(name=name))
            staged_relationships = deepcopy(relationships)
            pack.character_cards, pack.relationship_records, pack.relationship_aliases = (
                list(card_by_name.values()), staged_relationships, relationship_aliases)

        except (SQLAlchemyError, RequiredContextUnavailable):
            raise
        except Exception as e:
            logger.warning("Failed to load character graph context: %s", type(e).__name__)
            raise RequiredContextUnavailable('character_graph_context_unavailable') from e

    async def _overlay_character_states(
        self,
        pack: ContextPack,
        project_id: str,
        global_chapter_idx: int,
    ) -> None:
        """Retain every verified earlier state field and its actual source.

        The card reconciles PG/graph values by source chapter, with profiles
        as labelled references. A failed read is not an empty state history.
        """
        if not pack.character_cards:
            return
        try:
            from app.services.entity_context import verified_character_states
            states = await verified_character_states(await self._get_db(), project_id, global_chapter_idx,
                                                      [card.name for card in pack.character_cards])
            for card in pack.character_cards:
                state = states.get(card.name)
                if state:
                    card.apply_chapter_state(state['status'], state['field_sources'])
                    # 同章同字段的第二条位置观测也要接进来,否则同章两个地点
                    # 会被静默丢掉一个(折叠结果是 status 只留第一个值)。
                    state_rows = state.get('location_states') or []
                    if state_rows:
                        card.apply_location_states(state_rows)
        except SQLAlchemyError:
            raise
        except Exception as e:
            logger.warning("Failed to overlay character states: %s", type(e).__name__)
            raise RequiredContextUnavailable("character_states_context_unavailable") from e

    async def _cap_character_cards(
        self,
        pack: ContextPack,
        project_id: str,
        *, historical_roster: list[dict] | None = None,
    ) -> None:
        """W5: bound the full-card render to chapter-relevant characters.

        When the cast exceeds ``CONTEXT_PACK_MAX_CHARACTER_CARDS``:

        - relevance = the card name appears in the current chapter outline
          or the chapter's existing content (deterministic substring match,
          same spirit as ``_extract_entities_from_outline``);
        - frequently mentioned characters (in earlier completed prose)
          fill the remaining card slots;
        - the immediate predecessor's supplied prose is also relevant;
        - all relevant cards retain their state, even beyond the soft cap.
          Remaining space is filled by historical frequency. Prompt-budget
          overflow is explicit instead of silently replacing state by names.

        Casts within the cap are left untouched (output unchanged).
        """
        try:
            from app.config import settings

            max_cards = int(
                getattr(settings, "CONTEXT_PACK_MAX_CHARACTER_CARDS", 12)
            )
        except Exception:  # noqa: BLE001
            max_cards = 12
        cards = pack.character_cards
        if max_cards <= 0 or len(cards) <= max_cards:
            return

        # Relevance includes the supplied immediate-predecessor prose so
        # an unfinished handoff cannot lose its participant's state.
        try:
            outline_text = (
                json.dumps(pack.current_outline, ensure_ascii=False)
                if pack.current_outline
                else ""
            )
        except Exception:  # noqa: BLE001
            outline_text = str(pack.current_outline)
        relevance_text = "\n".join((outline_text, pack.current_content or "",
                                    pack.previous_chapter_text or ""))

        # Rank only by mentions in verified earlier prose, never future totals.
        if historical_roster is None:
            from app.services.character_roster import load_historical_roster
            historical_roster = await load_historical_roster(
                await self._get_db(), project_id, before_global_idx=pack.global_chapter_idx)
        counts = {row['character_name']: row['appearance_count'] for row in historical_roster}

        # Exact names take precedence over aliases in other profiles. Shared
        # remaining aliases retain every candidate, never a guessed identity.
        mentioned = {c.name for c in cards if c.name and c.name in relevance_text}
        for alias, candidates in registered_alias_candidates(cards).items():
            if alias in relevance_text:
                mentioned.update(candidates)
        # Deterministic rank: chapter-mentioned first, then appearance count
        # (protagonists), then original card order as the tiebreaker.
        ranked = sorted(
            range(len(cards)),
            key=lambda i: (
                0 if cards[i].name in mentioned else 1,
                -counts.get(cards[i].name, 0),
                i,
            ),
        )
        # An explicit participant must not lose location/injury/relationship
        # state just because another participant appeared earlier in the table.
        required_count = sum(card.name in mentioned for card in cards)
        kept = sorted(ranked[:max(max_cards, required_count)])
        pack.roster_extra_names = []
        pack.character_cards = [cards[i] for i in kept]
        selected_names = {card.name for card in pack.character_cards}
        selected_endpoints = selected_names | {
            alias for alias, candidates in pack.relationship_aliases.items()
            if selected_names.intersection(candidates)
        }
        pack.relationship_records = [rel for rel in pack.relationship_records
            if rel.source in selected_endpoints or rel.target in selected_endpoints]
        retained_endpoints = {name for rel in pack.relationship_records for name in (rel.source, rel.target)}
        pack.relationship_aliases = {alias: candidates for alias, candidates in pack.relationship_aliases.items()
                                     if alias in retained_endpoints}

    async def _build_returning_drilldown(
        self,
        pack: ContextPack,
        project_id: str,
        global_chapter_idx: int,
        *, historical_roster: list[dict] | None = None,
    ) -> None:
        """Tier-3 (细读层): 旧人重现 drill-down for long-absent characters.

        For each chapter-relevant character (the capped card list plus the
        name-only roster overflow), a roster ``last_seen_chapter`` more than
        ``MEMORY_DRILLDOWN_GAP_CHAPTERS`` chapters behind the current
        book-global index triggers a fetch of their memory cards
        (first_appearance + up to 2 most recent key_moments from earlier
        chapters) rendered into ``pack.returning_character_drilldown``.
        Deterministic, zero LLM; silently absent when nothing triggers.
        """
        names = [c.name for c in pack.character_cards if c.name]
        for n in pack.roster_extra_names:
            if n and n not in names:
                names.append(n)
        if not names:
            return
        try:
            from app.config import settings

            gap_chapters = int(
                getattr(settings, "MEMORY_DRILLDOWN_GAP_CHAPTERS", 20)
            )
        except Exception:  # noqa: BLE001
            gap_chapters = 20

        from app.models.project import CharacterMemoryCard

        db = await self._get_db()
        if historical_roster is None:
            from app.services.character_roster import load_historical_roster
            historical_roster = await load_historical_roster(
                db, project_id, before_global_idx=global_chapter_idx)
        returning: dict[str, int] = {}
        for row in historical_roster:
            name, last_seen = row['character_name'], row['last_seen_chapter']
            if name not in names:
                continue
            if name is None or last_seen is None:
                continue
            if is_returning_character(
                int(last_seen), int(global_chapter_idx), gap_chapters
            ):
                returning[name] = int(last_seen)
        if not returning:
            return

        card_rows = await db.execute(
            select(
                CharacterMemoryCard.character_name,
                CharacterMemoryCard.global_idx,
                CharacterMemoryCard.card_type,
                CharacterMemoryCard.excerpt,
                CharacterMemoryCard.source_chapter_id,
                CharacterMemoryCard.source_hash,
            ).where(
                CharacterMemoryCard.project_id == project_id,
                CharacterMemoryCard.character_name.in_(list(returning)),
                CharacterMemoryCard.global_idx < int(global_chapter_idx),
            )
        )
        by_name: dict[str, list[tuple[int, str, str]]] = {}
        from app.services.entity_sources import filter_current_entity_rows
        candidates = [dict(name=name, global_idx=gidx, card_type=kind, excerpt=excerpt,
                           source_chapter_id=sid, source_hash=shash)
                      for name, gidx, kind, excerpt, sid, shash in card_rows.all()]
        valid = await filter_current_entity_rows(db, project_id, candidates, before_global_idx=global_chapter_idx)
        for row in valid:
            name, gidx, card_type, excerpt = row['name'], row['global_idx'], row['card_type'], row['excerpt']
            if name and excerpt:
                by_name.setdefault(name, []).append(
                    (int(gidx), str(card_type), str(excerpt))
                )

        entries: list[dict] = []
        # Longest-absent first: they need the re-read anchor the most, and
        # the render cap drops tail sections first.
        for name in sorted(returning, key=lambda n: (returning[n], n)):
            cards = by_name.get(name) or []
            if not cards:
                continue
            ordered = sorted(cards, key=lambda c: (c[0], c[1] != "first_appearance", c[2]))
            # Old labels may contain several first_appearance cards. Keep
            # an early verified anchor and recent excerpts regardless of label.
            recent = sorted(ordered[1:], key=lambda c: (-c[0], c[1], c[2]))[:2]
            selected = [ordered[0]] + sorted(recent, key=lambda c: c[0])
            if selected:
                entries.append(
                    {
                        "name": name,
                        "last_seen": max(c[0] for c in cards),
                        "verified_anchor": True,
                        "cards": selected,
                    }
                )
        pack.returning_character_drilldown = render_drilldown_block(
            entries, int(global_chapter_idx), max_chars=1200
        )

    async def _build_strand_tracker(
        self,
        pack: ContextPack,
        project_id: str,
        chapter_idx: int,
    ) -> None:
        """Analyze recent chapters to track Quest/Fire/Constellation strands.

        ``chapter_idx`` must be BOOK-GLOBAL (the tracker queries and reports
        on ``Chapter.global_idx``).
        """
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
            await self._search_qdrant_snippets(pack, entities, pid)

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
        project_id: str,
    ) -> None:
        """Search chapter-summary memory for snippets relevant to the outline.

        Two-tier recall via qdrant_store.search_project_summaries (W10 fix):
        the project's live shard (legacy-global + project filter fallback
        pre-migration; compacted=true points excluded) merged with the
        compacted-rollup tier under a slight score penalty, capped at 5.
        Scoping to ``project_id`` is enforced by the shard itself or the
        payload filter — without it another project's chapter summaries
        could be injected as facts.
        """
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
            from app.services import qdrant_store as qs

            client = AsyncQdrantClient(
                host=getattr(settings, "QDRANT_HOST", "localhost"),
                port=getattr(settings, "QDRANT_PORT", 6333),
            )

            try:
                hits = await qs.search_project_summaries(
                    client,
                    str(project_id),
                    embedding,
                    limit=25,
                    score_threshold=0.4,
                    before_global_idx=pack.history_before_global_idx,
                )
                from app.services.vector_provenance import filter_current_memory
                history_db = await self._get_db()
                hits = await filter_current_memory(
                    history_db, project_id, hits,
                    before_global_idx=pack.history_before_global_idx,
                )
                for hit in hits[:5]:
                    payload = hit.get("payload") or {}
                    summary = payload.get("summary", payload.get("text", ""))
                    if summary:
                        if hit.get("tier") == "compacted":
                            summary = f"[压缩记忆] {summary}"
                        pack.rag_snippets.append(summary)

                # Tier-4 (自由检索层): one extra search against the
                # chapter-chunk shard with the SAME query embedding. The
                # current and future chapters are excluded by persisted global
                # index; unknown chronology is not admitted as history. Gated,
                # deterministic, silently absent on any failure.
                try:
                    if getattr(settings, "CHAPTER_CHUNK_RECALL_ENABLED", True):
                        chunk_hits = await qs.search_project_chunks(
                            client,
                            str(project_id),
                            embedding,
                            limit=15,
                            score_threshold=0.5,
                            before_global_idx=pack.history_before_global_idx,
                        )
                        chunk_hits = await filter_current_memory(
                            history_db, project_id, chunk_hits,
                            before_global_idx=pack.history_before_global_idx, chunks=True,
                        )
                        for hit in chunk_hits[:3]:
                            payload = hit.get("payload") or {}
                            text = payload.get("text") or ""
                            if not text:
                                continue
                            gidx = payload.get("global_idx")
                            label = f"[CH-{gidx}] " if gidx is not None else ""
                            pack.chunk_recall.append(
                                f"{label}{str(text)[:300]}"
                            )
                except Exception as chunk_exc:  # noqa: BLE001
                    logger.debug("chapter chunk recall skipped: %s", chunk_exc)
                # v0.6 — ContextPack v2: three-way recall from decompile collections
                # Gated on CONTEXT_PACK_V2_ENABLED env flag. Safe no-op if disabled
                # or if the project has no bound reference book.
                import os

                if os.getenv("CONTEXT_PACK_V2_ENABLED", "false").lower() in ("1", "true", "yes"):
                    try:
                        await self._v2_three_way_recall(
                            pack, embedding, client, str(project_id)
                        )
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
        original ``query_text`` is returned unchanged. The registry prompt
        (task_type=rag_query_rewrite) outputs ``{queries: [...], keywords:
        [...]}``; those are joined into one search string. Legacy shapes
        (``{"query"|"rewrite"|"text": "..."}`` or a plain string) are still
        accepted as fallback.
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
            # W9: actual prompt schema is {queries: [...], keywords: [...]}
            # — join both lists (deduped, order-preserving) into one search
            # string for embedding.
            parts: list[str] = []
            seen: set[str] = set()
            for key in ("queries", "keywords"):
                vals = out.get(key)
                if not isinstance(vals, list):
                    continue
                for v in vals:
                    if not isinstance(v, str):
                        continue
                    v = v.strip()
                    if v and v not in seen:
                        seen.add(v)
                        parts.append(v)
            if parts:
                return " ".join(parts)
            # Legacy fallback shapes.
            rewrote = out.get("query") or out.get("rewrite") or out.get("text")
            if isinstance(rewrote, str) and rewrote.strip():
                return rewrote.strip()
        if isinstance(out, str) and out.strip():
            return out.strip()
        return query_text

    async def _v2_three_way_recall(
        self, pack: ContextPack, embedding: list, client, project_id: str
    ) -> None:
        """v0.6 ContextPack v2: pull style_profiles + beat_sheets recall lines
        from the reference book bound to this project.

        The book id is resolved through the full binding chain (settings
        style_reference keys, then the resolved style profile's book binding).
        Without a resolved book id the recall is SKIPPED — an unfiltered
        search would mix style/beat lines from every ingested book.
        Recall lines land in ``pack.style_recall`` (own L3 render slot),
        never in rag_snippets where the [:5] cap truncated them away.
        """
        from app.services.qdrant_store import QdrantStore
        from app.services.style_runtime import get_dossier_block, resolve_reference_book_id

        db = await self._get_db()
        ref_book_id = await resolve_reference_book_id(db, project_id)
        if not ref_book_id:
            logger.debug(
                "v2 three-way recall skipped: no reference book resolved for project %s",
                project_id,
            )
            return

        # Dossier preference: a consolidated style_block on the reference book
        # replaces the vector style recall (it is the authoritative digest).
        dossier_style_block = ""
        try:
            from app.models.project import ReferenceBook

            book = await db.get(ReferenceBook, ref_book_id)
            if book is not None:
                dossier_style_block = get_dossier_block(book, "style_block")
        except Exception:
            dossier_style_block = ""

        store = QdrantStore(client)
        if dossier_style_block:
            pack.style_recall.append(dossier_style_block)
        else:
            # style_profiles: structured style prompts (top 3)
            style_hits = await store.search_style_profiles(
                embedding, book_id=ref_book_id, top_k=3
            )
            for h in style_hits:
                prof = (h.get("payload") or {}).get("profile") or {}
                if prof:
                    line = (
                        f"[风格] pov={prof.get('pov','?')} 节奏={prof.get('sentence_rhythm','?')} "
                        f"情感={prof.get('emotional_register','?')} "
                        f"词汇={','.join(prof.get('vocab_tone') or [])}"
                    )
                    pack.style_recall.append(line)
        # beat_sheets: entity-redacted plot scaffolds (top 2)
        beat_hits = await store.search_beat_sheets(embedding, book_id=ref_book_id, top_k=2)
        for h in beat_hits:
            beat = (h.get("payload") or {}).get("beat") or {}
            if beat:
                line = (
                    f"[骨架] {beat.get('scene_type','?')}: {beat.get('reusable_pattern','?')} "
                    f"→ {beat.get('outcome','?')}"
                )
                pack.style_recall.append(line)
        # PR-NO-RAW-INJECT (2026-05-05): style_samples_redacted disabled.
        # Injecting reference-book passages (even after entity redaction) is plagiarism territory.
        # Only abstract style descriptors above are kept.

        # PR-NO-RAW-INJECT (2026-05-05): scene_samples passage injection removed.
        # Vector store still indexed for offline analysis but never injected to prompt.

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
                settings_json.get("style_profile_id")
                or style_ref.get("profile_id")
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
        Returns one complete profile block, or no block when the profile is empty.
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

        # PR-NO-RAW-INJECT (2026-05-05): sample_passages block removed.
        # Style is learned via abstract rule_lines / dosage_profile / tone_keywords above.

        # Reference statistics describe a sample, not quotas for this chapter.
        from app.services.style_reference_observations import render_observations
        config = getattr(profile, "config_json", None) or {}
        if isinstance(config, dict):
            observed = render_observations(config.get("dosage_profile"))
            if observed:
                parts.append(observed)

        # These sections belong to one selected profile. Keep them together so
        # the ContextPack sample-count cap cannot silently discard section 4.
        return ["\n\n".join(parts)] if parts else []

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

        Describe variations with sample counts; average sensory values only over
        valid observations of each dimension. Reference cards do not set chapter rules.
        """
        try:
            from app.models.decompile import StyleProfileCard

            stmt = (
                select(StyleProfileCard)
                .where(StyleProfileCard.book_id == str(book_id))
                .order_by(StyleProfileCard.created_at.asc(), StyleProfileCard.id.asc())
                .limit(top_k)
            )
            result = await db.execute(stmt)
            cards = list(result.scalars().all())
            if not cards:
                return []

            from app.services.style_card_observations import render_style_cards
            return render_style_cards(c.profile_json for c in cards)
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
