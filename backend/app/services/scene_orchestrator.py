"""Scene-staged chapter writing orchestrator (v1.5.0 C1).

Replaces the single-shot "generation" prompt with a two-stage pipeline:

1. ``scene_planner`` (standard tier, structured JSON):
   chapter outline + context pack -> list of 3-6 SceneBrief.
2. ``scene_writer`` (flagship tier, streaming):
   per-scene, takes the SceneBrief + a rolling summary of already-written
   scenes -> 800-1200 char prose.

The orchestrator joins per-scene streams into one continuous chunk stream
so the existing SSE infrastructure (api/generate.py event_stream + auto-save)
works unchanged. A ``\n\n`` separator is yielded between scenes.

Design notes
------------
- Scene boundaries (titles / metadata) are NOT yielded as text. The caller
  can subscribe to the on-scene-start hook by passing ``on_scene_start``.
- ``plan_scenes`` is robust to imperfect JSON output: it tries strict JSON
  parse first, then ```json fenced parse, then a fallback heuristic that
  builds N=ceil(target_words/1000) generic SceneBriefs from the chapter
  outline so the chapter never silently fails to scene-mode produce text.
- Rolling "prior scenes summary" is intentionally cheap: we keep the last
  ~600 chars of each completed scene plus its title/key_action so writer
  has continuity context without exploding the prompt.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.narrative_contract import (
    SCENE_CONTRACT_FIELDS_PROMPT,
    WRITER_CONTRACT_PROMPT,
)
from app.services.prompt_registry import run_text_prompt, stream_text_prompt

logger = logging.getLogger(__name__)


DEFAULT_TARGET_WORDS = 3500
MIN_SCENE_WORDS = 800
MAX_SCENE_WORDS = 1200


@dataclass
class SceneBrief:
    idx: int
    title: str
    brief: str
    pov: str = ""
    location: str = ""
    time_cue: str = ""
    key_action: str = ""
    target_words: int = 1000
    hook: str = ""
    start_state: str = ""
    time_delta: str = ""
    location_path: str = ""
    entity_transfers: str = ""
    power_resource_map: str = ""
    information_state: str = ""
    mechanism_limits: str = ""
    result_strength: str = ""
    transition_bridge: str = ""
    continuity_ledger: str = ""

    @classmethod
    def from_dict(cls, idx: int, raw: dict) -> "SceneBrief":
        def _s(key: str, default: str = "") -> str:
            v = raw.get(key)
            return str(v).strip() if v is not None else default

        target_words = raw.get("target_words")
        try:
            target_words = int(target_words) if target_words is not None else 1000
        except (TypeError, ValueError):
            target_words = 1000
        target_words = max(MIN_SCENE_WORDS, min(MAX_SCENE_WORDS, target_words))

        # idx may be supplied by the model; we override with the canonical
        # 1-based positional idx so concatenation order is deterministic.
        return cls(
            idx=idx,
            title=_s("title") or f"场景 {idx}",
            brief=_s("brief"),
            pov=_s("pov"),
            location=_s("location"),
            time_cue=_s("time_cue"),
            key_action=_s("key_action"),
            target_words=target_words,
            hook=_s("hook"),
            start_state=_s("start_state"),
            time_delta=_s("time_delta"),
            location_path=_s("location_path"),
            entity_transfers=_s("entity_transfers"),
            power_resource_map=_s("power_resource_map"),
            information_state=_s("information_state"),
            mechanism_limits=_s("mechanism_limits"),
            result_strength=_s("result_strength"),
            transition_bridge=_s("transition_bridge") or _s("handoff_to_next"),
            continuity_ledger=(
                _s("continuity_ledger")
                or _s("ledger")
                or _s("连续性台账")
            ),
        )

    def to_writer_user_content(self) -> str:
        bullets: list[str] = []
        bullets.append(f"【场景号】第 {self.idx} 场")
        if self.title:
            bullets.append(f"【标题】{self.title}")
        if self.pov:
            bullets.append(f"【视角】{self.pov}")
        if self.location:
            bullets.append(f"【地点】{self.location}")
        if self.time_cue:
            bullets.append(f"【时间】{self.time_cue}")
        if self.key_action:
            bullets.append(f"【主要动作】{self.key_action}")
        contract_fields = [
            ("开场承接", self.start_state),
            ("时间成本", self.time_delta),
            ("空间路径", self.location_path),
            ("实体转移", self.entity_transfers),
            ("权力/资源图", self.power_resource_map),
            ("信息状态", self.information_state),
            ("机制边界", self.mechanism_limits),
            ("允许结果强度", self.result_strength),
            ("下场交接", self.transition_bridge),
            ("连续性台账", self.continuity_ledger),
        ]
        for label, value in contract_fields:
            if value:
                bullets.append(f"【{label}】{value}")
        bullets.append(f"【目标字数】约 {self.target_words} 字 (800-1200)")
        if self.hook:
            bullets.append(f"【场末过渡】{self.hook}")
        else:
            bullets.append("【场末过渡】（本场为末场，需送入本章钩子，但不干预下一章）")
        if self.brief:
            bullets.append("【场景 brief】")
            bullets.append(self.brief)
        return "\n".join(bullets)


# ---------------------------------------------------------------------------
# Planner: chapter outline -> List[SceneBrief]
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ARRAY_RE = re.compile(r"\[\s*\{.*\}\s*\]", re.DOTALL)


def _try_parse_scene_array(raw: str) -> list[dict] | None:
    """Best-effort parse of an LLM response into a list[dict] of scene briefs.

    Strategies, in order:
      1. strict json.loads on the whole string
      2. extract first ```json ... ``` fenced block
      3. extract first [...] array regex match
    Returns None if all strategies fail.
    """
    if not raw:
        return None
    candidates: list[str] = [raw.strip()]
    fence_match = _FENCE_RE.search(raw)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    array_match = _ARRAY_RE.search(raw)
    if array_match:
        candidates.append(array_match.group(0).strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("scenes"), list):
            return parsed["scenes"]
    return None



def _x4_inc_fallback(reason: str) -> None:
    """v1.6.0 X4: increment scene_plan_fallback_total. Best-effort."""
    try:
        from app.observability.metrics import SCENE_PLAN_FALLBACK_TOTAL
        SCENE_PLAN_FALLBACK_TOTAL.labels(reason=reason).inc()
    except Exception:
        pass


def _x4_observe_scene_count(n: int) -> None:
    """v1.6.0 X4: histogram observe scene count per chapter. Best-effort."""
    try:
        from app.observability.metrics import SCENE_COUNT_PER_CHAPTER
        SCENE_COUNT_PER_CHAPTER.observe(n)
    except Exception:
        pass


def _fallback_scene_briefs(target_words: int, chapter_outline_text: str) -> list[SceneBrief]:
    """Build deterministic scene briefs when the planner LLM fails to JSON.

    We pick N = round(target_words / 1000), clamp to [3, 6], then split the
    chapter outline text into N roughly equal slices to seed the briefs so
    the writer still has *some* structural anchor per scene.
    """
    n = max(3, min(6, round(max(target_words, MIN_SCENE_WORDS) / 1000)))
    text = (chapter_outline_text or "").strip()
    if text:
        chunk_size = max(1, len(text) // n)
        slices = [text[i * chunk_size : (i + 1) * chunk_size] for i in range(n)]
        slices[-1] = text[(n - 1) * chunk_size :]  # tail gets remainder
    else:
        slices = [""] * n
    per_scene = max(MIN_SCENE_WORDS, min(MAX_SCENE_WORDS, target_words // n))
    briefs: list[SceneBrief] = []
    for i, sl in enumerate(slices, start=1):
        briefs.append(
            SceneBrief(
                idx=i,
                title=f"场景 {i}",
                brief=sl[:200],
                pov="",
                location="",
                time_cue="",
                key_action="",
                target_words=per_scene,
                hook="" if i == n else "接下一场",
                start_state="首场承接章节开局" if i == 1 else "承接上一场末尾状态",
                time_delta="按本章大纲推导，不得跳过关键时间成本",
                location_path="按本章大纲推导关键实体移动路径",
                entity_transfers="列明本场关键人物/物件/信息/资源如何到场",
                power_resource_map="按当前题材推导冲突双方权力与资源差",
                information_state="只允许角色使用已获得且可信的信息",
                mechanism_limits="任何改变局势的机制必须有成本和边界",
                result_strength="支撑不足时降级为疑点/局部胜利/暂缓/后续线索",
                transition_bridge="本场末尾交代交给下一场的状态" if i != n else "本章钩子必须由前文因果触发",
                continuity_ledger="人物/物件/消息/证据/资源：场初承接上一场台账 -> 场末写清位置、持有人、知情人、转移路径和代价；新增实体必须写来源",
            )
        )
    return briefs


_REQUIRED_CONTRACT_FIELDS: tuple[str, ...] = (
    "start_state",
    "time_delta",
    "location_path",
    "entity_transfers",
    "power_resource_map",
    "information_state",
    "mechanism_limits",
    "result_strength",
    "transition_bridge",
    "continuity_ledger",
)


def _missing_contract_fields(brief: SceneBrief) -> list[str]:
    """Return missing generic continuity-contract fields for a planned scene."""
    missing: list[str] = []
    for field in _REQUIRED_CONTRACT_FIELDS:
        if not str(getattr(brief, field, "") or "").strip():
            missing.append(field)
    return missing


def _has_valid_scene_contract(briefs: list[SceneBrief]) -> bool:
    """Reject planner output that lacks generic time/space/entity ledgers.

    This is intentionally genre-agnostic: it checks abstract contract fields,
    never a specific book, trope, role, location, prop, or plot symptom.
    """
    if not briefs:
        return False
    return all(not _missing_contract_fields(brief) for brief in briefs)


class SceneOrchestrator:
    """Two-stage chapter writer: planner -> per-scene streaming writer."""

    def __init__(self, *, default_target_words: int = DEFAULT_TARGET_WORDS) -> None:
        self.default_target_words = default_target_words

    # ----- planner ---------------------------------------------------------

    async def plan_scenes(
        self,
        *,
        pack: ContextPack,
        db: AsyncSession,
        project_id: str | UUID,
        chapter_id: Optional[str | UUID],
        target_words: int,
        n_scenes_hint: Optional[int] = None,
        user_instruction: str = "",
    ) -> list[SceneBrief]:
        """Run scene_planner LLM call and return SceneBrief list (>=3, <=6)."""
        # Re-use ContextPack's system prompt as planner background, then
        # inject the planner-specific user instruction.
        background = pack.to_system_prompt()
        chapter_outline_text = self._extract_chapter_outline_text(pack)
        hint_line = (
            f"推荐场景数 = {n_scenes_hint}\n"
            if isinstance(n_scenes_hint, int) and 3 <= n_scenes_hint <= 6
            else ""
        )
        instr_block = (
            f"【额外用户指令（改写要求）】\n{user_instruction.strip()}\n\n"
            if user_instruction and user_instruction.strip()
            else ""
        )
        # PR-GEN-REVISE-DEDUP: hard mutex constraint on scene boundaries.
        # Without this block scene_planner happily emits the same key_action
        # (e.g. one meeting / one verification / one entry-into-hall) across
        # two adjacent scenes, which then makes scene_writer narrate that
        # event twice. Observed on chixin ch8/10/12 (batch 5/9-5/10).
        mutex_block = (
            "【场景互斥硬约束（最高优先级，违反即失败）】\n"
            "- 各场景的 location / time_cue / key_action 必须互不重复；"
            "同一情节（一次会面、一次验证、一次入廊、一次表态）"
            "只允许在 1 个 scene 中推进，禁止跨 scene 复述或回拨。\n"
            "- 当某事件在 scene N 已结束，scene N+1 起不得再写「再次进入」「重新开始」"
            "「回到刚才」「补做一遍」等动作；只能向前推进到下一阶段。\n"
            "- 若同一线索需分阶段呈现，必须以可辨别的状态切片（前置铺垫 / 当下推进 / 后续余响），"
            "且 brief 中不得出现与前一 scene 相同的「动词 + 受事」组合。\n\n"
        )
        contract_block = SCENE_CONTRACT_FIELDS_PROMPT + "\n"
        bridge_block = (
            "【章节过桥硬约束】\n"
            "- scene 1 的 start_state 必须显式承接本章大纲 start_state / transition_bridge / 上章末尾状态；如果上章末尾有人物、物件、消息或风险停在别处，必须先写清到场路径、交接人、见证者、时间成本。\n"
            "- 每个 scene 的 transition_bridge 必须把本场末状态完整交给下一场；禁止用省略句、抽象钩子或‘已经到了/转眼/不多时’跳过关键过桥。\n"
            "- 若角色在本场使用信息，brief 必须能追溯信息来源；没有来源时只能写疑点、误判或局部尝试，不能写强结论。\n"
            "- 每个 scene 必须输出 continuity_ledger 字段，结构为：人物/物件/消息/证据/资源：场初状态 -> 场末状态；新增实体必须写来源、入场路径、知情人和代价。\n"
            "- continuity_ledger 与 start_state / entity_transfers / transition_bridge 必须互相一致；不一致时不得输出该规划。\n\n"
        )
        user_content = (
            f"{instr_block}"
            f"{contract_block}"
            f"{bridge_block}"
            f"{mutex_block}"
            f"本章目标字数：约 {target_words} 字\n"
            f"{hint_line}"
            f"请按系统提示输出严格 JSON；每个 scene 必须包含场景合同字段，字段缺失视为规划失败。"
        )
        try:
            result = await run_text_prompt(
                task_type="scene_planner",
                user_content=user_content,
                db=db,
                extra_system=background,
                project_id=str(project_id),
                chapter_id=str(chapter_id) if chapter_id else None,
                rag_hits=[],
            )
            raw_text = getattr(result, "text", "") or ""
        except Exception as exc:  # broad: planner is best-effort, never block writer
            logger.warning("scene_planner LLM call failed: %s", exc)
            raw_text = ""

        parsed = _try_parse_scene_array(raw_text)
        if not parsed:
            logger.warning(
                "scene_planner returned unparseable output (len=%d); using fallback",  # v1.6.0 X4 metric: planner fallback
                len(raw_text),
            )
            return _fallback_scene_briefs(target_words, chapter_outline_text)

        briefs: list[SceneBrief] = []
        for i, raw in enumerate(parsed[:6], start=1):
            if not isinstance(raw, dict):
                continue
            briefs.append(SceneBrief.from_dict(i, raw))
        if len(briefs) < 3:
            logger.warning(
                "scene_planner returned %d briefs (<3); using fallback instead",
                len(briefs),
            )
            _x4_inc_fallback("too_few")
            return _fallback_scene_briefs(target_words, chapter_outline_text)
        if not _has_valid_scene_contract(briefs):
            missing_by_scene = {
                brief.idx: _missing_contract_fields(brief)
                for brief in briefs
                if _missing_contract_fields(brief)
            }
            logger.warning(
                "scene_planner returned briefs with missing continuity contract fields: %s; using fallback instead",
                missing_by_scene,
            )
            _x4_inc_fallback("missing_contract_fields")
            return _fallback_scene_briefs(target_words, chapter_outline_text)
        return briefs

    @staticmethod
    def _extract_chapter_outline_text(pack: ContextPack) -> str:
        """Best-effort: pull chapter outline / volume context from the pack."""
        # ContextPack stringifies its own to_system_prompt(); we just take a
        # bounded slice as the fallback seed text.
        try:
            sys_prompt = pack.to_system_prompt()
        except Exception:
            return ""
        return sys_prompt[-2000:] if sys_prompt else ""

    # ----- writer ----------------------------------------------------------

    async def write_scene_stream(
        self,
        *,
        scene: SceneBrief,
        pack: ContextPack,
        prior_scenes_summary: str,
        db: AsyncSession,
        project_id: str | UUID,
        chapter_id: Optional[str | UUID],
        user_instruction: str = "",
    ) -> AsyncIterator[str]:
        """Stream prose for a single scene through scene_writer."""
        background = pack.to_system_prompt()
        ctx_block = scene.to_writer_user_content()
        # PR-GEN-REVISE-DEDUP: prior_block becomes a hard "do not redo"
        # constraint instead of mere context. Pre-fix observation: scene_writer
        # treated the summary as background, then redid the prior scene's
        # action under a different framing (chixin ch8 verify-twice, ch10
        # meeting-thrice, ch12 verify-then-rewind).
        prior_block = (
            f"\n\n【已发生场景（禁止重写、禁止回拨、禁止再演）】\n"
            f"以下凝缩中描述的动作、对白、地点切换、状态变化均已在前序场景完成："
            f"本场禁止重写、禁止改述、禁止让人物再次进入这些动作或场景，"
            f"只能从凝缩末尾的状态向前推进到本场的 key_action。\n"
            f"{prior_scenes_summary}"
            if prior_scenes_summary
            else "\n\n【已写场景】本场为本章首场，请从本章开场起手。"
        )
        instr_block = (
            f"\n\n【额外用户指令（改写要求）】\n{user_instruction.strip()}"
            if user_instruction and user_instruction.strip()
            else ""
        )
        contract_block = "\n\n" + WRITER_CONTRACT_PROMPT
        ledger_block = (
            "\n\n【连续性台账写作硬约束】\n"
            f"- 本场结构化台账：{scene.continuity_ledger or '必须从场景合同自行补齐连续性台账'}\n"
            "- 写正文前先依据本场合同在心中核对人物、物件、消息、证据、资源的场初状态。\n"
            "- 正文中任何到场、离场、换手、被看见、被误解、被封存、被消耗，都必须有可读路径或代价。\n"
            "- 不能为增强压迫感临时增加人手/道具/能力；需要新增时必须在正文出现入场路径和见证信息。\n"
            "- 场末只允许留下 transition_bridge 台账能解释的结果；支撑不足时降级为疑点或待验证线索。"
        )
        user_content = ctx_block + contract_block + ledger_block + prior_block + instr_block + "\n\n请开始写本场景。"
        async for chunk in stream_text_prompt(
            task_type="scene_writer",
            user_content=user_content,
            db=db,
            extra_system=background,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            rag_hits=[],
        ):
            yield chunk

    # ----- end-to-end ------------------------------------------------------

    async def orchestrate_chapter_stream(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession,
        chapter_id: Optional[str | UUID] = None,
        user_instruction: str = "",
        target_words: Optional[int] = None,
        n_scenes_hint: Optional[int] = None,
        on_scene_start: Optional[Callable[[SceneBrief], Awaitable[None]]] = None,
    ) -> AsyncIterator[str]:
        """Build pack, plan scenes, then stream all scenes back-to-back.

        ``on_scene_start`` (if given) is awaited just before each scene's
        first chunk is emitted, with the SceneBrief as argument.
        """
        pack = await ContextPackBuilder(db=db).build(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
        )
        twords = target_words or self.default_target_words
        briefs = await self.plan_scenes(
            pack=pack,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
            target_words=twords,
            n_scenes_hint=n_scenes_hint,
            user_instruction=user_instruction,
        )
        _x4_observe_scene_count(len(briefs))  # v1.6.0 X4 metric: scene count per chapter
        prior_summary_parts: list[str] = []
        for i, scene in enumerate(briefs):
            if on_scene_start is not None:
                try:
                    await on_scene_start(scene)
                except Exception as cb_err:
                    logger.warning("on_scene_start callback failed: %s", cb_err)
            scene_text_parts: list[str] = []
            if i > 0:
                # visible separator between scenes (kept minimal)
                yield "\n\n"
            async for chunk in self.write_scene_stream(
                scene=scene,
                pack=pack,
                prior_scenes_summary="\n\n".join(prior_summary_parts),
                db=db,
                project_id=project_id,
                chapter_id=chapter_id,
                user_instruction=user_instruction,
            ):
                if chunk:
                    scene_text_parts.append(chunk)
                yield chunk
            full_scene_text = "".join(scene_text_parts)
            prior_summary_parts.append(self._summarize_scene(scene, full_scene_text))

    @staticmethod
    def _summarize_scene(scene: SceneBrief, scene_text: str) -> str:
        """Cheap rolling summary: title + key_action + last 600 chars of prose."""
        tail = (scene_text or "").strip()
        if len(tail) > 600:
            tail = tail[-600:]
        head_line = f"[场 {scene.idx} | {scene.title}]"
        if scene.key_action:
            head_line += f" 主要动作：{scene.key_action}"
        if scene.transition_bridge:
            head_line += f" 交接：{scene.transition_bridge}"
        elif scene.result_strength:
            head_line += f" 结果强度：{scene.result_strength}"
        if not tail:
            return head_line
        return f"{head_line}\n末段：{tail}"
