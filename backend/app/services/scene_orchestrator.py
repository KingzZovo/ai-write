"""Scene-staged chapter writing orchestrator (v1.5.0 C1).

Replaces the single-shot "generation" prompt with a two-stage pipeline:

1. ``scene_planner`` (standard tier, structured JSON):
   chapter outline + context pack -> list of 1-20 SceneBrief.
2. ``scene_writer`` (flagship tier, streaming):
   per-scene, takes the SceneBrief + the complete already-written scenes.

The orchestrator joins per-scene streams into one continuous chunk stream
so the existing SSE infrastructure (api/generate.py event_stream + auto-save)
works unchanged. A ``\n\n`` separator is yielded between scenes.

Design notes
------------
- Scene boundaries (titles / metadata) are NOT yielded as text. The caller
  can subscribe to the on-scene-start hook by passing ``on_scene_start``.
- ``plan_scenes`` uses the Registry structured parser and bounded retries.
  Invalid planning fails by default. Explicit emergency template fallback
  uses only the complete current chapter outline, never arbitrary context.
- Prior-scene context preserves actual prose, not planned outcomes or a tail
  excerpt. This costs more input tokens but retains early facts within a chapter.
"""
from __future__ import annotations

from contextlib import aclosing

import json
import logging
import re
from typing import AsyncIterator, Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.generation_context import generation_context_session
from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.narrative_contract import (
    SCENE_CONTRACT_FIELDS_PROMPT,
    WRITER_CONTRACT_PROMPT,
)
from app.services.money_plan import reconcile_money_totals
from app.services.outline_readiness import build_outline_readiness_report
from app.services.outline_contract import render_contract_value
from app.services.prompt_registry import run_text_prompt, stream_text_prompt

# Compatibility exports remain here for existing scene callers.
from app.services.scene_plan_contract import (
    DEFAULT_TARGET_WORDS, MIN_SCENE_WORDS, MAX_SCENE_WORDS, MAX_SCENE_COUNT,
    SceneBrief, ScenePlanValidationError, scene_budget_bounds, validate_scene_plan,
    recommended_scene_count,
    _REQUIRED_CONTRACT_FIELDS, _CONDITIONAL_CONTRACT_FIELDS,
    _cap_parsed_scenes, _missing_contract_fields, _has_valid_scene_contract,
    _scene_budget_reaches_target,
)

logger = logging.getLogger(__name__)


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


def _scene_count_for_target(target_words: int) -> int:
    target = max(int(target_words or DEFAULT_TARGET_WORDS), MIN_SCENE_WORDS)
    return max(1, min(MAX_SCENE_COUNT, round(target / 1000)))


def _scene_continuation_enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "SCENE_LENGTH_CONTINUATION_ENABLED", True))
    except Exception:  # noqa: BLE001
        return True


def _scene_continuation_max() -> int:
    try:
        from app.config import settings

        return max(0, int(getattr(settings, "SCENE_LENGTH_CONTINUATION_MAX", 1) or 0))
    except Exception:  # noqa: BLE001
        return 1


def _scene_min_words(target_words: int) -> int:
    """本场至少要写到多少字才算够(默认 95%,2026-11-17 由 85% 上调)。"""
    try:
        from app.config import settings

        ratio = float(getattr(settings, "SCENE_LENGTH_MIN_RATIO", 0.95) or 0)
    except Exception:  # noqa: BLE001
        ratio = 0.95
    ratio = max(0.0, min(1.0, ratio))
    return max(1, int(int(target_words or 0) * ratio))


def _planning_target(target_words: int) -> int:
    """送给规划器/写手的"章目标"(放大系数 SCENE_TARGET_OVERALLOC)。

    2026-11-18 实测(gemini-3.1-pro @ 目标 2000 字,100 章,`continuity.json` 的
    word_counts):中位 **1952 字(比 0.976)**、均值 1944;92 章里 55 章 <2000。
    去掉空白字符后中位 1854(中文"字数"习惯不含空白,两个口径都记着)。
    即每场系统性欠 2~3%,场级续写阈值(0.95)触发一次也补不回这点零头,
    所以在**送模的目标**上放大 8%,让模型按放大值写、欠完仍落在验收线以上。

    注意:章节记录里的真实目标、验收口径、统计口径都不变,只有本函数返回的
    数字进入提示词与规划器预算窗。系数为 1.0 时完全等价于旧行为。
    """
    try:
        from app.config import settings

        factor = float(getattr(settings, "SCENE_TARGET_OVERALLOC", 1.10) or 1.0)
    except Exception:  # noqa: BLE001 - 配置读不到就按旧行为
        factor = 1.0
    factor = max(1.0, min(1.5, factor))
    base = max(int(target_words or 0), 0)
    if base <= 0 or factor == 1.0:
        return base
    return int(round(base * factor))




def _chapter_min_words(target_words: int) -> int:
    """整章交付下限(与质量门同口径:CHAPTER_MIN_TARGET_WORD_RATIO)。"""
    try:
        from app.config import settings

        ratio = float(getattr(settings, "CHAPTER_MIN_TARGET_WORD_RATIO", 0.95) or 0)
    except Exception:  # noqa: BLE001
        ratio = 0.95
    ratio = max(0.0, min(1.0, ratio))
    return max(1, int(int(target_words or 0) * ratio))



def _chapter_topup_threshold(target_words: int) -> int:
    """章级补足的**停止线**(默认 = 真实章目标 × 1.0,即"补到够 2000 字")。

    与 ``_chapter_min_words``(验收线,0.95)分开:验收线管"能不能算交付通过",
    补足线管"还要不要再写"。2026-11-18 实测把补足线设在 0.95 时,1900~2000 区间
    的章(如 1969 字)不会被补,用户要求的"起码 2000"仍不满足。
    """
    try:
        from app.config import settings

        ratio = float(getattr(settings, "CHAPTER_LENGTH_TOPUP_RATIO", 1.0) or 0)
    except Exception:  # noqa: BLE001
        ratio = 1.0
    ratio = max(0.0, min(1.5, ratio))
    return max(1, int(round(int(target_words or 0) * ratio)))

def _chapter_continuation_max() -> int:
    try:
        from app.config import settings

        return max(0, int(getattr(settings, "CHAPTER_LENGTH_CONTINUATION_MAX", 1) or 0))
    except Exception:  # noqa: BLE001
        return 1
def _fallback_scene_briefs(target_words: int, chapter_outline_text: str) -> list[SceneBrief]:
    """Build deterministic scene briefs when the planner LLM fails to JSON.

    We pick N = round(target_words / 1000), clamp to [1, MAX_SCENE_COUNT],
    then split the
    chapter outline text into N roughly equal slices to seed the briefs so
    the writer still has *some* structural anchor per scene.
    """
    n = _scene_count_for_target(target_words)
    text = (chapter_outline_text or "").strip()
    if text:
        chunk_size = max(1, len(text) // n)
        slices = [text[i * chunk_size : (i + 1) * chunk_size] for i in range(n)]
        slices[-1] = text[(n - 1) * chunk_size :]  # tail gets remainder
    else:
        raise RuntimeError("scene_planner_failed: missing_current_outline_for_fallback")
    per_scene = max(MIN_SCENE_WORDS, min(MAX_SCENE_WORDS, target_words // n))
    briefs: list[SceneBrief] = []
    for i, sl in enumerate(slices, start=1):
        briefs.append(
            SceneBrief(
                idx=i,
                title=f"场景 {i}",
                brief=sl,
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
                power_resource_map="按作者要求记录实际权限、资源与限制；无冲突可写不涉及",
                information_state="只允许角色使用已获得且可信的信息",
                mechanism_limits="遵守已经建立的机制条件；普通行动不另设成本或反制",
                result_strength="按作者要求区分本场实际完成、确认与留待以后的结果",
                transition_bridge="本场末尾交代交给下一场的状态" if i != n else "按本章要求自然收束，不强加钩子",
                continuity_ledger="人物/物件/消息/证据/资源：场初已发生状态 -> 本场计划变化；关键变化须有依据，不把规划当事实",
                action_budget="有时间、身体或空间限制时记录实际限制；无关项写不涉及，不人为限制动作次数",
                inference_ledger="关键判断列明感知来源/证据、结论强度、替代解释和允许写法；弱证据只能推出疑点",
            )
        )
    return briefs


def _declared_time_cue(brief: "SceneBrief", pack: ContextPack) -> str:
    """本章声明的时间推进:场景简报优先,其次本章大纲里的时间字段/梗概。

    只取"已经写明的说法"(次日清晨/一周后);兜底简报的占位话术不算声明。
    """
    from app.services.time_pins import extract_time_cue

    sources: list[object] = [getattr(brief, "time_cue", ""), getattr(brief, "time_delta", "")]
    outline = getattr(pack, "current_outline", None)
    if isinstance(outline, dict):
        for key in ("time", "时间", "时间线", "梗概", "summary", "synopsis", "brief", "content"):
            sources.append(outline.get(key))
    elif isinstance(outline, str):
        sources.append(outline)
    return extract_time_cue(*sources)


class SceneOrchestrator:
    """Two-stage chapter writer: planner -> per-scene streaming writer."""

    def __init__(self, *, default_target_words: int = DEFAULT_TARGET_WORDS) -> None:
        self.default_target_words = default_target_words

    # ----- planner ---------------------------------------------------------

    async def plan_scenes(
        self,
        *,
        pack: ContextPack,
        db: AsyncSession | None,
        project_id: str | UUID,
        chapter_id: Optional[str | UUID],
        target_words: int,
        n_scenes_hint: Optional[int] = None,
        user_instruction: str = "",
    ) -> list[SceneBrief]:
        """Run scene_planner LLM call and return SceneBrief list (1..MAX_SCENE_COUNT)."""
        try:
            budget_lower, budget_upper = scene_budget_bounds(target_words)
        except ScenePlanValidationError as exc:
            raise RuntimeError(f"scene_planner_failed: {exc}") from exc
        # Root-cause guard (2026-05): do NOT proceed with chapter writing when
        # scene_planner fails. Template fallback briefs are known to trigger
        # continuity/time/space/information violations and cause quality collapse.
        # Operators can override in emergencies via:
        #   ALLOW_SCENE_PLANNER_FALLBACK=1
        # Planner timeout can be tuned via:
        #   SCENE_PLANNER_TIMEOUT_SECONDS=<float>
        from app.config import settings
        # Template fallback is an explicit emergency opt-in. It cannot stand
        # in for a successful model plan or substitute unrelated context.
        allow_fallback = settings.ALLOW_SCENE_PLANNER_FALLBACK
        # Planner call is the main reliability bottleneck for reaching the
        # quality bar (it gates all contract/ledger constraints). In practice
        # 60s is too aggressive and causes empty output timeouts.
        # Default bumped to 600s because scene_planner calls frequently exceed
        # 180s on the current standard endpoint, which causes avoidable timeouts
        # and blocks chapter generation.
        planner_timeout_s = settings.SCENE_PLANNER_TIMEOUT_SECONDS
        # Hard cap keeps scene_planner from blocking the whole generation with 0 emitted chars.
        # Operators may tune it; timeout is an error unless fallback was explicitly enabled.
        planner_timeout_cap_s = settings.SCENE_PLANNER_TIMEOUT_HARD_CAP_SECONDS
        planner_timeout_s = min(planner_timeout_s, planner_timeout_cap_s)
        # Re-use ContextPack's system prompt as planner background, then
        # inject the planner-specific user instruction.
        background = pack.to_system_prompt()
        chapter_outline_text = self._extract_chapter_outline_text(pack)
        # 单次生成的输出上限:由它决定推荐场数,而不是由"情节自然转折"决定。
        # 一章只写一场时,整章就是一次生成,分段重注入上下文与已写正文的机制完全用不上。
        scene_ceiling = max(MIN_SCENE_WORDS,
                            min(MAX_SCENE_WORDS,
                                int(getattr(settings, "SCENE_MAX_OUTPUT_WORDS", 600) or 0)))
        recommended = recommended_scene_count(target_words, scene_ceiling)
        if type(n_scenes_hint) is int and 1 <= n_scenes_hint <= MAX_SCENE_COUNT:
            recommended = n_scenes_hint
            scene_ceiling = max(MIN_SCENE_WORDS, -(-target_words // recommended))
        hint_line = (
            f"推荐场景数 = {recommended}(本章 {target_words} 字,单场上限 {scene_ceiling} 字)\n"
        )
        instr_block = (
            f"【额外用户指令（改写要求）】\n{user_instruction.strip()}\n\n"
            if user_instruction and user_instruction.strip()
            else ""
        )
        mutex_block = (
            "【场景推进与篇幅】\n"
            f"- 单场 target_words 不得超过 {scene_ceiling} 字:这是一次生成的输出上限,"
            "越界就把这场拆成两场,不要靠「写长一点」解决。\n"
            f"- 本章目标 {target_words} 字,因此推荐 {recommended} 场;"
            "篇幅要拆开写,但分场仍依据人物处境或行动的变化,不为凑场数而换地点、跳时间。\n"
            "- 同一地点、同一时间和持续中的动作可以跨场推进;"
            "不为满足字段差异而增加制度或新事件。\n"
            "- 在 briefs 中分清:本场开始时已经完成什么、本场新增什么、什么仍待以后完成。"
            "已完成的交接或离场不能在下一场重新演一遍;尚未完成的动作可继续,准确名称可重复。\n"
            "- 全部 target_words 合计应接近本章目标,按情节需要分配,没有每场 800 字的下限;"
            "也不允许把整章压成一场来「省事」。短过渡不扩写成独立高潮,最后一场不重复已写出的结尾。\n"
            f"- 每场 target_words 必须为 {MIN_SCENE_WORDS}–{scene_ceiling} 的 JSON 整数;"
            f"本章合计在 {budget_lower}–{budget_upper} 字内(目标的85%–115%)。"
            "预算失衡时保留全部事件,按重要性重新分配详略,不通过删事件或扩大章节目标解决。\n\n"
        )
        contract_block = SCENE_CONTRACT_FIELDS_PROMPT + "\n"
        bridge_block = (
            "【场景之间的承接】\n"
            "- 首场从本章开场要求及前文实际状态起写，后续场景说明本场计划带来的变化；规划不是已发生事实。\n"
            "- transition_bridge 交代下一场需要承接的状态。补足影响事件成立的关键连接，日常到场、离场可合理省略，不额外要求见证者或代价。\n"
            "- continuity_ledger 记录相关人物、物件、消息和资源的场初状态及本场计划变化，与其他合同字段保持一致。\n"
            "- 人物所用信息应符合既有知情范围；有依据的确认保留确认，无依据时不编造来源。作者明确的时间、数量和结果不随规划新增细节改变。\n\n"
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
        # The orchestrator owns the single repair, including all original
        # input and the complete rejected output. Disable Registry's nested
        # repair so this stays at two logical calls, each within the hard cap.
        import asyncio
        from app.services.prompt_registry import run_structured_prompt

        original_user_content = user_content
        previous_output = ""
        last_problem = "unparseable_output"
        known_plan_error: ScenePlanValidationError | None = None
        incomplete_delivery = False
        attempt_plan_error: ScenePlanValidationError | None = None

        def validate_output(payload):
            nonlocal known_plan_error, attempt_plan_error
            try:
                briefs = validate_scene_plan(payload, target_words)
            except ScenePlanValidationError as exc:
                known_plan_error = attempt_plan_error = exc
                raise
            # 计划层定数:与大纲冲突的金额条目丢掉(不阻断),见 services/money_plan.py
            self._reconcile_plan_money(briefs, pack)
            return briefs

        for attempt in (1, 2):
            attempt_plan_error = None
            try:
                parsed = await asyncio.wait_for(
                    run_structured_prompt(
                        task_type="scene_planner", user_content=user_content,
                        db=db, extra_system=background,
                        project_id=str(project_id),
                        chapter_id=str(chapter_id) if chapter_id else None,
                        rag_hits=[], output_validator=validate_output,
                        retry_on_validation_error=False, strict_json=True,
                        **({"temperature": 0.0} if attempt == 2 else {}),
                    ),
                    timeout=planner_timeout_s,
                )
                if isinstance(parsed, dict) and (parsed.get("parse_error") or parsed.get("_parse_error")):
                    problem = parsed.get("validation_error")
                    last_problem = str(attempt_plan_error or problem or "unparseable_output")
                    incomplete_delivery = incomplete_delivery or problem == "provider_incomplete_response"
                    if parsed.get("validation_stage") == "schema" and attempt_plan_error is None:
                        # Registry rejects explicit error markers before the
                        # task validator runs. This is still a known bad plan.
                        known_plan_error = ScenePlanValidationError("invalid_scene_plan", last_problem)
                    raw = parsed.get("raw_text")
                    previous_output = raw if isinstance(raw, str) else ""
                else:
                    previous_output = json.dumps(parsed, ensure_ascii=False)
                    # Also validate at the public boundary; no downstream
                    # consumer relies on a parser or test double doing it.
                    return validate_output(parsed)
            except ScenePlanValidationError as exc:
                last_problem = str(exc)
            except asyncio.TimeoutError:
                last_problem = "timeout"
                logger.warning("scene_planner timed out (attempt=%d/2 timeout=%.1fs)", attempt, planner_timeout_s)
            except Exception as exc:
                last_problem = "model_call_failed"
                logger.warning("scene_planner call failed (attempt=%d/2 type=%s)", attempt, type(exc).__name__)

            if attempt == 1:
                user_content = (
                    original_user_content
                    + f"\n【规划修复】上一版未通过校验：{last_problem}。\n"
                    + "请按原作者要求和已写事实重新输出完整规划。保留原定事件、关键转折及结局，"
                    + "不删除中间事件，不把过程缩成摘要。场数超限时合并场景，篇幅失衡时调整各场详略。"
                    + "上一版若擅改人物关系、数量、物件状态或结果，应按原材料纠正，不能把新编事实固化。"
                    + "只输出一个完整JSON数组，不要解释。\n"
                    + "【上一版规划，仅供修复，不是已发生事实】\n"
                    + (previous_output or "（未取得模型输出）")
                )

        # An existing invalid plan may contain irreplaceable events. Never
        # discard it in favour of emergency templates, even if the next call
        # times out. Explicit fallback remains only for no usable delivery.
        if known_plan_error is not None:
            raise RuntimeError(f"scene_planner_failed: {known_plan_error}")
        if incomplete_delivery:
            raise RuntimeError("scene_planner_failed: provider_incomplete_response")
        _x4_inc_fallback("no_usable_output")
        if allow_fallback:
            return _fallback_scene_briefs(target_words, chapter_outline_text)
        raise RuntimeError(f"scene_planner_failed: {last_problem}")

    @staticmethod
    def _reconcile_plan_money(briefs, pack) -> None:
        """计划里与大纲冲突的金额条目丢掉(**不阻断**)。

        实测(2026-11-17):给了 money_totals 之后**写手**不再自造,但**规划者**会 ——
        ch07 计划写「工钱=八百四十元(三十套×二十八元)」,而 ch08 大纲写"八百二"。
        这里按条目收敛,丢过东西时追加"以大纲已定的金额为准"。见 services/money_plan.py。
        """
        getter = getattr(pack, "numeric_pin_lines", None)
        try:
            pinned = getter() if callable(getter) else ""
        except Exception:  # noqa: BLE001
            pinned = ""
        # 本章自己的大纲同样是权威来源:numeric_pin_lines() 只列"其它章"的数,
        # 漏掉本章大纲会把"照本章大纲写的"误判成自造(实测 ch08 的 820 被整串丢掉)。
        current = getattr(pack, "current_outline", None)
        if isinstance(current, dict):
            try:
                pinned = f"{pinned}\n{json.dumps(current, ensure_ascii=False)}"
            except (TypeError, ValueError):
                pass
        if not pinned.strip():
            return
        for brief in briefs:
            text, dropped = reconcile_money_totals(brief.money_totals, pinned)
            if not dropped:
                continue
            brief.money_totals = text
            logger.warning("scene_planner money conflict dropped=%s kept=%r", dropped, text)

    @staticmethod
    def _extract_chapter_outline_text(pack: ContextPack) -> str:
        """Use the current chapter contract only, never a system-prompt tail."""
        outline = getattr(pack, "current_outline", None)
        if not isinstance(outline, dict):
            return ""
        from app.services.outline_authority import outline_for_generation
        return render_contract_value(outline_for_generation(outline))

    # ----- writer ----------------------------------------------------------

    async def write_scene_stream(
        self,
        *,
        scene: SceneBrief,
        pack: ContextPack,
        prior_scenes_summary: str,
        db: AsyncSession | None,
        project_id: str | UUID,
        chapter_id: Optional[str | UUID],
        user_instruction: str = "",
        extra_user_block: str = "",
    ) -> AsyncIterator[str]:
        """Stream prose for a single scene through scene_writer.

        ``extra_user_block`` 只用于"本场还没写够"的那一次续写:它把续写指令
        插在正文末尾,不改变首写路径的任何提示文本。
        """
        background = pack.to_system_prompt()
        ctx_block = scene.to_writer_user_content()
        prior_block = (
            f"\n\n【本章此前已写正文（完整原文）】\n{prior_scenes_summary}\n"
            "【接写要求】从上述正文最后的实际状态继续。核对前文已经说出的时间、数量、"
            "人物知情和物件去向；它们不能随规划措辞重新变化。"
            "前文没有写成的结果不能仅凭计划就声称已完成。不要重复已完成的动作、对白或离场。\n"
            if prior_scenes_summary
            else "\n\n【已写场景】本场为本章首场，请从本章开场起手。"
        )
        instr_block = (
            f"\n\n【作者对全章的要求】\n{user_instruction.strip()}\n"
            "以上约束用于全章，按本场负责的进展落实；不要求每一场重新完成全部事件。"
            "尚未到本场的后续结果不要提前写出，前场已经完成的事件不要重演。"
            if user_instruction and user_instruction.strip()
            else ""
        )
        # Fix1: inject character name roster directly into user content
        # so it survives even if system prompt is truncated
        name_roster_block = ""
        if pack.character_cards:
            names = [c.name for c in pack.character_cards]
            name_roster_block = (
                f"\n\n【角色名册】本次上下文角色姓名（并非都需出场）：{'、'.join(names)}\n"
                "可依角色档案或作者明确要求使用别名与称谓；结合场景关系区分重名，不得擅自改名或混同人物。"
            )
        contract_block = "\n\n" + WRITER_CONTRACT_PROMPT
        ledger_block = (
            "\n\n【规划与正文】\n"
            "上面的场景台账尚待正文落实，辅助安排本场进展，不能覆盖作者明确要求或已写事实。"
            "按前文实际状态接写，写出本场应有的变化；合理省略按共享写作合同处理。"
        )
        # 大纲已定的金额要贴在生成旁边,不能只埋在系统提示里(约 10k 字,
        # 实测模型会无视)。这里进 user content,和角色名册同一个道理。
        pin_lines = ""
        getter = getattr(pack, "numeric_pin_lines", None)
        if callable(getter):
            pin_lines = getter() or ""
        numeric_block = (
            f"\n\n【本章写到的金额必须与大纲一致】\n{pin_lines}\n"
            "写到这些名目时只能用上面的数;不要自己另算一套总数,也不要把它们改成别的数目。"
            if pin_lines else ""
        )
        # 大纲声明的"次日清晨 / 一周后"要贴在生成旁边:场景合同里的 time_delta
        # 常常退化成占位话术,写手在万字上下文里也不会主动落实(实测 8/100 章)。
        time_block = ""
        try:
            from app.services.time_pins import time_pin_block

            time_block = time_pin_block(_declared_time_cue(scene, pack))
        except Exception as exc:  # noqa: BLE001 - 钉子失败绝不能挡住写作
            logger.warning("time pin block failed: %s", exc)
        # 已确立事实(前面章节写下的约定/金额/期限),与评审侧看同一份。
        # 不加这一段,写手只能现编经济关系,于是出现"期限没到就还款""两千不叫抵一部分"
        # 这类自相矛盾(2026-11-20 用户实测);加了之后模型有据可依,评审也有共同基准。
        fact_block = ""
        try:
            from app.services.established_facts import build_established_facts

            fact_block = (await build_established_facts(
                db, project_id, chapter_id)).block()
        except Exception as exc:  # noqa: BLE001 - 事实块失败绝不能挡住写作
            logger.warning("established facts block failed: %s", exc)
        user_content = (ctx_block + name_roster_block + contract_block + ledger_block
                        + numeric_block + time_block + fact_block
                        + prior_block + instr_block + extra_user_block + "\n\n请开始写本场景。")
        async with aclosing(stream_text_prompt(task_type='scene_writer', user_content=user_content, db=db, extra_system=background, project_id=str(project_id), chapter_id=str(chapter_id) if chapter_id else None, rag_hits=[])) as owned_stream:
            async for chunk in owned_stream:
                yield chunk

    # ----- end-to-end ------------------------------------------------------

    async def orchestrate_chapter_stream(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession | None,
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
        async with generation_context_session(db) as context_db:
            await self._assert_outline_chain_ready(
                db=context_db,
                project_id=project_id,
                volume_id=volume_id,
                chapter_idx=chapter_idx,
                chapter_id=chapter_id,
            )
            pack = await ContextPackBuilder(db=context_db).build(
                project_id=project_id,
                volume_id=volume_id,
                chapter_idx=chapter_idx,
            )
        await pack.resolve_cognition(db=db, user_instruction=user_instruction)
        twords = self.default_target_words if target_words is None else target_words
        # 送模的目标 = 真实章目标 × SCENE_TARGET_OVERALLOC(默认 1.10)。
        # 真实目标仍用于落库/验收/统计,只有规划与写作按放大值进行。
        planning_target = _planning_target(twords) or twords
        if planning_target != twords:
            logger.info(
                "scene planning target overallocated: %d -> %d (factor from SCENE_TARGET_OVERALLOC)",
                twords, planning_target,
            )
        briefs = await self.plan_scenes(
            pack=pack,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
            target_words=planning_target,
            n_scenes_hint=n_scenes_hint,
            user_instruction=user_instruction,
        )
        _x4_observe_scene_count(len(briefs))  # v1.6.0 X4 metric: scene count per chapter
        prior_summary_parts: list[str] = []
        # 章级篇幅核算:场级续写之后仍可能整章偏短(每场各欠一点),所以这里累计
        # 真实产出字数与最后一场正文,循环结束后按需做一次章级补足。
        produced_chars = 0
        last_scene_text = ""
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
            async with aclosing(self.write_scene_stream(scene=scene, pack=pack, prior_scenes_summary='\n\n'.join(prior_summary_parts), db=db, project_id=project_id, chapter_id=chapter_id, user_instruction=user_instruction)) as owned_stream:
                async for chunk in owned_stream:
                    if chunk:
                        scene_text_parts.append(chunk)
                    yield chunk
            full_scene_text = "".join(scene_text_parts)
            # 每场篇幅兜底:提示里已经写了目标字数,但实测只写 330–500/600;
            # 章级质量门在"篇幅不足"时直接 blocked、不进改写,所以这里补有界续写。
            # 续写的 chunk **必须 yield 给调用方**,否则流式产出与落库正文会不一致。
            async for merged in self._scene_continuations(
                    scene=scene, pack=pack, prior_text=full_scene_text, db=db,
                    project_id=project_id, chapter_id=chapter_id,
                    user_instruction=user_instruction):
                full_scene_text += merged
                yield merged
            prior_summary_parts.append(self._summarize_scene(scene, full_scene_text))
            produced_chars += len(full_scene_text)
            last_scene_text = full_scene_text

        # 章级补足:只在整章仍短于验收线时触发,增量必须 yield 出去。
        if briefs:
            async for merged in self._chapter_length_topup(
                scene=briefs[-1], pack=pack, produced_chars=produced_chars,
                chapter_target=twords, last_scene_text=last_scene_text, db=db,
                project_id=project_id, chapter_id=chapter_id,
                user_instruction=user_instruction,
            ):
                yield merged

    async def _scene_continuations(self, *, scene: SceneBrief, pack: ContextPack,
                                   prior_text: str, db: AsyncSession | None,
                                   project_id: str | UUID, chapter_id: Optional[str | UUID],
                                   user_instruction: str = "") -> AsyncIterator[str]:
        """本场写不够时,补 ≤N 次有界续写;产出的是"可直接追加的增量"。

        每次续写都走 ``write_scene_stream``(所以上下文与已写正文会重新注入),
        并按最长公共重叠去掉重复尾巴;续写无效(只是重复 / 太短)就丢弃并停止,
        不再重试。不改变首写路径的任何行为。
        """
        text = prior_text or ""
        rounds = 0
        max_rounds = _scene_continuation_max() if _scene_continuation_enabled() else 0
        while rounds < max_rounds and len(text.strip()) < _scene_min_words(scene.target_words):
            rounds += 1
            shortfall = max(1, _scene_min_words(scene.target_words) - len(text.strip()))
            extra_block = (
                f"\n\n【本场续写】本场还没写够,还差约 {shortfall} 字。"
                "从最后一句接着写下去:不要重述已经写过的内容,不要重新开场,"
                "不要写总结或拿感慨凑数;把本场该发生的转折、人物反应和后果写足。\n"
            )
            parts: list[str] = []
            async with aclosing(self.write_scene_stream(
                    scene=scene, pack=pack, prior_scenes_summary=text, db=db,
                    project_id=project_id, chapter_id=chapter_id,
                    user_instruction=user_instruction,
                    extra_user_block=extra_block)) as continuation:
                async for chunk in continuation:
                    if chunk:
                        parts.append(chunk)
            merged = self._strip_duplicate_seam(text, "".join(parts))
            if not merged:
                logger.warning("scene_length_continuation_discarded scene=%d round=%d",
                               scene.idx, rounds)
                break
            text += merged
            logger.info("scene_length_continuation scene=%d round=%d added=%d total=%d target=%d",
                        scene.idx, rounds, len(merged), len(text), int(scene.target_words or 0))
            yield merged
    async def _chapter_length_topup(
        self, *, scene: SceneBrief, pack: ContextPack, produced_chars: int,
        chapter_target: int, last_scene_text: str, db: AsyncSession | None,
        project_id: str | UUID, chapter_id: Optional[str | UUID],
        user_instruction: str = "",
    ) -> AsyncIterator[str]:
        """整章不足时,用最后一场的收尾上下文补 ≤N 次(增量直接 yield)。

        场级续写只看"单场够不够",章是场之和 —— 每场各欠一点时整章就短了。
        实测(gemini@2000 + 超分配 1.08)10 章里仍有 4 章 <2000,最短 1582。
        这里按**真实章目标 × 验收比**再核一次,不足才补,补到的增量必须 yield 给
        调用方,否则流式展示与落库正文会不一致。
        """
        # 停止线默认取真实章目标(=补到够字数才停),不是质量门验收线 ——
        # 见 _chapter_topup_threshold 的说明。
        need = _chapter_topup_threshold(chapter_target) - int(produced_chars or 0)
        if need <= 0:
            return
        max_rounds = _chapter_continuation_max()
        text = last_scene_text or ""
        rounds = 0
        while rounds < max_rounds and need > 0:
            rounds += 1
            extra_block = (
                f"\n\n【整章补足】本章离目标还差约 {need} 字。从最后一句接着写下去:"
                "不要重述已经写过的内容,不要重新开场,不要写总结或拿感慨凑数;"
                "把本章该落地的动作、人物反应和后果写足,篇幅用在事件推进上。\n"
            )
            parts: list[str] = []
            async with aclosing(self.write_scene_stream(
                    scene=scene, pack=pack, prior_scenes_summary=text, db=db,
                    project_id=project_id, chapter_id=chapter_id,
                    user_instruction=user_instruction,
                    extra_user_block=extra_block)) as continuation:
                async for chunk in continuation:
                    if chunk:
                        parts.append(chunk)
            merged = self._strip_duplicate_seam(text, "".join(parts))
            if not merged:
                logger.warning(
                    "chapter_length_topup_discarded round=%d produced=%d need=%d",
                    rounds, produced_chars, need,
                )
                break
            text += merged
            need -= len(merged)
            logger.info(
                "chapter_length_topup round=%d added=%d remaining=%d target=%d",
                rounds, len(merged), need, chapter_target,
            )
            yield merged


    @staticmethod
    async def _assert_outline_chain_ready(
        *,
        db: AsyncSession | None,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        chapter_id: Optional[str | UUID] = None,
    ) -> None:
        if db is None:
            return
        readiness = await build_outline_readiness_report(
            db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            volume_id=str(volume_id),
            chapter_idx=chapter_idx,
        )
        if not readiness.ready:
            missing = ",".join(readiness.missing_layers)
            raise RuntimeError(
                f"outline_chain_incomplete: {readiness.block_message()} ({missing})"
            )

    @staticmethod
    def _summarize_scene(scene: SceneBrief, scene_text: str) -> str:
        """Keep actual prose; planned actions are not evidence they happened."""
        prose = (scene_text or "").strip()
        return f"[已写第 {scene.idx} 场]\n{prose}" if prose else ""

    @staticmethod
    def _strip_duplicate_seam(prior: str, continuation: str, *, min_overlap: int = 12,
                              min_remain: int = 40) -> str:
        """去掉续写开头重复的尾巴。

        续写最容易犯的错是把上一段最后几句再写一遍。按字符找最长公共重叠:
        重叠 ≥ ``min_overlap`` 就从续写里删掉;删完不足 ``min_remain`` 字,
        这次续写直接丢弃(返回空串),不拼接。机械处理,不指望模型自觉。
        """
        head = (prior or "").rstrip()
        text = (continuation or "").lstrip()
        if not text:
            return ""
        if not head:
            return text if len(text) >= min_remain else ""
        limit = min(len(head), len(text), 400)
        for size in range(limit, min_overlap - 1, -1):
            if head[-size:] == text[:size]:
                rest = text[size:].lstrip()
                return rest if len(rest) >= min_remain else ""
        return text if len(text) >= min_remain else ""
