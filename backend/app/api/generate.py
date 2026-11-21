"""AI content generation endpoints (SSE streaming)."""

import hashlib
import json
import logging
from contextlib import aclosing

import asyncio
from datetime import datetime, timezone
from app.config import settings as _cfg
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Project, Chapter, Outline
from app.services.chapter_target_words import (
    build_planning_scale,
    resolve_chapter_target_word_count,
    resolve_chapter_words,
)
from app.services.chapter_quality_gate import apply_chapter_quality_gate, chapter_persist_status, looks_truncated, looks_like_refusal
from app.services.chapter_generator import ChapterGenerator
from app.services.chinese_prose_mechanics_checker import analyze_chinese_prose_mechanics
from app.services.prose_sanitizer import sanitize_prose
from app.services.established_facts import established_facts_block_from_text as _established_facts_block
from app.services.outline_readiness import (
    LAYER_LABELS,
    build_outline_readiness_report,
    has_meaningful_outline_content,
)
from app.services.outline_generator import OutlineGenerator
from app.services.generation_errors import generation_error_payload

logger = logging.getLogger(__name__)
_CHAPTER_SAVE_BG_TASKS: set = set()  # PR-OL16 + PR-CH-SAVE-RACE

def _x4_inc_revise(outcome: str) -> None:
    """v1.6.0 X4: increment scene_revise_round_total. Best-effort."""
    try:
        from app.observability.metrics import SCENE_REVISE_ROUND_TOTAL
        SCENE_REVISE_ROUND_TOTAL.labels(outcome=outcome).inc()
    except Exception:
        pass

def _inc_chapter_auto_save(kind: str, outcome: str, reason: str) -> None:
    """v1.8.1: increment chapter_auto_save_total. Best-effort.

    Records whether the post-SSE persistence into ``chapters.content_text`` /
    ``outlines`` actually committed. Before v1.8.1, failures were silently
    swallowed by ``logger.warning("Failed to auto-save chapter")`` while the
    SSE client had already received the streamed text, leaving
    ``chapters.content_text`` empty in the main table. Alert on
    ``outcome="failure"`` rate > 0.
    """
    try:
        from app.observability.metrics import CHAPTER_AUTO_SAVE_TOTAL
        CHAPTER_AUTO_SAVE_TOTAL.labels(
            kind=kind, outcome=outcome, reason=reason,
        ).inc()
    except Exception:
        pass


def resolve_rollback_text(
    *,
    baseline_text: str,
    current_text: str,
    persist_on_block: bool,
) -> str:
    """Decide what content a chapter rolls back to when auto-revise aborts.

    The rollback exists to stop a known-bad new draft from replacing a
    known-GOOD prior chapter. That only applies when a prior chapter exists:

    - baseline non-empty  -> restore the baseline (prior confirmed chapter).
    - baseline empty (FRESH chapter):
        * persist_on_block + non-empty current draft -> KEEP the best saved
          draft. Wiping it would strand the chapter at len=0/draft and defeat
          QUALITY_GATE_PERSIST_ON_BLOCK (live ch3: 5003-char on-genre draft
          scored ~8.0 was clobbered to empty).
        * otherwise -> empty (legacy behaviour).
    """
    if (baseline_text or "").strip():
        return baseline_text
    if persist_on_block and (current_text or "").strip():
        return current_text
    return baseline_text or ""


def resolve_rollback_decision(
    *,
    baseline_text: str,
    current_text: str,
    persist_on_block: bool,
    best_draft_text: str | None,
    best_draft_score: float | None,
    baseline_score: float | None,
    keep_min_score: float,
) -> tuple[str, bool, str | None]:
    """Decide between restoring the baseline and keeping the run's best draft.

    Returns ``(text_to_persist, kept_best_draft, keep_reason)``. ``keep_reason``
    is non-None only when this policy kept the best draft; the caller then
    persists it with status='draft' (it is sub-threshold by definition and must
    never masquerade as completed).

    Live incident (2026-07-26): an explicit regeneration improved
    6.5->7.1->7.3->7.9 across auto-revise rounds, missed the 8.2 threshold,
    and the rollback restored a baseline whose only evaluation row was a 0.00
    parse-failure relic -- a scored-7.9 draft was silently replaced by an
    unscored old text.

    Decision table (best draft "eligible" = non-empty text + REAL evaluation
    score >= keep_min_score; parse-failed evaluations never produce a score):

      baseline    baseline_score    best draft           -> outcome
      ---------   ---------------   -------------------  ----------------------
      non-empty   None (unscored)   eligible             keep best (as draft)
      non-empty   None              not eligible         roll back to baseline
      non-empty   s_b               eligible, s_d > s_b  keep best (as draft)
      non-empty   s_b               s_d <= s_b           roll back (equal ->
                                                         conservative baseline)
      non-empty   s_b               below floor          roll back to baseline
      empty       --                --                   legacy
                                                         resolve_rollback_text
                                                         (persist_on_block
                                                         contract unchanged)
    """
    baseline_nonempty = bool((baseline_text or "").strip())
    best_eligible = (
        best_draft_text is not None
        and bool(best_draft_text.strip())
        and best_draft_score is not None
        and best_draft_score >= keep_min_score
    )
    if baseline_nonempty and best_eligible:
        if baseline_score is None:
            return best_draft_text, True, "baseline_unscored"
        if best_draft_score > baseline_score:
            return best_draft_text, True, "best_beats_baseline"
    text = resolve_rollback_text(
        baseline_text=baseline_text,
        current_text=current_text,
        persist_on_block=persist_on_block,
    )
    return text, bool((text or "").strip()) and not baseline_nonempty, None


def _latest_valid_baseline_overall(
    rows,
    *,
    baseline_updated_at,
    run_started_at,
) -> float | None:
    """Newest evaluation ``overall`` attributable to the baseline's CURRENT text.

    ``rows`` are ``(overall, created_at)`` tuples, newest first. A row counts
    only when:

    - ``overall > 0`` -- evaluator parse failures persist sentinel 0.0 rows
      (the incident baseline's only row), which are not real verdicts; and
    - ``baseline_updated_at <= created_at <= run_started_at`` -- evaluations
      are written after the text they scored was saved, so a row older than
      the baseline text's last save scored some EARLIER text, and a row newer
      than run start scored one of THIS run's drafts.

    This is the cheaply-determinable attribution: ``Chapter.updated_at`` also
    bumps on non-content edits (status/summary), so a real baseline score can
    occasionally be misclassified as stale. The cost is bounded -- we then
    keep a floor-gated scored draft as 'draft', and the baseline text stays
    recoverable via its chapter_versions row. Naive datetimes are treated as
    UTC (sqlite test sessions store naive).
    """
    if baseline_updated_at is None or run_started_at is None:
        return None

    def _norm(dt):
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    lo = _norm(baseline_updated_at)
    hi = _norm(run_started_at)
    for overall, created_at in rows:
        if overall is None or created_at is None or overall <= 0:
            continue
        if lo <= _norm(created_at) <= hi:
            return float(overall)
    return None


async def _load_valid_baseline_score(
    chapter_id,
    *,
    baseline_updated_at,
    run_started_at,
) -> float | None:
    """DB half of the baseline-score check (see _latest_valid_baseline_overall).

    Best-effort: any failure returns None, i.e. the baseline is treated as
    unscored -- ROLLBACK_KEEP_MIN_SCORE still guards what may replace it.
    """
    try:
        from app.db.session import async_session_factory
        from app.models.project import ChapterEvaluation
        async with async_session_factory() as sdb:
            rows = (await sdb.execute(
                select(ChapterEvaluation.overall, ChapterEvaluation.created_at)
                .where(ChapterEvaluation.chapter_id == chapter_id)
                .order_by(ChapterEvaluation.created_at.desc())
                .limit(50)
            )).all()
        return _latest_valid_baseline_overall(
            rows,
            baseline_updated_at=baseline_updated_at,
            run_started_at=run_started_at,
        )
    except Exception:
        logger.warning("Rollback baseline-score lookup failed", exc_info=True)
        return None


def _sync_single_shot_llm_kwargs() -> dict[str, float | int | bool]:
    """LLM kwargs for synchronous single-shot chapter generation.

    Direct chapter generation can take several minutes and some upstream
    OpenAI-compatible routes have returned HTTP/2 stream INTERNAL_ERROR near
    the end of a long response. Use a non-streaming request for the direct
    single-shot path so the backend either receives a complete draft it can
    quality-check/save or gets a clean failure, instead of losing a partial SSE
    draft before persistence.
    """
    return {
        "request_timeout": _cfg.SYNC_SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS,
        "retry_attempts": _cfg.SYNC_SINGLE_SHOT_LLM_RETRY_ATTEMPTS,
        "stream": False,
    }


# SSE-HEARTBEAT (2026-07-26): interval between keep-alive comment frames
# emitted while a long non-yielding phase (logic critic / drafter / rewrite /
# evaluator) runs. Must stay well under nginx proxy_read_timeout (600s).
_SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0




async def _foreshadow_setup_event(project_id, chapter_id):
    """章落库后核对"计划了但正文没埋"的伏笔;只读、失败不影响保存。

    2026-11-17:此前这段逻辑直接写在事件流里,却引用了只存在于 `_persist_chapter_now`
    内部的 `save_db` —— 在事件流作用域里是未定义名,`NameError` 被外层 except 吞掉,
    于是这个检查**在生产路径上从未执行过**。抽成独立函数后自己开会话,也便于单测。
    """
    from app.db.session import async_session_factory
    from app.services.foreshadow_setup_check import (
        check_chapter_foreshadow_setup,
        setup_event_payload,
    )

    async with async_session_factory() as db:
        report = await check_chapter_foreshadow_setup(
            db, project_id=project_id, chapter_id=chapter_id
        )
    return report, setup_event_payload(report)


async def _await_chapter_save(task):
    """Finish an already-started save before releasing the project guard."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _yield_with_heartbeat(
    coro, interval: float = _SSE_HEARTBEAT_INTERVAL_SECONDS
) -> AsyncGenerator[tuple[bool, object], None]:
    """Await ``coro`` while keeping the SSE connection alive.

    Yields ``(False, ": ping\\n\\n")`` heartbeat frames while the coroutine is
    still running, then a final ``(True, result)``. SSE comment lines (leading
    colon) are protocol-safe: parsers only act on ``data:`` lines, but proxies
    (nginx proxy_read_timeout=600s) see traffic and keep the pipe open during
    LLM calls that would otherwise emit nothing for >600s.

    The coroutine's exception is re-raised when its result is retrieved, so
    existing try/except blocks around call sites behave unchanged. If the
    consuming generator is closed mid-flight (client disconnect), the inner
    task is cancelled instead of being orphaned.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                break
            yield False, ": ping\n\n"
        yield True, task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


router = APIRouter(prefix="/api/generate", tags=["generate"])


class GenerateChapterRequest(BaseModel):
    project_id: str
    chapter_id: str | None = None  # If None, generates from outline
    volume_id: str | None = None
    chapter_idx: int | None = None
    style_id: str | None = None  # Specific StyleProfile to use
    style_instruction: str = ""
    user_instruction: str = ""
    max_tokens: int = 4096
    skip_polish: bool = False
    # v1.5.0 C1: opt-in two-stage scene-by-scene writing.
    # When true, the chapter is generated via scene_planner -> per-scene
    # scene_writer streams (each 800-1200 chars), giving more coherent
    # pacing and easier per-scene rewrite hooks downstream (C2).
    use_scene_mode: bool = True
    # Hint for scene_planner; optional, 1..20 scenes; natural event boundaries take priority. None = auto.
    n_scenes_hint: int | None = None
    # Override the chapter target word count for scene-mode planning.
    target_words: int | None = None
    # v1.5.0 C2: opt-in auto-revise loop. After the initial scene-mode
    # write completes and is saved, ChapterEvaluator scores the chapter;
    # if overall < revise_threshold, SceneOrchestrator is re-run with the
    # issues fed back as a revise instruction (up to max_revise_rounds).
    # Only effective when use_scene_mode=True (single-shot ChapterGenerator
    # cannot consume per-issue feedback meaningfully).
    auto_revise: bool = True
    # On the 0-10 evaluator scale. Below threshold = regenerate/rewrite.
    revise_threshold: float = 8.2
    # Hard cap on rewrite rounds to bound LLM cost (4 total writes max at N=3).
    max_revise_rounds: int = 3

    # PR-CHGEN-ALIAS (2026-05-07): tolerate legacy/short payload field names
    # so a driver script that posts {"scene_mode": true, "auto_revise": true}
    # still hits scene-mode + auto-revise paths instead of silently falling
    # back to the single-shot ChapterGenerator (which produced the empty
    # `event: scored` SSE traces on ch9/ch10 retries 2026-05-07).
    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_aliases(cls, data):
        if isinstance(data, dict):
            if "scene_mode" in data and "use_scene_mode" not in data:
                data["use_scene_mode"] = data.get("scene_mode")
            if "scene_mode_on" in data and "use_scene_mode" not in data:
                data["use_scene_mode"] = data.get("scene_mode_on")
        return data


class GenerateOutlineRequest(BaseModel):
    project_id: str | None = None
    level: str  # book, volume, chapter
    user_input: str = ""
    parent_outline_id: str | None = None
    volume_idx: int | None = None
    chapter_idx: int | None = None
    style_id: str | None = None
    planned_volume_count: int | None = None
    # v1.4.2 Task B: opt-in structured staged SSE stream for book outline.
    # Mirrors ?staged_stream=1 query param; body field wins when both set.
    staged_stream: bool | None = None


def _settings_style_profile_id(settings: dict) -> str | None:
    sid = settings.get("style_profile_id")
    if not sid:
        style_ref = settings.get("style_reference") or {}
        if isinstance(style_ref, dict):
            sid = style_ref.get("profile_id")
    return sid if isinstance(sid, str) and sid.strip() else None


def _settings_structure_book_id(settings: dict) -> str | None:
    plot_structure = settings.get("plot_structure") or {}
    if not isinstance(plot_structure, dict):
        return None
    sid = plot_structure.get("structure_book_id")
    return sid if isinstance(sid, str) and sid.strip() else None


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/chapter")
async def generate_chapter(
    req: GenerateChapterRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Generate chapter content via SSE streaming."""
    # Query all DB data BEFORE creating the generator to avoid
    # using the session after FastAPI's dependency lifecycle closes it.
    project = await db.get(Project, req.project_id)
    if not project:
        return StreamingResponse(
            # [DONE] sentinel: every exit path must terminate the stream so
            # clients don't hang until their own timeout (same pattern as the
            # in-stream error exits).
            iter([
                f"data: {json.dumps({'error': 'Project not found'})}\n\n",
                "data: [DONE]\n\n",
            ]),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    # 2026-11-17: 每章 token 预算护栏。只拦"再次生成一个已存在的章" —— 新章
    # (req.chapter_id 为空)还没有任何用量。超限时用 SSE 事件终止,不开始流式
    # 生成;已保存的正文与章状态不受影响。护栏自身故障绝不阻断生成。
    try:
        from app.services.chapter_budget import chapter_budget_verdict

        _budget = await chapter_budget_verdict(db, req.chapter_id)
        if _budget.exceeded:
            logger.warning(
                "chapter_budget_exceeded chapter_id=%s used=%d limit=%d",
                req.chapter_id, _budget.used_tokens, _budget.limit_tokens,
            )
            return StreamingResponse(
                iter([
                    f"data: {json.dumps(_budget.as_event(), ensure_ascii=False)}\n\n",
                    "data: [DONE]\n\n",
                ]),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
    except Exception as _budget_err:  # pragma: no cover - guard must never block
        logger.warning("chapter budget preflight failed: %s", _budget_err)

    # A2 (2026-11-17): 生成期间作者可能已经改稿并保存。请求开始时记下正文基线,
    # 保存前比对;不一致就保留作者文本、把 AI 稿落成候选,并发事件。
    from app.services.chapter_autosave import author_edited_since, content_hash

    _baseline_content_hash: str | None = None
    if req.chapter_id:
        _row = (await db.execute(
            select(Chapter.content_text).where(Chapter.id == req.chapter_id)
        )).scalar_one_or_none()
        _baseline_content_hash = content_hash(_row)
    _save_notice: dict[str, str | None] = {"reason": None}
    project_settings = project.settings_json or {}
    # PR-FIX-PROJSET-STYLEFALLBACK (2026-05-05): resolve project-bound style/structure from settings_json
    if not req.style_id:
        sid = project_settings.get("style_profile_id")
        if not sid:
            sr = project_settings.get("style_reference") or {}
            if isinstance(sr, dict):
                sid = sr.get("profile_id")
        if sid:
            req.style_id = sid

    # PR-FIX-PROJSET-STRUCTURE (2026-05-05): resolve plot structure from settings_json.plot_structure.structure_book_id
    structure_prompt: str = ""
    try:
        ps_cfg = project_settings.get("plot_structure") or {}
        sb_id = ps_cfg.get("structure_book_id") if isinstance(ps_cfg, dict) else None
        if sb_id:
            from app.models.project import ReferenceBook
            from app.services.plot_structure import compile_structure_prompt
            from app.services.style_runtime import (
                get_dossier_block,
                scrub_reference_proper_nouns,
            )
            book = await db.get(ReferenceBook, sb_id)
            if book:
                # Prefer the consolidated dossier structure block; fall back
                # to compiling the stored plot_structure extraction.
                structure_prompt = get_dossier_block(book, "structure_block")
                if not structure_prompt and book.metadata_json:
                    ps = (book.metadata_json or {}).get("plot_structure")
                    if ps and isinstance(ps, dict) and "error" not in ps:
                        structure_prompt = compile_structure_prompt(ps) or ""
                if structure_prompt:
                    # Structure prompts have leaked source-book character names
                    # (e.g. 西泽尔/密涅瓦) into new projects; scrub known names.
                    structure_prompt = scrub_reference_proper_nouns(structure_prompt, book)
    except Exception as e:
        logger.warning("Plot structure resolve failed: %s", e)

    chapter_outline: dict = {}
    current_text = ""
    target_words: int | None = None
    # ROLLBACK-POLICY (2026-07-27): when the baseline chapter row was last
    # written, so the rollback site can tell whether an old evaluation row
    # scored THIS baseline text or some earlier one.
    baseline_updated_at_before_run = None
    baseline_status_before_run = "draft"

    chapter = None
    if req.chapter_id:
        chapter = await db.get(Chapter, req.chapter_id)
        if chapter:
            chapter_outline = chapter.outline_json or {}
            current_text = chapter.content_text or ""
            baseline_status_before_run = chapter.status
            target_words = chapter.target_word_count
            # getattr: fakes/legacy rows without updated_at -> None, which the
            # attribution helper treats as "baseline unscored".
            baseline_updated_at_before_run = getattr(chapter, "updated_at", None)

    target_words = resolve_chapter_target_word_count(
        target_words,
        resolve_chapter_words(project_settings),
    )

    # Resolve style: manual text > explicit style_id > settings/bind chain.
    # resolve_style_context returns the layered style text (基调层 dossier
    # style_block + 修正层 compiled profile rules when both exist, single
    # layer otherwise; samples never injected raw). The layered text flows
    # into BOTH the writer prompt (build_style_injection_block below) and
    # the ChapterEvaluator calls (style_profile=resolved_style).
    resolved_style = req.style_instruction
    if not resolved_style:
        try:
            from app.services.style_runtime import resolve_style_context
            _style_ctx = await resolve_style_context(
                db, req.project_id, req.chapter_id, style_id=req.style_id
            )
            resolved_style = _style_ctx.style_text
        except Exception as e:
            logger.warning("Style resolve failed: %s", e)

    async def event_stream() -> AsyncGenerator[str, None]:
        nonlocal current_text
        collected_text: list[str] = []
        last_saved_quality_meta: dict | None = None
        from app.services.chapter_acceptance import ChapterAcceptance
        acceptance = ChapterAcceptance(
            required=bool(req.use_scene_mode and req.auto_revise and req.chapter_id),
            threshold=float(req.revise_threshold),
        )
        # PR-SCENE-PARTIAL (2026-07-26): set when scene mode AND its
        # single-shot fallback both fail but non-trivial completed-scene text
        # was accumulated. The preserved partial then skips the quality gate
        # and auto-revise and is persisted as a draft instead of being lost.
        partial_draft_only = False
        partial_quality_meta: dict | None = None
        # Baseline snapshot so auto-revise failures do not leave a known-bad
        # draft as the active chapter.content_text.
        baseline_text_before_run = current_text
        # ROLLBACK-POLICY: evaluation rows created after this instant belong
        # to THIS run's drafts, not the baseline.
        run_started_at = datetime.now(timezone.utc)
        try:
            yield f"data: {json.dumps({'status': 'generating', 'message': 'Starting...'})}\n\n"

            effective_user_instruction = req.user_instruction or ""
            if target_words:
                effective_user_instruction = (
                    effective_user_instruction
                    + f"\n\n【本章目标字数】软目标约 {target_words} 字。默认单章可在 2000-6000 字内自然浮动；不要为凑字数补空话、重复心理剖白、制度解释或大纲外剧情。"
                )
            # Style threading (mirrors the async path's [风格要求] block):
            # effective_user_instruction reaches BOTH ChapterGenerator.generate
            # and SceneOrchestrator.orchestrate_chapter_stream (-> scene_writer).
            if resolved_style:
                from app.services.style_runtime import build_style_injection_block
                _style_block = build_style_injection_block(resolved_style)
                if _style_block:
                    effective_user_instruction = (
                        effective_user_instruction + "\n\n" + _style_block
                    ).strip()
            if structure_prompt:
                effective_user_instruction = (
                    effective_user_instruction + "\n\n" + structure_prompt
                )
            # Defect-class immunity preflight (era/seam guidance + the
            # per-project recurring-defect ledger). Same builder as the
            # celery path; failure-tolerant so it can never block the SSE run.
            if chapter is not None:
                try:
                    from app.tasks.generation_tasks import (
                        _build_chinese_prose_preflight_prompt,
                    )
                    _preflight, _ = await _build_chinese_prose_preflight_prompt(chapter, db)
                    if _preflight:
                        effective_user_instruction = (
                            effective_user_instruction + "\n\n" + _preflight
                        ).strip()
                except Exception as _pf_err:  # pragma: no cover - defensive
                    logger.warning("SSE preflight skipped: %s", _pf_err)

            # v0.5: ChapterGenerator takes project_id/volume_id/chapter_idx.
            # Resolve them — prefer loaded `chapter` above, fall back to request fields.
            resolved_volume_id: str | None = None
            resolved_chapter_idx: int | None = None
            if req.chapter_id:
                ch = await db.get(Chapter, req.chapter_id)
                if ch:
                    resolved_volume_id = str(ch.volume_id)
                    resolved_chapter_idx = ch.chapter_idx
            if resolved_volume_id is None:
                resolved_volume_id = req.volume_id
            if resolved_chapter_idx is None:
                resolved_chapter_idx = req.chapter_idx

            if not resolved_volume_id or resolved_chapter_idx is None:
                yield f"data: {json.dumps({'error': 'volume_id and chapter_idx required (directly or via chapter_id)'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            readiness = await build_outline_readiness_report(
                db,
                project_id=req.project_id,
                chapter_id=req.chapter_id,
                volume_id=resolved_volume_id,
                chapter_idx=resolved_chapter_idx,
            )
            if not readiness.ready:
                yield f"data: {json.dumps({'event': 'generation_blocked', 'status': 'blocked', 'reason': 'outline_chain_incomplete', 'message': readiness.block_message(), 'missing_layers': readiness.missing_layers, 'readiness': readiness.to_dict()}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            evaluation_context = ""
            following_context_for_eval = ""
            previous_text = ""
            if req.chapter_id:
                from app.services.evaluation_neighbors import load_evaluation_neighbors
                neighbors = await load_evaluation_neighbors(db, req.chapter_id, req.project_id)
                evaluation_context = neighbors.previous_context
                following_context_for_eval = neighbors.following_context
                previous_text = neighbors.previous_tail

            # All values used below are snapshots; this endpoint has only read.
            # Do not retain its transaction while the model plans/writes.
            await db.rollback()

            # v1.5.0 C1: opt-in scene-staged streaming. SceneOrchestrator
            # plans scenes around natural event boundaries, with per-scene budgets.
            # Falls back to ChapterGenerator's
            # single-shot "generation" prompt when use_scene_mode is False.
            stream_iter: AsyncGenerator[str, None]
            async def _run_single_shot_generator() -> str:
                generator = ChapterGenerator()
                text = await generator.generate(
                    project_id=req.project_id,
                    volume_id=resolved_volume_id,
                    chapter_idx=resolved_chapter_idx,
                    db=None,
                    chapter_id=req.chapter_id,
                    user_instruction=effective_user_instruction,
                    **_sync_single_shot_llm_kwargs(),
                )
                return text or ""

            if req.use_scene_mode:
                from app.services.scene_orchestrator import SceneOrchestrator

                orchestrator = SceneOrchestrator()
                effective_target_words = req.target_words or target_words

                # SceneOrchestrator awaits this callback just before each
                # scene's first chunk; a callback cannot yield into this SSE
                # generator, so it buffers the frame and the chunk loops below
                # drain the buffer before emitting the next text chunk (same
                # spirit as collected_text for text). SceneBrief only carries
                # its own 1-based idx/title — the orchestrator does not pass
                # the plan size to the callback — so ``total`` is null.
                _pending_scene_events: list[str] = []

                async def _on_scene_start(scene) -> None:  # type: ignore[no-untyped-def]
                    payload = {
                        "event": "scene",
                        "scene_idx": int(getattr(scene, "idx", 0) or 0),
                        "total": None,
                        "title": getattr(scene, "title", "") or "",
                    }
                    _pending_scene_events.append(
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )

                try:
                    stream_iter = orchestrator.orchestrate_chapter_stream(
                        project_id=req.project_id,
                        volume_id=resolved_volume_id,
                        chapter_idx=resolved_chapter_idx,
                        db=None,
                        chapter_id=req.chapter_id,
                        user_instruction=effective_user_instruction,
                        target_words=effective_target_words,
                        n_scenes_hint=req.n_scenes_hint,
                        on_scene_start=_on_scene_start,
                    )
                    async with aclosing(stream_iter) as owned_stream:
                        async for chunk in owned_stream:
                            while _pending_scene_events:
                                yield _pending_scene_events.pop(0)
                            if chunk:
                                collected_text.append(chunk)
                            yield f"data: {json.dumps({'text': chunk})}\n\n"
                    # A scene that produced zero chunks leaves its event queued.
                    while _pending_scene_events:
                        yield _pending_scene_events.pop(0)
                except Exception as scene_err:
                    from app.services.context_errors import RequiredContextUnavailable
                    if isinstance(scene_err, RequiredContextUnavailable) and not any(collected_text):
                        raise scene_err
                    logger.warning("Scene generation interrupted: %s", type(scene_err).__name__)
                    if any(collected_text):
                        # Keep already delivered prose; do not reset the client
                        # or switch generators after a partial response.
                        partial_draft_only = True
                        partial_quality_meta = {
                            "persisted_on_block": True,
                            "reason": "partial_generation_interrupted",
                        }
                        yield f"data: {json.dumps({'event': 'generation_interrupted', 'message': '生成中断，已保留收到的片段，内容可能不完整，请复核。'}, ensure_ascii=False)}\n\n"
                    else:
                        _pending_scene_events.clear()
                        yield f"data: {json.dumps({'event': 'fallback_restart'}, ensure_ascii=False)}\n\n"
                        generated_text = ""
                        async with aclosing(_yield_with_heartbeat(_run_single_shot_generator())) as owned_stream:
                            async for (_hb_done, _hb_val) in owned_stream:
                                if _hb_done:
                                    generated_text = _hb_val
                                else:
                                    yield _hb_val
                        if generated_text:
                            collected_text.append(generated_text)
                        yield f"data: {json.dumps({'text': generated_text}, ensure_ascii=False)}\n\n"
            else:
                generated_text = ""
                async with aclosing(_yield_with_heartbeat(_run_single_shot_generator())) as owned_stream:
                    async for (_hb_done, _hb_val) in owned_stream:
                        if _hb_done:
                            generated_text = _hb_val
                        else:
                            yield _hb_val
                if generated_text:
                    collected_text.append(generated_text)
                yield f"data: {json.dumps({'text': generated_text}, ensure_ascii=False)}\n\n"

            # PR-CH-SAVE-RACE: chapter SSE save was racing with Starlette
            # BaseHTTPMiddleware cancel scope (same root cause as PR-OL16).
            # nginx logged POST /api/generate/chapter 200 + 229KB while
            # chapters.content_text stayed empty; backend log showed
            # CancelledError + asyncpg connection terminating mid-commit.
            # Fix: run the entire save body as a detached bg task with
            # asyncio.shield + module-level strong reference so the commit
            # survives the SSE pipe being torn down.
            full_text = "".join(collected_text)
            # Q3 v1.9.1 (review fix): the final settled chapter text to feed the
            # character cognition ledger exactly once, right before `completed`.
            # None = nothing was saved, or the revise loop rolled the text back.
            cognition_ingest_text: str | None = None
            if full_text:
                quality_gate_result = None
                quality_gate_meta = None
                if partial_draft_only:
                    # PR-SCENE-PARTIAL: a preserved partial goes straight to
                    # the draft save — running the quality gate on it could
                    # block the save and lose the partial all over again.
                    quality_gate_meta = partial_quality_meta
                elif not req.skip_polish:
                    try:
                        try:
                            # Release any locks inherited from earlier SELECTs
                            # before the quality gate opens its own session.
                            await db.rollback()
                        except Exception:
                            pass
                        pre_quality_report = analyze_chinese_prose_mechanics(full_text)
                        yield f"data: {json.dumps({'event': 'quality_check', 'status': 'passed' if pre_quality_report.passed else 'needs_rewrite', 'passed': pre_quality_report.passed, 'report': pre_quality_report.to_safe_dict()}, ensure_ascii=False)}\n\n"
                        if not pre_quality_report.passed:
                            yield f"data: {json.dumps({'event': 'quality_rewrite_start', 'rounds': 2}, ensure_ascii=False)}\n\n"
                        from app.db.session import async_session_factory
                        from app.services.chapter_pipeline import run_chapter_pipeline
                        async with async_session_factory() as quality_db:
                            pipeline_result = None
                            async with aclosing(_yield_with_heartbeat(run_chapter_pipeline(text=full_text, db=None, project_id=req.project_id, chapter_id=req.chapter_id, target_word_count=target_words, chapter_outline=chapter_outline, prev_chapter_tail=(previous_text or '')[-1500:], skip_polish=False))) as owned_stream:
                                async for (_hb_done, _hb_val) in owned_stream:
                                    if _hb_done:
                                        pipeline_result = _hb_val
                                    else:
                                        yield _hb_val
                        # logic_critic 报告作为独立可选事件（前端可选消费，不破坏既有事件）。
                        yield f"data: {json.dumps({'event': 'logic_critic_done', **pipeline_result.to_echo_report()}, ensure_ascii=False)}\n\n"
                        quality_gate_result = pipeline_result.quality_gate_result
                        quality_gate_meta = quality_gate_result.to_safe_metadata()
                        if quality_gate_result.status != "passed":
                            # PERSIST-ON-BLOCK (env-gated, default off): when the
                            # gate cannot reach a clean pass, optionally keep its
                            # best improved text (with a quality warning) instead
                            # of discarding it and stranding stale/placeholder prose.
                            _persist_on_block = _cfg.QUALITY_GATE_PERSIST_ON_BLOCK
                            _pob_text = quality_gate_result.final_text or ""
                            if _persist_on_block and _pob_text.strip():
                                yield f"data: {json.dumps({'event': 'quality_failed', 'status': quality_gate_result.status, 'reason': quality_gate_result.warning_reason, 'rounds': quality_gate_result.rewrite_rounds, 'persisted_on_block': True, 'report': quality_gate_result.final_report.to_safe_dict()}, ensure_ascii=False)}\n\n"
                                full_text = _pob_text
                                if isinstance(quality_gate_meta, dict):
                                    quality_gate_meta = {**quality_gate_meta, "persisted_on_block": True}
                            else:
                                yield f"data: {json.dumps({'event': 'quality_failed', 'status': quality_gate_result.status, 'reason': quality_gate_result.warning_reason, 'rounds': quality_gate_result.rewrite_rounds, 'report': quality_gate_result.final_report.to_safe_dict()}, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                return
                        elif quality_gate_result.rewrite_rounds > 0:
                            yield f"data: {json.dumps({'event': 'quality_rewrite_done', 'status': quality_gate_result.status, 'rounds': quality_gate_result.rewrite_rounds, 'content_text': quality_gate_result.final_text, 'report': quality_gate_result.final_report.to_safe_dict()}, ensure_ascii=False)}\n\n"
                        if quality_gate_result.status == "passed":
                            full_text = quality_gate_result.final_text
                    except Exception as quality_err:
                        logger.warning(
                            "chapter quality gate failed; blocking save: %s",
                            quality_err,
                            exc_info=True,
                        )
                        yield f"data: {json.dumps({'event': 'quality_failed', 'status': 'blocked', 'reason': 'quality_gate_exception', 'error_class': type(quality_err).__name__}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                async def _persist_chapter_now(text_to_save: str, quality_meta: dict | None = None):
                    """Returns (chapter_id_or_none, word_count, no_target_row)."""
                    nonlocal last_saved_quality_meta
                    from app.db.session import async_session_factory
                    async with async_session_factory() as save_db:
                        target_chapter = None
                        if req.chapter_id:
                            # A2: 行锁 —— 与作者手工保存/另一路生成互斥
                            _locked = await save_db.execute(
                                select(Chapter).where(Chapter.id == req.chapter_id).with_for_update()
                            )
                            target_chapter = _locked.scalar_one_or_none()
                        if target_chapter is None and resolved_volume_id and resolved_chapter_idx is not None:
                            lookup = await save_db.execute(
                                select(Chapter).where(
                                    Chapter.volume_id == resolved_volume_id,
                                    Chapter.chapter_idx == resolved_chapter_idx,
                                ).with_for_update()
                            )
                            target_chapter = lookup.scalar_one_or_none()
                        if target_chapter is None:
                            return None, 0, True
                        _save_notice["reason"] = None
                        # A2: 作者在生成期间改过并保存过 → 不覆盖作者文本
                        if author_edited_since(_baseline_content_hash, target_chapter.content_text):
                            from app.models.project import ChapterVersion
                            save_db.add(ChapterVersion(
                                chapter_id=target_chapter.id,
                                parent_id=None,
                                branch_name="main",
                                content_text=text_to_save,
                                content_diff="",
                                word_count=len(text_to_save),
                                is_active=0,
                                source="ai_generation_conflict",
                                metadata_json={
                                    "caller": "api.generate.stream_generate",
                                    "chapter_idx": resolved_chapter_idx,
                                    "quality_gate": quality_meta or {},
                                    "reason": "author_edited_during_generation",
                                },
                            ))
                            await save_db.commit()
                            _save_notice["reason"] = "author_edited"
                            _inc_chapter_auto_save("chapter", "success", "author_edited")
                            logger.warning(
                                "auto-save kept author text; AI draft stored as candidate "
                                "chapter_id=%s words=%d", target_chapter.id, len(text_to_save),
                            )
                            return str(target_chapter.id), target_chapter.word_count or 0, False
                        # PR-CH-TRUNCATION (2026-06-29): a chapter whose body ends
                        # mid-sentence (last scene_writer call hit max_tokens; the
                        # stream ended with no terminal punctuation) must NOT be
                        # silently marked completed. Persist the text (nothing is
                        # lost) but downgrade status to draft and surface the signal.
                        # Mechanism-level meta/structure leakage guard: strip
                        # any "第X章"/context-delimiter that leaked into prose
                        # before it is ever persisted (both gen paths funnel here).
                        text_to_save, _leak_hits = sanitize_prose(text_to_save)
                        if _leak_hits:
                            logger.warning(
                                "prose_sanitizer stripped meta leakage chapter_id=%s hits=%s",
                                req.chapter_id, _leak_hits,
                            )
                        target_chapter.content_text = text_to_save
                        target_chapter.word_count = len(text_to_save)
                        # PR-BLOCKED-STATUS (2026-07-26): truncation, refusal, OR a
                        # persist-on-block save all downgrade to draft — a blocked
                        # chapter must never be silently marked completed.
                        target_chapter.status = acceptance.status(text_to_save, quality_meta)
                        # A4 (2026-11-17): 正文与 active 版本切换**同一事务**。
                        # 原先正文先 commit、版本行再 commit 且失败只 warning,会出现
                        # "正文已更新但 active 版本仍是旧稿",后续所有依赖当前保存稿的
                        # 入口都会拿旧稿当真值。版本写失败 → 整体回滚,正文不落库。
                        try:
                            from app.models.project import ChapterVersion
                            from sqlalchemy import update as _sql_update
                            await save_db.flush()
                            if target_chapter.status == "completed":
                                await save_db.execute(
                                    _sql_update(ChapterVersion)
                                    .where(ChapterVersion.chapter_id == target_chapter.id,
                                           ChapterVersion.is_active == 1)
                                    .values(is_active=0)
                                )
                            cv = ChapterVersion(
                                chapter_id=target_chapter.id,
                                parent_id=None,
                                branch_name="main",
                                content_text=text_to_save,
                                content_diff="",
                                word_count=len(text_to_save),
                                is_active=1 if target_chapter.status == "completed" else 0,
                                source="ai_generation",
                                metadata_json={
                                    "caller": "api.generate.stream_generate",
                                    "chapter_idx": resolved_chapter_idx,
                                    "quality_gate": quality_meta or {},
                                },
                            )
                            save_db.add(cv)
                            await save_db.commit()
                            logger.info(
                                "PR-VER1: ChapterVersion written chapter_id=%s ver_id=%s",
                                target_chapter.id, cv.id,
                            )
                        except Exception as _ver_err:
                            await save_db.rollback()
                            _save_notice["reason"] = "save_failed"
                            _inc_chapter_auto_save("chapter", "failure", "version_write_failed")
                            logger.error(
                                "auto-save ROLLED BACK (version write failed) chapter_id=%s err=%s",
                                req.chapter_id, _ver_err,
                            )
                            return None, 0, True
                        last_saved_quality_meta = quality_meta
                        await save_db.refresh(target_chapter)
                        _inc_chapter_auto_save("chapter", "success", "ok")
                        # 章后收尾走**统一实现**(唯一执行链):摘要 / 实体派发 /
                        # 卷桥接 / 伏笔缺铺垫 / 事实一致性 —— 三入口(SSE / 队列 / 探针)
                        # 共用同一条链,见 app/services/chapter_finalize.py。
                        # 每步独立成败:任何一步失败只记 warning,不影响正文交付。
                        try:
                            from app.services.chapter_finalize import finalize_chapter

                            # 先把收尾前的簿记落盘(摘要与派发各自有独立读写阶段,
                            # 这里提前 commit 避免长事务)。
                            await save_db.commit()
                            await finalize_chapter(
                                db=save_db,
                                chapter=target_chapter,
                                project_id=str(req.project_id),
                                chapter_id=target_chapter.id,
                                volume_id=getattr(target_chapter, "volume_id", None),
                                full_text=getattr(target_chapter, "content_text", "") or "",
                                caller="api.generate.stream_generate",
                            )
                        except Exception as _finalize_err:
                            logger.warning(
                                "Chapter finalize after auto-save failed: %s", _finalize_err
                            )
                        # Q3 v1.9.1 (review fix): cognition ledger ingestion
                        # intentionally NOT here. Extracting at draft-save time
                        # polluted the ledger with the chapter's own facts
                        # before evaluation (self-masking cognition_violation),
                        # double-ingested via the final-polish re-save, and
                        # survived revise-abort rollbacks. It now runs exactly
                        # once at the end of the stream, after the final text
                        # is settled (see `cognition_ingest_text` below).
                        return str(target_chapter.id), target_chapter.word_count, False

                _chsave_task = asyncio.create_task(_persist_chapter_now(full_text, quality_gate_meta))
                _CHAPTER_SAVE_BG_TASKS.add(_chsave_task)
                _chsave_task.add_done_callback(_CHAPTER_SAVE_BG_TASKS.discard)
                try:
                    _saved_id, _wc, _no_target = await _await_chapter_save(_chsave_task)
                    if _no_target:
                        logger.warning(
                            "Auto-save chapter: no target row (chapter_id=%s vol=%s idx=%s)",
                            req.chapter_id, resolved_volume_id, resolved_chapter_idx,
                        )
                        _inc_chapter_auto_save("chapter", "failure", "no_target_row")
                        yield f"data: {json.dumps({'status': 'save_failed', 'kind': 'chapter', 'reason': 'no_target_row', 'chapter_id': req.chapter_id, 'volume_id': str(resolved_volume_id) if resolved_volume_id else None, 'chapter_idx': resolved_chapter_idx})}\n\n"
                    else:
                        if not partial_draft_only:
                            # PR-SCENE-PARTIAL: a preserved partial is not
                            # settled chapter text; keep it out of the ledger.
                            cognition_ingest_text = full_text
                        if looks_truncated(full_text):
                            yield f"data: {json.dumps({'event': 'truncated', 'status': 'draft', 'reason': 'midsentence_ending', 'chapter_id': _saved_id, 'word_count': _wc}, ensure_ascii=False)}\n\n"
                        if looks_like_refusal(full_text):
                            yield f"data: {json.dumps({'event': 'refusal', 'status': 'draft', 'reason': 'image_model_refusal', 'chapter_id': _saved_id, 'word_count': _wc}, ensure_ascii=False)}\n\n"
                        if _save_notice.get("reason") == "author_edited":
                            # A2: 作者在生成期间保存过 —— 正文是作者的,AI 稿落候选
                            yield f"data: {json.dumps({'status': 'saved', 'event': 'author_edited_during_generation', 'skipped_overwrite': True, 'chapter_id': _saved_id, 'word_count': _wc}, ensure_ascii=False)}\n\n"
                        else:
                            yield f"data: {json.dumps({'status': 'saved', 'chapter_id': _saved_id, 'word_count': _wc})}\n\n"
                        if partial_draft_only:
                            yield f"data: {json.dumps({'event': 'partial_saved', 'status': 'draft', 'reason': 'partial_generation_interrupted', 'chapter_id': _saved_id, 'word_count': _wc}, ensure_ascii=False)}\n\n"
                        # 2026-11-17: 伏笔缺铺垫——大纲/作者为本章计划的伏笔,
                        # 正文里是否留下了可检索的锚点。只发事件:不阻断保存、
                        # 不改章状态、不写库;核对失败也只是 warning。
                        try:
                            from app.config import settings as _settings

                            if getattr(_settings, "FORESHADOW_SETUP_CHECK_ENABLED", False):
                                _setup_report, _setup_event = await _foreshadow_setup_event(
                                    req.project_id, _saved_id
                                )
                                if _setup_event:
                                    logger.warning(
                                        "foreshadow_setup_missing chapter_id=%s planned=%d missing=%d",
                                        _saved_id,
                                        _setup_report.planned,
                                        len(_setup_report.missing),
                                    )
                                    yield f"data: {json.dumps(_setup_event, ensure_ascii=False)}\n\n"
                        except Exception as _setup_err:
                            logger.warning(
                                "foreshadow setup check failed chapter_id=%s: %s",
                                _saved_id, _setup_err,
                            )
                except asyncio.CancelledError:
                    # SSE pipe cancelled mid-save; bg task continues commit independently.
                    raise
                except Exception as save_err:
                    logger.error(
                        "Auto-save chapter FAILED (chapter_id=%s vol=%s idx=%s): %s",
                        req.chapter_id, resolved_volume_id, resolved_chapter_idx,
                        save_err, exc_info=True,
                    )
                    _inc_chapter_auto_save(
                        "chapter", "failure", type(save_err).__name__
                    )
                    try:
                        yield (
                            f"data: {json.dumps({'status': 'save_failed', 'kind': 'chapter', 'error': '保存未获确认，请刷新检查当前内容后再操作。', 'error_class': type(save_err).__name__})}\n\n"
                        )
                    except Exception:
                        pass


            # ----------------------------------------------------------------
            # v1.5.0 C2: scene-mode auto-revise loop.
            # After the initial save, evaluate the chapter; if overall <
            # revise_threshold, re-run SceneOrchestrator with the issues fed
            # back as a revise instruction. Up to req.max_revise_rounds extra
            # writes (so 3 total at N=2). Persists each ChapterEvaluation row
            # for telemetry and overwrites Chapter.content_text on every
            # revised round so the latest revision wins. Single-shot
            # ChapterGenerator path is intentionally skipped — it cannot
            # consume per-issue feedback meaningfully (no scene boundaries).
            # ----------------------------------------------------------------
            if (
                full_text
                and not partial_draft_only
                and req.use_scene_mode
                and req.auto_revise
                and resolved_volume_id is not None
                and resolved_chapter_idx is not None
                and req.chapter_id
            ):
                # Pre-bind loop state so the post-loop polish / rollback /
                # cognition-ingestion settlement below never hits a NameError
                # when the try-block fails before its own initialization.
                accepted_final = False
                aborted_with_blocking = False
                c2_revised = False
                # ROLLBACK-POLICY (2026-07-27): best REAL-scored draft of this
                # run (text + overall). Parse-failed evaluations (sentinel
                # overall=0) never populate these.
                best_draft_text: str | None = None
                best_draft_score: float | None = None
                current_text = full_text
                try:
                    # C2 deadlock fix: the outer baseline-path session (`db`)
                    # may still hold an open transaction with row-level locks
                    # on prompt_assets / projects from earlier ContextPack and
                    # PromptRegistry SELECTs. Without an explicit rollback,
                    # the revise scene_writer's UPDATE prompt_assets SET
                    # success_count=... blocks indefinitely on a transactionid
                    # lock held by this idle outer session. Force-release.
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    from app.db.session import async_session_factory
                    from app.models.project import Chapter as _Chapter
                    from app.models.project import ChapterEvaluation
                    from app.services.auto_revise import (
                        issues_to_revise_instruction,
                        merge_revise_into_user_instruction,
                        revise_spans,
                        should_revise,
                        targeted_revision_enabled,
                        RevisionContext,
                    )
                    from app.services.chapter_evaluator import ChapterEvaluator
                    from app.services.scene_orchestrator import SceneOrchestrator

                    revise_chapter_id = req.chapter_id
                    current_text = full_text
                    revise_outline = chapter_outline or {}
                    max_rounds = max(0, int(req.max_revise_rounds))
                    threshold = float(req.revise_threshold)

                    accepted_final = False
                    aborted_with_blocking = False
                    c2_revised = False
                    from app.services.cognition_retrieval import load_candidate_context
                    for round_idx in range(1, max_rounds + 1):
                        # 1) Score the current saved version.
                        yield f"data: {json.dumps({'event': 'evaluating', 'round': round_idx})}\n\n"
                        cognition_ledger_text = ''
                        async with aclosing(_yield_with_heartbeat(load_candidate_context(req.project_id,
                            before_chapter_id=req.chapter_id, chapter_outline=revise_outline,
                            chapter_text=current_text, previous_text=evaluation_context,
                            user_instruction=effective_user_instruction))) as owned_stream:
                            async for (_hb_done, _hb_val) in owned_stream:
                                if _hb_done:
                                    cognition_ledger_text = _hb_val
                                else:
                                    yield _hb_val
                        evaluator = ChapterEvaluator()
                        eval_result = None
                        async with aclosing(_yield_with_heartbeat(evaluator.evaluate(chapter_text=current_text, project_id=req.project_id, chapter_id=req.chapter_id, chapter_outline=revise_outline, previous_summary=evaluation_context, following_context=following_context_for_eval, style_profile=resolved_style, user_instruction=effective_user_instruction, cognition_ledger_text=cognition_ledger_text, established_facts=_established_facts_block(previous_text)))) as owned_stream:
                            async for (_hb_done, _hb_val) in owned_stream:
                                if _hb_done:
                                    eval_result = _hb_val
                                else:
                                    yield _hb_val
                        _x4_inc_revise("scored")  # v1.6.0 X4 metric: revise round outcome
                        scored_payload = json.dumps({
                            "event": "scored",
                            "round": round_idx,
                            "overall": eval_result.overall,
                            "issues": len(eval_result.issues),
                            "evaluation_failure": (getattr(eval_result, "failure_reason", None) or "parse_failed")
                            if getattr(eval_result, "parse_failed", False) else None,
                        })
                        yield f"data: {scored_payload}\n\n"
                        # Persist evaluation row (best-effort; never blocks).
                        try:
                            async with async_session_factory() as eval_db:
                                eval_db.add(ChapterEvaluation(
                                    chapter_id=revise_chapter_id,
                                    plot_coherence=eval_result.plot_coherence,
                                    character_consistency=eval_result.character_consistency,
                                    style_adherence=eval_result.style_adherence,
                                    narrative_pacing=eval_result.narrative_pacing,
                                    foreshadow_handling=eval_result.foreshadow_handling,
                                    overall=eval_result.overall,
                                    issues_json=eval_result.issues,
                                    source_json=getattr(eval_result, "chapter_source", None),
                                ))
                                await eval_db.commit()
                        except Exception as persist_err:
                            logger.warning(
                                "C2 auto-revise: failed to persist ChapterEvaluation row (round=%d): %s",
                                round_idx, persist_err,
                            )

                        # ROLLBACK-POLICY: remember the best draft with a REAL
                        # score so the rollback below can weigh it against the
                        # baseline instead of discarding it unseen.
                        if (
                            not getattr(eval_result, "parse_failed", False)
                            and eval_result.overall > 0
                            and (
                                best_draft_score is None
                                or eval_result.overall > best_draft_score
                            )
                        ):
                            best_draft_text = current_text
                            best_draft_score = float(eval_result.overall)

                        acceptance.record(current_text, eval_result)

                        # 2) Rewrite decision; acceptance is settled separately.
                        if not should_revise(eval_result, threshold=threshold):
                            # B2: distinguish "good enough to accept" from
                            # "evaluation untrustworthy, don't act on it" -- the
                            # latter has overall=0, so reporting it as
                            # score_above_threshold is self-contradictory.
                            _parse_failed = getattr(eval_result, "parse_failed", False)
                            _x4_inc_revise(
                                "skipped_parse_failed" if _parse_failed else "skipped"
                            )  # v1.6.0 X4 metric: revise round outcome
                            skipped_payload = json.dumps({
                                "event": "revise_skipped",
                                "reason": (
                                    "evaluation_" + (getattr(eval_result, "failure_reason", None) or "parse_failed") if _parse_failed
                                    else ("score_above_threshold" if acceptance.passed(current_text) else "review_required")
                                ),
                                "overall": eval_result.overall,
                                "threshold": threshold,
                            })
                            yield f"data: {skipped_payload}\n\n"
                            accepted_final = True
                            break

                        # 3) Q2 targeted span revision (QMAI rewriteTarget):
                        # locate issues via their evaluator `quote` and rewrite
                        # only paragraph-±1 spans, splicing the results back
                        # into the current text. An unapplied targeted pass
                        # preserves the candidate for review; it never widens
                        # automatically into a full-chapter rewrite.
                        targeted_text: str | None = None
                        if targeted_revision_enabled():
                            async with aclosing(_yield_with_heartbeat(load_candidate_context(req.project_id,
                                before_chapter_id=req.chapter_id, chapter_outline=revise_outline,
                                chapter_text=current_text, previous_text=evaluation_context,
                                user_instruction=effective_user_instruction))) as owned_stream:
                                async for (_hb_done, _hb_val) in owned_stream:
                                    if _hb_done:
                                        cognition_ledger_text = _hb_val
                                    else:
                                        yield _hb_val
                            try:
                                span_result = None
                                async with aclosing(_yield_with_heartbeat(revise_spans(current_text, eval_result.issues, project_id=req.project_id, chapter_id=req.chapter_id, context=RevisionContext(user_instruction=effective_user_instruction, chapter_outline=revise_outline, previous_context=evaluation_context, following_context=following_context_for_eval, style_profile=resolved_style, cognition_ledger=cognition_ledger_text)))) as owned_stream:
                                    async for (_hb_done, _hb_val) in owned_stream:
                                        if _hb_done:
                                            span_result = _hb_val
                                        else:
                                            yield _hb_val
                                if span_result.spans_revised > 0:
                                    targeted_text = span_result.text
                                    targeted_payload = json.dumps({
                                        "event": "targeted_revision",
                                        "round": round_idx,
                                        "spans_revised": span_result.spans_revised,
                                        "unlocatable_issues": len(span_result.unlocatable_issues),
                                    })
                                    yield f"data: {targeted_payload}\n\n"
                            except Exception as targeted_err:
                                logger.warning(
                                    "C2 targeted revision failed; preserving candidate (round=%d): %s",
                                    round_idx, targeted_err,
                                )
                            if targeted_text is None:
                                _x4_inc_revise("skipped")
                                yield f"data: {json.dumps({'event': 'revise_skipped', 'reason': 'targeted_revision_unapplied', 'round': round_idx, 'message': '未能完成可靠的局部修订，已保留当前候选及评审意见，请审阅。'})}\n\n"
                                break
                        _x4_inc_revise("revised")  # v1.6.0 X4 metric: revise round outcome
                        revising_payload = json.dumps({
                            "event": "revising",
                            "round": round_idx,
                            "overall": eval_result.overall,
                            "threshold": threshold,
                            "mode": "targeted" if targeted_text is not None else "full",
                        })
                        yield f"data: {revising_payload}\n\n"

                        revised_chunks: list[str] = []
                        revise_timed_out = False
                        if targeted_text is not None:
                            revised_chunks.append(targeted_text)
                            yield f"data: {json.dumps({'text': targeted_text, 'revise_round': round_idx})}\n\n"
                        else:
                            # Full-chapter fallback: build revise instruction
                            # and rerun SceneOrchestrator.
                            revise_instr = issues_to_revise_instruction(
                                eval_result, round_idx=round_idx,
                            )
                            merged_instruction = merge_revise_into_user_instruction(
                                effective_user_instruction, revise_instr,
                            )
                            revise_orchestrator = SceneOrchestrator()
                            try:
                                async with asyncio.timeout(900):  # 15min hard cap per round
                                    async with async_session_factory() as revise_db:
                                        try:
                                            async with aclosing(revise_orchestrator.orchestrate_chapter_stream(project_id=req.project_id, volume_id=resolved_volume_id, chapter_idx=resolved_chapter_idx, db=None, chapter_id=revise_chapter_id, user_instruction=merged_instruction, target_words=effective_target_words, n_scenes_hint=req.n_scenes_hint, on_scene_start=_on_scene_start)) as owned_stream:
                                                async for chunk in owned_stream:
                                                    while _pending_scene_events:
                                                        yield _pending_scene_events.pop(0)
                                                    if chunk:
                                                        revised_chunks.append(chunk)
                                                    yield f"data: {json.dumps({'text': chunk, 'revise_round': round_idx})}\n\n"
                                            while _pending_scene_events:
                                                yield _pending_scene_events.pop(0)
                                        except Exception as revise_scene_err:
                                            # Root-cause policy: do NOT lottery-fallback to a different
                                            # generator when we are trying to meet a strict quality bar.
                                            # Scene-mode failures must surface as repair-required so
                                            # upstream planning/contract issues get fixed deterministically.
                                            logger.warning(
                                                "C2 auto-revise: SceneOrchestrator failed; aborting revise loop round=%d err=%s",
                                                round_idx,
                                                type(revise_scene_err).__name__,
                                            )
                                            err_payload = json.dumps({
                                                "event": "revise_error",
                                                "round": round_idx,
                                                "reason": "scene_orchestrator_failed",
                                                **generation_error_payload(revise_scene_err, "revision"),
                                            }, ensure_ascii=False)
                                            yield f"data: {err_payload}\n\n"
                                            revise_timed_out = True
                                            # 2026-11-17 审计:这个 break 跳出的是外层
                                            # 回合循环,会跳过 1339 行的
                                            # ``aborted_with_blocking = True``,于是
                                            # 1698+ 的 blocking 回滚 / best-draft 保护
                                            # 不执行,失败回合的半成品状态留在正文里。
                                            aborted_with_blocking = True
                                            break
                            except asyncio.TimeoutError:
                                revise_timed_out = True
                                logger.warning(
                                    "C2 auto-revise round %d timed out after 900s; aborting loop",
                                    round_idx,
                                )
                                err_payload = json.dumps({
                                    "event": "revise_error",
                                    "round": round_idx,
                                    "reason": "timeout",
                                    "timeout_seconds": 900,
                                })
                                yield f"data: {err_payload}\n\n"
                        revised_text = "".join(revised_chunks)
                        if revise_timed_out or not revised_text:
                            if not revise_timed_out:
                                logger.warning(
                                    "C2 auto-revise round %d produced empty text; aborting loop",
                                    round_idx,
                                )
                                err_payload = json.dumps({
                                    "event": "revise_error",
                                    "round": round_idx,
                                    "reason": "empty_briefs",
                                })
                                yield f"data: {err_payload}\n\n"
                            aborted_with_blocking = True
                            break

                        # 4) Overwrite chapter content with the revised version.
                        try:
                            async with async_session_factory() as save_db2:
                                ch2 = await save_db2.get(_Chapter, revise_chapter_id)
                                if ch2 is not None:
                                    # PR-CH-TRUNCATION: the revised text can also end
                                    # mid-sentence (scene_writer hit max_tokens). Keep the
                                    # text but downgrade to draft so a dangling revision is
                                    # never silently promoted to completed (parity with the
                                    # initial-save path in _persist_chapter_now).
                                    revised_text, _rev_leak = sanitize_prose(revised_text)
                                    if _rev_leak:
                                        logger.warning(
                                            "prose_sanitizer stripped meta leakage (revise) chapter_id=%s hits=%s",
                                            revise_chapter_id, _rev_leak,
                                        )
                                    _rev_truncated = looks_truncated(revised_text)
                                    ch2.content_text = revised_text
                                    ch2.word_count = len(revised_text)
                                    ch2.status = acceptance.status(revised_text)
                                    await save_db2.commit()
                                    last_saved_quality_meta = None
                                    saved_payload = json.dumps({
                                        "status": "saved",
                                        "chapter_id": revise_chapter_id,
                                        "word_count": len(revised_text),
                                        "revise_round": round_idx,
                                        **({"truncated": True} if _rev_truncated else {}),
                                    })
                                    yield f"data: {saved_payload}\n\n"
                        except Exception as save2_err:
                            logger.warning(
                                "C2 auto-revise round %d save failed: %s",
                                round_idx, save2_err,
                            )
                            aborted_with_blocking = True
                            break
                        current_text = revised_text
                        c2_revised = True
                    else:
                        # for-else: ran out of rounds without breaking. Emit a
                        # final scored event for the last write so the UI sees
                        # the converged score even if we never met threshold.
                        try:
                            evaluator = ChapterEvaluator()
                            final_eval = None
                            async with aclosing(_yield_with_heartbeat(evaluator.evaluate(chapter_text=current_text, project_id=req.project_id, chapter_id=req.chapter_id, chapter_outline=revise_outline, previous_summary=evaluation_context, following_context=following_context_for_eval, style_profile=resolved_style, user_instruction=effective_user_instruction, cognition_ledger_text=cognition_ledger_text, established_facts=_established_facts_block(previous_text)))) as owned_stream:
                                async for (_hb_done, _hb_val) in owned_stream:
                                    if _hb_done:
                                        final_eval = _hb_val
                                    else:
                                        yield _hb_val
                            acceptance.record(current_text, final_eval)
                            final_payload = json.dumps({
                                "event": "scored",
                                "round": max_rounds + 1,
                                "overall": final_eval.overall,
                                "issues": len(final_eval.issues),
                                "rounds_exhausted": True,
                            })
                            yield f"data: {final_payload}\n\n"
                            try:
                                async with async_session_factory() as eval_db2:
                                    eval_db2.add(ChapterEvaluation(
                                        chapter_id=revise_chapter_id,
                                        plot_coherence=final_eval.plot_coherence,
                                        character_consistency=final_eval.character_consistency,
                                        style_adherence=final_eval.style_adherence,
                                        narrative_pacing=final_eval.narrative_pacing,
                                        foreshadow_handling=final_eval.foreshadow_handling,
                                        overall=final_eval.overall,
                                        issues_json=final_eval.issues,
                                        source_json=getattr(final_eval, "chapter_source", None),
                                    ))
                                    await eval_db2.commit()
                            except Exception:
                                logger.warning("C2 auto-revise final eval persist failed", exc_info=True)

                            # ROLLBACK-POLICY: the final revision is scored
                            # here (never inside the round loop) -- track it
                            # too, or the incident's 7.9 draft is invisible to
                            # the rollback decision.
                            if (
                                not getattr(final_eval, "parse_failed", False)
                                and final_eval.overall > 0
                                and (
                                    best_draft_score is None
                                    or final_eval.overall > best_draft_score
                                )
                            ):
                                best_draft_text = current_text
                                best_draft_score = float(final_eval.overall)

                            # Only accept the final text when it meets the
                            # threshold and has no blocking contract violations.
                            if not should_revise(final_eval, threshold=threshold):
                                accepted_final = True
                            else:
                                aborted_with_blocking = True

                            # C4-4: cascade auto-regenerate trigger.
                            # When auto-revise exhausts max_rounds without
                            # meeting `threshold`, the chapter has structural
                            # issues that no further chapter-local rewrite
                            # will fix -- the upstream outline / character /
                            # foreshadow entities are likely the root cause.
                            # Planner + cascade celery queue handle that out
                            # of band; SSE "cascade_triggered" notifies the
                            # UI so it can poll cascade_tasks for status.
                            #
                            # Best-effort: any failure here is logged and the
                            # SSE stream continues to the [DONE] terminator.
                            try:
                                from app.services.cascade_planner import (
                                    plan_cascade,
                                    should_trigger_cascade,
                                )
                                from app.tasks.cascade import (
                                    enqueue_cascade_candidates,
                                )
                                # Look up the row we just persisted via the
                                # natural ordering. We avoid ``refresh`` on
                                # the original session because it has
                                # already exited the ``async with`` scope.
                                final_eval_row_id = None
                                try:
                                    async with async_session_factory() as cdb_lookup:
                                        from sqlalchemy import select as _select
                                        latest_id = (await cdb_lookup.execute(
                                            _select(ChapterEvaluation.id)
                                            .where(ChapterEvaluation.chapter_id == revise_chapter_id)
                                            .order_by(ChapterEvaluation.created_at.desc())
                                            .limit(1)
                                        )).scalar_one_or_none()
                                        final_eval_row_id = (
                                            str(latest_id) if latest_id else None
                                        )
                                except Exception:
                                    logger.warning(
                                        "C4 cascade: failed to look up final evaluation id",
                                        exc_info=True,
                                    )

                                if (
                                    final_eval_row_id is not None
                                    # B2 hotfix: a parse_failed evaluation
                                    # carries sentinel overall=0, not a real
                                    # verdict -- never plan a cascade
                                    # regeneration on garbage signal.
                                    and not getattr(final_eval, "parse_failed", False)
                                    and should_trigger_cascade(
                                        overall=final_eval.overall,
                                        rounds_exhausted=True,
                                        threshold=threshold,
                                    )
                                ):
                                    async with async_session_factory() as cdb:
                                        candidates = await plan_cascade(
                                            db=cdb,
                                            project_id=req.project_id,
                                            source_chapter_id=revise_chapter_id,
                                            source_evaluation_id=final_eval_row_id,
                                            issues_json=final_eval.issues,
                                        )
                                        cascade_result = await enqueue_cascade_candidates(
                                            cdb,
                                            candidates,
                                            caller="generate.auto_revise.exhausted",
                                        )
                                    cascade_payload = json.dumps({
                                        "event": "cascade_triggered",
                                        "chapter_id": revise_chapter_id,
                                        "evaluation_id": final_eval_row_id,
                                        "overall": final_eval.overall,
                                        "threshold": threshold,
                                        "candidates_planned": len(candidates),
                                        "tasks_inserted": len(
                                            cascade_result["inserted"]
                                        ),
                                        "duplicates": cascade_result["duplicates"],
                                        "dispatched": cascade_result["dispatched"],
                                        "task_ids": cascade_result["inserted"],
                                    })
                                    yield f"data: {cascade_payload}\n\n"
                            except Exception:
                                logger.warning(
                                    "C4 cascade trigger failed", exc_info=True,
                                )
                        except Exception:
                            logger.warning("C2 auto-revise final eval call failed", exc_info=True)
                except Exception as revise_err:
                    logger.warning(
                        "C2 auto-revise loop failed: %s", type(revise_err).__name__,
                    )
                    aborted_with_blocking = True
                    yield f"data: {json.dumps({'event': 'revise_error', **generation_error_payload(revise_err, 'revision')})}\n\n"

                if c2_revised and accepted_final and not aborted_with_blocking and not req.skip_polish:
                    try:
                        pre_final_quality_report = analyze_chinese_prose_mechanics(current_text)
                        yield f"data: {json.dumps({'event': 'quality_check', 'status': 'passed' if pre_final_quality_report.passed else 'needs_rewrite', 'passed': pre_final_quality_report.passed, 'report': pre_final_quality_report.to_safe_dict()}, ensure_ascii=False)}\n\n"
                        if not pre_final_quality_report.passed:
                            yield f"data: {json.dumps({'event': 'quality_rewrite_start', 'rounds': 2}, ensure_ascii=False)}\n\n"
                        from app.db.session import async_session_factory
                        async with async_session_factory() as quality_db2:
                            final_quality_result = None
                            async with aclosing(_yield_with_heartbeat(apply_chapter_quality_gate(text=current_text, db=None, project_id=req.project_id, chapter_id=req.chapter_id, target_word_count=target_words, skip_polish=False))) as owned_stream:
                                async for (_hb_done, _hb_val) in owned_stream:
                                    if _hb_done:
                                        final_quality_result = _hb_val
                                    else:
                                        yield _hb_val
                        final_quality_meta = final_quality_result.to_safe_metadata()
                        if final_quality_result.status != "passed":
                            _persist_on_block = _cfg.QUALITY_GATE_PERSIST_ON_BLOCK
                            _pob_text2 = final_quality_result.final_text or ""
                            if _persist_on_block and _pob_text2.strip():
                                if isinstance(final_quality_meta, dict):
                                    final_quality_meta = {**final_quality_meta, "persisted_on_block": True}
                                yield f"data: {json.dumps({'event': 'quality_failed', 'status': final_quality_result.status, 'reason': final_quality_result.warning_reason, 'rounds': final_quality_result.rewrite_rounds, 'persisted_on_block': True, 'report': final_quality_result.final_report.to_safe_dict()}, ensure_ascii=False)}\n\n"
                                current_text = _pob_text2
                                full_text = current_text
                                _chsave_taskb = asyncio.create_task(_persist_chapter_now(current_text, final_quality_meta))
                                _CHAPTER_SAVE_BG_TASKS.add(_chsave_taskb)
                                _chsave_taskb.add_done_callback(_CHAPTER_SAVE_BG_TASKS.discard)
                                try:
                                    await _await_chapter_save(_chsave_taskb)
                                except Exception:
                                    pass
                            else:
                                accepted_final = False
                                aborted_with_blocking = True
                                yield f"data: {json.dumps({'event': 'quality_failed', 'status': final_quality_result.status, 'reason': final_quality_result.warning_reason, 'rounds': final_quality_result.rewrite_rounds, 'report': final_quality_result.final_report.to_safe_dict()}, ensure_ascii=False)}\n\n"
                        elif final_quality_result.rewrite_rounds > 0:
                            yield f"data: {json.dumps({'event': 'quality_rewrite_done', 'status': final_quality_result.status, 'rounds': final_quality_result.rewrite_rounds, 'content_text': final_quality_result.final_text, 'report': final_quality_result.final_report.to_safe_dict()}, ensure_ascii=False)}\n\n"

                        if final_quality_result.status == "passed" and final_quality_result.final_text != current_text:
                            current_text = final_quality_result.final_text
                            full_text = current_text
                            _chsave_task2 = asyncio.create_task(_persist_chapter_now(current_text, final_quality_meta))
                            _CHAPTER_SAVE_BG_TASKS.add(_chsave_task2)
                            _chsave_task2.add_done_callback(_CHAPTER_SAVE_BG_TASKS.discard)
                            try:
                                _saved_id2, _wc2, _no_target2 = await _await_chapter_save(_chsave_task2)
                                if _no_target2:
                                    logger.warning(
                                        "Auto-save chapter: final quality gate no target row (chapter_id=%s vol=%s idx=%s)",
                                        req.chapter_id, resolved_volume_id, resolved_chapter_idx,
                                    )
                                    _inc_chapter_auto_save("chapter", "failure", "no_target_row")
                                    yield f"data: {json.dumps({'status': 'save_failed', 'kind': 'chapter', 'reason': 'no_target_row', 'chapter_id': req.chapter_id, 'volume_id': str(resolved_volume_id) if resolved_volume_id else None, 'chapter_idx': resolved_chapter_idx})}\n\n"
                                else:
                                    yield f"data: {json.dumps({'status': 'saved', 'chapter_id': _saved_id2, 'word_count': _wc2, 'quality_gate': final_quality_result.status})}\n\n"
                            except asyncio.CancelledError:
                                raise
                            except Exception as save2_final_err:
                                logger.warning(
                                    "Auto-save chapter final quality gate FAILED (chapter_id=%s vol=%s idx=%s): %s",
                                    req.chapter_id, resolved_volume_id, resolved_chapter_idx,
                                    save2_final_err, exc_info=True,
                                )
                                _inc_chapter_auto_save(
                                    "chapter", "failure", type(save2_final_err).__name__
                                )
                    except Exception as quality2_err:
                        logger.warning(
                            "chapter final quality gate failed after auto-revise; rolling back: %s",
                            quality2_err,
                            exc_info=True,
                        )
                        accepted_final = False
                        aborted_with_blocking = True
                        yield f"data: {json.dumps({'event': 'quality_failed', 'status': 'blocked', 'reason': 'quality_gate_exception', 'error_class': type(quality2_err).__name__}, ensure_ascii=False)}\n\n"

                # If the revise loop aborted due to blocking issues / timeout /
                # rounds exhausted, rollback chapter.content_text so a known-bad
                # draft does not become the active chapter.
                if (not accepted_final) and aborted_with_blocking:
                    try:
                        _rb_persist_on_block = _cfg.QUALITY_GATE_PERSIST_ON_BLOCK
                        # ROLLBACK-POLICY (2026-07-27): only pay the baseline
                        # score lookup when a floor-eligible scored best draft
                        # exists AND a non-empty baseline could win over it.
                        _rb_baseline_score = None
                        if (
                            (baseline_text_before_run or "").strip()
                            and (best_draft_text or "").strip()
                            and best_draft_score is not None
                            and best_draft_score >= _cfg.ROLLBACK_KEEP_MIN_SCORE
                        ):
                            _rb_baseline_score = await _load_valid_baseline_score(
                                req.chapter_id,
                                baseline_updated_at=baseline_updated_at_before_run,
                                run_started_at=run_started_at,
                            )
                        _rb_text, _rb_kept_best, _rb_keep_reason = resolve_rollback_decision(
                            baseline_text=baseline_text_before_run or "",
                            current_text=current_text or "",
                            persist_on_block=_rb_persist_on_block,
                            best_draft_text=best_draft_text,
                            best_draft_score=best_draft_score,
                            baseline_score=_rb_baseline_score,
                            keep_min_score=_cfg.ROLLBACK_KEEP_MIN_SCORE,
                        )
                        from app.db.session import async_session_factory
                        from app.models.project import Chapter as _ChapterRollback
                        async with async_session_factory() as rb_db:
                            ch_rb = await rb_db.get(_ChapterRollback, req.chapter_id)
                            if ch_rb is not None:
                                ch_rb.content_text = _rb_text
                                ch_rb.word_count = len(ch_rb.content_text or "")
                                if _rb_keep_reason is not None:
                                    # Kept best draft is sub-threshold by
                                    # definition: persist as draft, never
                                    # masquerade as completed.
                                    ch_rb.status = "draft"
                                else:
                                    # A kept best-draft stays usable; a true wipe
                                    # (empty result) OR a mid-sentence truncated
                                    # draft downgrades to draft rather than passing
                                    # off a dangling chapter as completed.
                                    ch_rb.status = (
                                        baseline_status_before_run
                                        if _rb_text == baseline_text_before_run and (_rb_text or "").strip()
                                        else acceptance.status(_rb_text)
                                    )
                                await rb_db.commit()
                                _rb_payload = {
                                    "event": "rollback_applied",
                                    "reason": "auto_revise_blocking_or_timeout",
                                    "chapter_id": req.chapter_id,
                                    "kept_best_draft": _rb_kept_best,
                                }
                                if _rb_keep_reason is not None:
                                    _rb_payload["keep_reason"] = _rb_keep_reason
                                    _rb_payload["best_draft_score"] = best_draft_score
                                    _rb_payload["baseline_score"] = _rb_baseline_score
                                yield f"data: {json.dumps(_rb_payload)}\n\n"
                    except Exception:
                        logger.warning("Auto-revise rollback failed", exc_info=True)

                # Q3 v1.9.1 (review fix): settle the ledger ingestion source.
                # An aborted/rolled-back draft must never reach the ledger;
                # otherwise the final converged text (post-revise, post-polish)
                # supersedes the initial draft saved above. Only upgrade when
                # something was actually persisted (initial save succeeded —
                # cognition_ingest_text already set — or a revise round
                # overwrote the chapter).
                if (not accepted_final) and aborted_with_blocking:
                    cognition_ingest_text = None
                elif cognition_ingest_text is not None or c2_revised:
                    cognition_ingest_text = current_text

            # A stopped rewrite loop is not proof of acceptance. Bind the
            # verdict to the exact persisted body, including post-polish changes.
            if acceptance.required and full_text and not partial_draft_only and req.chapter_id:
                from app.db.session import async_session_factory
                from app.models.project import ChapterVersion
                async with async_session_factory() as verdict_db:
                    verdict_chapter = await verdict_db.get(Chapter, req.chapter_id)
                    if verdict_chapter is not None:
                        persisted = verdict_chapter.content_text or ""
                        # A restored author baseline keeps its pre-run status.
                        restored = (persisted == baseline_text_before_run
                                    and not accepted_final and aborted_with_blocking)
                        if not restored and persisted == sanitize_prose(current_text)[0]:
                            verdict_chapter.status = acceptance.status(persisted, last_saved_quality_meta)
                            # Final acceptance changes status, not author text.
                            if verdict_chapter.status != "completed":
                                cognition_ingest_text = None
                            if verdict_chapter.status == "completed":
                                from sqlalchemy import update as _version_update
                                version = await verdict_db.scalar(
                                    select(ChapterVersion)
                                    .where(ChapterVersion.chapter_id == verdict_chapter.id,
                                           ChapterVersion.content_text == persisted)
                                    .order_by(ChapterVersion.created_at.desc()).limit(1)
                                )
                                await verdict_db.execute(
                                    _version_update(ChapterVersion)
                                    .where(ChapterVersion.chapter_id == verdict_chapter.id,
                                           ChapterVersion.is_active == 1).values(is_active=0)
                                )
                                if version is None:
                                    version = ChapterVersion(
                                        chapter_id=verdict_chapter.id, branch_name="main",
                                        content_text=persisted, word_count=len(persisted),
                                        source="ai_generation", metadata_json={"caller": "chapter_acceptance"},
                                    )
                                    verdict_db.add(version)
                                version.is_active = 1
                            await verdict_db.commit()
                        yield f"data: {json.dumps({'event': 'chapter_acceptance', 'chapter_id': req.chapter_id, 'status': verdict_chapter.status})}\n\n"

            # Q3 v1.9.1 (review fix): character cognition ledger ingestion runs
            # exactly ONCE per stream, here, after the final chapter text is
            # settled. Best-effort: never blocks stream completion.
            if cognition_ingest_text:
                try:
                    from app.db.session import async_session_factory
                    from app.services.character_cognition import extract_and_update
                    async with async_session_factory() as cog_ingest_db:
                        await extract_and_update(
                            cog_ingest_db, req.project_id, cognition_ingest_text, chapter_id=req.chapter_id,
                        )
                except Exception as cog_err:
                    logger.warning(
                        "Cognition ledger update after final save failed: %s",
                        cog_err,
                    )
                # C2/F1: recompute whole-book style stats in the background
                # (deterministic, non-blocking, celery-optional).
                try:
                    from app.services.style_stat import dispatch_style_recompute
                    dispatch_style_recompute(req.project_id, caller="api.generate.chapter")
                except Exception as style_err:
                    logger.warning("Style stats dispatch failed: %s", style_err)

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.warning("Generation failed: %s", type(e).__name__)
            yield f"data: {json.dumps(generation_error_payload(e, 'chapter'), ensure_ascii=False)}\n\n"
            # Terminate like every other exit path so clients waiting on the
            # [DONE] sentinel don't hang until their own timeout.
            yield "data: [DONE]\n\n"

    async def protected_stream():
        from contextlib import aclosing
        from app.db import session
        from app.services.production.writer import guarded_stream
        try:
            async with aclosing(guarded_stream(
                session.engine, session.async_session_factory, req.project_id, event_stream,
                chapter_id=req.chapter_id, volume_id=req.volume_id, chapter_idx=req.chapter_idx,
                expected_updated_at=getattr(chapter, "updated_at", None),
            )) as stream:
                async for frame in stream:
                    yield frame
        except Exception as exc:
            yield f"data: {json.dumps(generation_error_payload(exc, 'chapter'), ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        protected_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/outline")
async def generate_outline(
    req: GenerateOutlineRequest,
    db: AsyncSession = Depends(get_db),
    staged_stream: int = Query(0),
) -> StreamingResponse:
    """Generate outline at specified level via SSE streaming."""
    book_save_snapshot = None
    if req.level == "book" and req.project_id:
        from app.services.book_outline_save import snapshot_book_outline
        book_save_snapshot = await snapshot_book_outline(db, req.project_id)
    # Pre-fetch all DB data before creating the generator (same pattern as generate_chapter)
    book_outline: dict = {}
    volume_outline: dict = {}
    project_settings: dict = {}
    if req.project_id:
        try:
            project = await db.get(Project, req.project_id)
            if project and isinstance(project.settings_json, dict):
                project_settings = project.settings_json
        except Exception as _proj_settings_err:
            logger.warning("Outline project settings load failed: %s", _proj_settings_err)

    if req.level == "volume":
        if req.parent_outline_id:
            parent = await db.get(Outline, req.parent_outline_id)
            book_outline = parent.content_json if parent else {}
        elif req.project_id:
            book_result = await db.execute(
                select(Outline).where(
                    Outline.project_id == req.project_id,
                    Outline.level == "book",
                )
            )
            parent = book_result.scalar_one_or_none()
            book_outline = parent.content_json if parent else {}

    elif req.level == "chapter":
        if req.project_id:
            book_result = await db.execute(
                select(Outline).where(
                    Outline.project_id == req.project_id,
                    Outline.level == "book",
                ).order_by(Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1)
            )
            book_ol = book_result.scalar_one_or_none()
            if book_ol:
                book_outline = book_ol.content_json or {}

        if req.parent_outline_id:
            vol_ol = await db.get(Outline, req.parent_outline_id)
            if vol_ol:
                volume_outline = vol_ol.content_json or {}
                if vol_ol.parent_id:
                    linked_book = await db.get(Outline, vol_ol.parent_id)
                    if linked_book and linked_book.project_id == vol_ol.project_id and linked_book.level == "book":
                        book_outline = linked_book.content_json or {}

    chapter_word_target: int | None = None
    target_chapter = None
    if req.level == "chapter" and req.project_id and req.chapter_idx and volume_outline:
        from app.models.project import Volume as _TargetVolume
        from sqlalchemy import cast, String
        target_query = select(Chapter).join(_TargetVolume, Chapter.volume_id == _TargetVolume.id).where(
            _TargetVolume.project_id == req.project_id, Chapter.chapter_idx == req.chapter_idx,
        )
        if volume_outline.get("volume_id") is not None:
            target_query = target_query.where(cast(_TargetVolume.id, String) == str(volume_outline["volume_id"]))
        else:
            target_query = target_query.where(cast(_TargetVolume.volume_idx, String) == str(volume_outline.get("volume_idx")))
        target_chapters = (await db.scalars(target_query.limit(2))).all()
        if len(target_chapters) > 1:
            raise HTTPException(status_code=409, detail="同卷章号重复，请先整理章节后再生成细纲")
        target_chapter = target_chapters[0] if target_chapters else None
        if target_chapter is not None:
            chapter_word_target = resolve_chapter_target_word_count(
                target_chapter.target_word_count, resolve_chapter_words(project_settings),
            )

    # Saved chapter plans and the actual predecessor are resolved together in
    # the chapter snapshot below, then rechecked at save time.
    previous_chapter_summary: str = ""
    chapter_breakdown_entry: dict | None = None

    # Plans are read from their outlines; prose references require a current
    # author-confirmed source and an explicit prior-chapter/volume boundary.
    active_foreshadow_block: str = ""
    if req.project_id and req.level in ("volume", "chapter"):
        from app.services.foreshadow_lifecycle import (
            load_active_foreshadows_for_context, format_active_foreshadows_for_prompt,
        )
        _upper_idx = None
        if req.level == "chapter":
            if target_chapter is None:
                raise HTTPException(409, detail="目标章节不可用，请刷新后核对分卷与章号")
            _upper_idx = target_chapter.global_idx
        try:
            _af_items = await load_active_foreshadows_for_context(
                db, req.project_id, _upper_idx, limit=20,
                target_volume_idx=req.volume_idx if req.level == "volume" else None,
            )
        except ValueError:
            raise HTTPException(409, detail="伏笔计划或章节位置暂时无法核实，请检查大纲和卷章顺序后重试") from None
        except Exception:
            logger.warning("Foreshadow planning context unavailable", exc_info=True)
            raise HTTPException(503, detail="伏笔资料暂时无法读取，请稍后重试") from None
        active_foreshadow_block = format_active_foreshadows_for_prompt(_af_items)

    # PR-OL10: compute scale (n_volumes / chapters_per_volume / chapter_words)
    # from the project's target_word_count + settings_json so prompts inject
    # hard numeric constraints instead of free-form "2-8 卷".
    project_scale: dict | None = None
    if req.project_id:
        try:
            from app.models.project import Project as _Project
            _proj = await db.get(_Project, req.project_id)
            if _proj is not None:
                _twc = int(_proj.target_word_count or 0)
                _settings = _proj.settings_json or {}
                if not isinstance(_settings, dict):
                    _settings = {}

                # 每章字数 / 每卷章数统一走 chapter_target_words(规范键
                # ``target_chapter_words``/``chapters_per_volume_*`` 与简写键
                # ``chapter_words``/``chapters_per_volume`` 双侧回退),避免只认一套
                # 让用户设的值被静默忽略。
                project_scale = build_planning_scale(_twc, _settings)
        except Exception as _scale_err:
            logger.warning("PR-OL10 compute_scale failed: %s", _scale_err)
            project_scale = None

    # Resolve style: explicit style_id > project settings > auto-resolve
    style_instruction = ""
    if not req.style_id and project_settings:
        req.style_id = _settings_style_profile_id(project_settings)
    if req.style_id:
        try:
            from app.models.project import StyleProfile
            from app.services.style_compiler import compile_style
            profile = await db.get(StyleProfile, req.style_id)
            if profile:
                style_instruction = compile_style(profile)
        except Exception:
            pass
    if not style_instruction and req.project_id:
        try:
            from app.services.style_runtime import resolve_style_prompt
            style_instruction = await resolve_style_prompt(db, req.project_id) or ""
        except Exception:
            pass

    # Resolve extracted plot structure from project settings. Do not route project
    # creation or normal outline generation directly through a reference book.
    structure_instruction = ""
    structure_book_id = _settings_structure_book_id(project_settings) if project_settings else None
    if structure_book_id:
        try:
            from app.models.project import ReferenceBook
            from app.services.plot_structure import compile_structure_prompt
            from app.services.style_runtime import (
                get_dossier_block,
                scrub_reference_proper_nouns,
            )
            structure_book = await db.get(ReferenceBook, structure_book_id)
            if structure_book is not None:
                # Prefer the consolidated dossier structure block.
                structure_instruction = get_dossier_block(structure_book, "structure_block")
                if not structure_instruction:
                    plot_structure = (structure_book.metadata_json or {}).get("plot_structure")
                    if isinstance(plot_structure, dict) and "error" not in plot_structure:
                        structure_instruction = compile_structure_prompt(plot_structure) or ""
                if structure_instruction:
                    # Scrub source-book proper nouns (西泽尔/密涅瓦-class leaks).
                    structure_instruction = scrub_reference_proper_nouns(
                        structure_instruction, structure_book
                    )
        except Exception as _structure_err:
            logger.warning("Outline plot structure resolve failed: %s", _structure_err)

    # Build Anti-AI instruction from filter words
    anti_ai_instruction = ""
    try:
        from app.models.project import FilterWord
        fw_result = await db.execute(
            select(FilterWord).where(FilterWord.enabled == 1).limit(50)
        )
        filter_words = [fw.word for fw in fw_result.scalars().all()]
        if filter_words:
            anti_ai_instruction = f"\n\n【禁用词】严禁使用以下词汇：{'、'.join(filter_words)}\n用更自然、更口语化的表达替代。避免四字成语堆砌。句式要多变，长短交替。"
    except Exception:
        pass

    # Combine user input with style and anti-AI instructions
    enhanced_input = req.user_input
    if style_instruction:
        enhanced_input = f"{style_instruction}\n\n---\n\n用户创意：{req.user_input}"
    if structure_instruction:
        enhanced_input = f"{enhanced_input}\n\n{structure_instruction}"
    if anti_ai_instruction:
        enhanced_input += anti_ai_instruction

    async def event_stream() -> AsyncGenerator[str, None]:
        collected_text = []
        try:
            # PR-USAGE-LOGMETA: bind project_id so every router call inside
            # OutlineGenerator threads _log_meta and lands in llm_call_logs +
            # usage_quotas (was silently bypassed before).
            # PR-CHAPTER-NAMING: load chapter naming directive from project's style profile
            _chapter_naming_directive = ""
            if req.project_id:
                try:
                    from app.services.outline_generator import format_chapter_naming_directive as _fmt_naming
                    from app.models.project import Project as _Project, StyleProfile as _StyleProfile
                    _proj = await db.get(_Project, req.project_id)
                    if _proj is not None:
                        _settings = _proj.settings_json or {}
                        _style_ref = _settings.get("style_reference") or {}
                        _profile_id = _style_ref.get("profile_id") or _settings.get("default_style_profile_id")
                        if _profile_id:
                            _profile = await db.get(_StyleProfile, _profile_id)
                            if _profile is not None:
                                _chapter_naming_directive = _fmt_naming(_profile.config_json) or ""
                except Exception as _cnd_err:
                    logger.warning("PR-CHAPTER-NAMING directive load failed: %s", _cnd_err)
            yield f"data: {json.dumps({'status': 'generating', 'level': req.level})}\n\n"

            blocked_layers: list[str] = []
            if req.level == "volume" and not has_meaningful_outline_content(book_outline):
                blocked_layers.append("book")
            elif req.level == "chapter":
                if not has_meaningful_outline_content(book_outline):
                    blocked_layers.append("book")
                if not has_meaningful_outline_content(volume_outline):
                    blocked_layers.append("volume")

            if blocked_layers:
                labels = "、".join(LAYER_LABELS.get(layer, layer) for layer in blocked_layers)
                yield f"data: {json.dumps({'event': 'generation_blocked', 'status': 'blocked', 'reason': 'outline_chain_incomplete', 'message': f'缺少：{labels}', 'missing_layers': blocked_layers}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # BUGFIX (2026-06-25): force the shared model-router singleton to
            # load its DB config BEFORE OutlineGenerator grabs it via the sync
            # get_model_router(). Inside this running SSE event loop the sync
            # getter takes the `loop.is_running()` branch and returns WITHOUT a
            # DB load, falling back to env providers. This deployment configures
            # LLMs in the DB (env keys empty), so the unhealed router has zero
            # providers and outline routing raised "No model configured for
            # 'outline_book'" into the stream (the "回退到 prompt" symptom).
            # Awaiting the async getter heals the singleton both functions share.
            from app.services.model_router import get_model_router_async
            await get_model_router_async()
            generator = OutlineGenerator(project_id=req.project_id, chapter_naming_directive=_chapter_naming_directive)

            # PR-OUTLINE-STAGED-PERSIST-STRUCT: nonlocal-style payload
            # captured from the staged-stream `done` event.
            _staged_book_done_payload: dict | None = None

            if req.level == "book":
                # Release preparation reads before the slow model call.
                await db.rollback()
                # v1.4.2 Task B: opt in to structured staged SSE events.
                # Body field wins over the query param when explicitly set.
                want_staged = (
                    req.staged_stream
                    if req.staged_stream is not None
                    else bool(staged_stream)
                )
                if want_staged:
                    stage_iter = await generator.generate_book_outline(
                        user_input=enhanced_input,
                        stream=True,
                        staged=True,
                        scale=project_scale,  # PR-OL10
                    )
                    async with aclosing(stage_iter) as owned_stream:
                        async for event in owned_stream:
                            if not isinstance(event, dict):
                                # Defensive: legacy stream fall-through.
                                collected_text.append(str(event))
                                yield f"data: {json.dumps({'text': str(event)})}\n\n"
                                continue
                            kind = event.get("event")
                            if kind == "error" or event.get("error") or event.get("status") in ("failed", "save_failed"):
                                raise ValueError("整书大纲生成失败，已停止保存")
                            if kind == "stage_chunk":
                                delta = event.get("delta", "")
                                if delta:
                                    collected_text.append(delta)
                            elif kind == "done":
                                full = event.get("full_outline", "")
                                if not isinstance(full, str) or not full.strip():
                                    raise ValueError("分阶段大纲完成事件缺少有效正文，已停止保存")
                                if full:
                                    # Replace the per-chunk accumulator with the
                                    # canonical reassembled text so auto-save keeps
                                    # the in-order 9-section document.
                                    collected_text = [full]
                                # PR-OUTLINE-STAGED-PERSIST-STRUCT: capture
                                # done-event payload so persist_outline_now can
                                # promote volume_plan / structured to top-level.
                                _staged_book_done_payload = {
                                    "volume_plan": event.get("volume_plan"),
                                    "structured": event.get("structured") or {},
                                }
                            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if _staged_book_done_payload is None:
                        raise ValueError("分阶段全书大纲未完成，已停止保存不完整结果")
                else:
                    async with aclosing(await generator.generate_book_outline(user_input=enhanced_input, stream=True, staged=False, scale=project_scale)) as owned_stream:
                        async for chunk in owned_stream:
                            collected_text.append(chunk)
                            yield f"data: {json.dumps({'text': chunk})}\n\n"

            elif req.level == "volume":
                volume_creation_snapshot = None
                if req.project_id:
                    from app.services.volume_creation import snapshot_creation
                    volume_creation_snapshot = await snapshot_creation(
                        db, req.project_id, str(parent.id), req.volume_idx or 1, req.planned_volume_count,
                    )
                    await db.rollback()
                _vol_user_notes = req.user_input or ""
                if active_foreshadow_block:
                    _vol_user_notes = (_vol_user_notes + "\n\n" + active_foreshadow_block).strip()
                async with aclosing(await generator.generate_volume_outline(book_outline=volume_creation_snapshot.book_data if volume_creation_snapshot else book_outline, volume_idx=req.volume_idx or 1, user_notes=_vol_user_notes, stream=True)) as owned_stream:
                    async for chunk in owned_stream:
                        collected_text.append(chunk)
                        yield f"data: {json.dumps({'text': chunk})}\n\n"

            elif req.level == "chapter":
                chapter_save_snapshot = None
                _chapter_breakdown_entry = chapter_breakdown_entry
                if req.project_id:
                    from app.services.chapter_outline_save import snapshot_chapter_outline
                    chapter_save_snapshot = await snapshot_chapter_outline(
                        db, req.project_id, req.parent_outline_id,
                        req.chapter_idx if req.chapter_idx is not None else 1,
                    )
                    await db.rollback()
                    _chapter_breakdown_entry = chapter_save_snapshot.chapter_plan
                # PR-OL12: enrich user_notes with the per-chapter breakdown
                # entry from the volume outline so the LLM gets the planned
                # main_progress / side_progress / foreshadow_state / key_scene
                # for this exact chapter, not just the whole volume blob.
                _chapter_user_notes = req.user_input or ""
                if _chapter_breakdown_entry:
                    try:
                        import json as _json_pr_ol12
                        _entry_json = _json_pr_ol12.dumps(
                            _chapter_breakdown_entry, ensure_ascii=False, indent=2
                        )
                        _hint = (
                            "\n\n【本章现有写作安排】\n"
                            + _entry_json
                            + "\n\n结合本次作者要求，在本章已有安排上补充具体过程和场景。"
                            "作者修订后的文字优先于旧分卷摘要；计划尚未在正文发生，不得当作前文事实。"
                        )
                        _chapter_user_notes = (_chapter_user_notes + _hint).strip()
                    except Exception as _hint_err:
                        logger.warning(
                            "PR-OL12 breakdown hint serialisation failed: %s",
                            _hint_err,
                        )
                if active_foreshadow_block:
                    _chapter_user_notes = (_chapter_user_notes + "\n\n" + active_foreshadow_block).strip()
                async with aclosing(await generator.generate_chapter_outline(book_outline=chapter_save_snapshot.book_data if chapter_save_snapshot else book_outline, volume_outline=chapter_save_snapshot.volume_data if chapter_save_snapshot else volume_outline, chapter_idx=req.chapter_idx or 1, previous_chapter_summary=chapter_save_snapshot.previous_summary if chapter_save_snapshot else previous_chapter_summary, user_notes=_chapter_user_notes, stream=True, target_word_count=chapter_save_snapshot.target_words if chapter_save_snapshot else chapter_word_target)) as owned_stream:
                    async for chunk in owned_stream:
                        collected_text.append(chunk)
                        yield f"data: {json.dumps({'text': chunk})}\n\n"

            # Auto-save outline to DB
            full_text = "".join(collected_text)
            if not full_text.strip():
                raise ValueError("大纲未返回完整内容")
            if full_text and req.project_id:
                # Every outline save drains before releasing the project lock.
                async def _persist_outline_now():
                    from app.db.session import async_session_factory
                    if req.level == "volume":
                        from app.services.volume_creation import materialize_volume
                        raw = full_text.strip()
                        if raw.startswith("```") and raw.endswith("```"):
                            raw = "\n".join(raw.splitlines()[1:-1])
                        parsed = json.loads(raw)
                        async with async_session_factory() as save_db:
                            outline = await materialize_volume(save_db, volume_creation_snapshot, parsed)
                            await save_db.commit()
                            saved_id = str(outline.id)
                        _inc_chapter_auto_save("outline", "success", "ok")
                        return saved_id, None
                    if req.level == "chapter":
                        from app.services.chapter_outline_save import save_chapter_outline
                        async with async_session_factory() as save_db:
                            outline, written_title = await save_chapter_outline(save_db, chapter_save_snapshot, full_text)
                            await save_db.commit()
                            saved_id = str(outline.id)
                        _inc_chapter_auto_save("outline", "success", "ok")
                        return saved_id, written_title
                    from app.services.book_outline_save import save_book_outline
                    async with async_session_factory() as save_db:
                        outline = await save_book_outline(save_db, book_save_snapshot, full_text,
                                                          _staged_book_done_payload)
                        await save_db.commit()
                        saved_id = str(outline.id)
                    _inc_chapter_auto_save("outline", "success", "ok")
                    return saved_id, None

                try:
                    _saved_id, _written_title = await _persist_outline_now()
                    if _written_title:
                        yield f"data: {json.dumps({'status': 'saved', 'outline_id': _saved_id, 'chapter_title': _written_title}, ensure_ascii=False)}\n\n"
                    elif _saved_id:
                        yield f"data: {json.dumps({'status': 'saved', 'outline_id': _saved_id})}\n\n"
                except asyncio.CancelledError:
                    # Rollback/cancellation must finish before the guard releases.
                    raise
                except Exception as save_err:
                    # v1.8.1 Bug L: do NOT silently swallow (parity with chapter path).
                    logger.error(
                        "Auto-save outline FAILED (project_id=%s level=%s): %s",
                        req.project_id,
                        req.level,
                        save_err,
                        exc_info=True,
                    )
                    _inc_chapter_auto_save(
                        "outline", "failure", type(save_err).__name__
                    )
                    try:
                        yield (
                            f"data: {json.dumps({'status': 'save_failed', 'kind': 'outline', 'error': '保存未获确认，请刷新检查当前内容后再操作。', 'error_class': type(save_err).__name__})}\n\n"
                        )
                    except Exception:  # pragma: no cover - SSE pipe might already be closed
                        pass
                    return

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            # 只记异常类型时,真实的失败原因(缺哪一段、卷数不符)全被吞掉,
            # 排查只能靠猜。带上消息。
            logger.warning("Outline generation failed: %s: %s", type(e).__name__, e)
            yield f"data: {json.dumps(generation_error_payload(e, 'outline'), ensure_ascii=False)}\n\n"

    async def protected_outline_stream():
        from contextlib import aclosing
        from app.db import session
        from app.services.production.writer import guarded_stream
        try:
            async with aclosing(guarded_stream(session.engine, session.async_session_factory,
                                               req.project_id, event_stream, metadata_only=True)) as stream:
                async for item in stream:
                    yield item
        except Exception:
            logger.warning("Outline generation guard failed", exc_info=True)
            yield 'data: ' + json.dumps({'status': 'failed', 'error': '大纲生成未获确认，请刷新检查后再操作。'}) + '\n\n'

    return StreamingResponse(
        protected_outline_stream() if req.project_id else event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# =========================================================================
# Async generation (background task mode — for mobile / persistent progress)
# =========================================================================


class AsyncGenerateRequest(BaseModel):
    project_id: str
    request_id: str | None = None
    expected_content_text: str | None = None
    task_type: str | None = None  # outline_book, outline_volume, outline_chapter, chapter
    user_input: str = ""
    style_id: str | None = None
    structure_book_id: str | None = None  # Optional: extract & use plot structure from this book
    enable_polish: bool = False  # Optional: second-pass anti-AI polishing
    skip_polish: bool = False  # Optional: skip chapter quality rewrite gate
    chapter_id: str | None = None
    volume_idx: int | None = Field(None, strict=True, ge=1, le=2147483647)
    chapter_idx: int | None = Field(None, strict=True, ge=1, le=2147483647)
    params: dict | None = None


@router.post("/async")
async def start_async_generation(
    req: AsyncGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Commit a fixed author request before publishing its original task."""
    from app.services.generation_submissions import (
        GenerationSubmissionError, submit_generation,
    )
    try:
        return await submit_generation(db, req.model_dump())
    except GenerationSubmissionError as exc:
        raise HTTPException(exc.http_status, detail=exc.detail) from None


def _async_task_identity(task) -> dict:
    """Expose the persisted target without leaking arbitrary task parameters."""
    from app.services.generation_submissions import generation_task_identity
    return generation_task_identity(task)


def _effective_task_status(task) -> str:
    """Report 'cancelled' whenever the cancel marker exists in params_json.

    The Celery worker's periodic progress commits can overwrite a
    status='cancelled' column value (it re-reads/writes the row without
    checking for cancellation). The cancel endpoint therefore also persists
    ``params_json['cancelled_at']``; any row carrying that marker is
    reported as cancelled regardless of the status column.
    """
    from app.services.generation_submissions import effective_generation_task_status
    return effective_generation_task_status(task)


@router.get("/async/{task_id}")
async def get_async_generation(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Poll generation task progress."""
    from app.models.generation_task import GenerationTask

    task = await db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.services.polish_candidate import polish_provenance

    return {
        **_async_task_identity(task),
        "status": _effective_task_status(task),
        "char_count": task.char_count,
        "progress_text": task.progress_text or "",
        "result_text": task.result_text or "",
        "polished_text": task.polished_text or "",
        "polish_provenance": polish_provenance(task),
        "error_message": task.error_message,
        "created_at": str(task.created_at),
    }


@router.get("/async/project/{project_id}")
async def list_project_tasks(
    project_id: str,
    limit: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List generation tasks for a project (created_at desc).

    ``active_only`` excludes terminal work before applying the limit, so an
    old active task is not hidden by newer completed history.
    ``status`` filters on the *effective* status (marker-derived 'cancelled'
    included — see :func:`_effective_task_status`), so filtering is applied
    in Python after fetching rather than in SQL.
    """
    from app.models.generation_task import GenerationTask

    progress_length = func.length(GenerationTask.progress_text)
    progress_tail = func.substr(
        GenerationTask.progress_text,
        case((progress_length > 200, progress_length - 199), else_=1),
    ).label("progress_text")
    query = (
        select(GenerationTask.id, GenerationTask.project_id, GenerationTask.task_type,
               GenerationTask.status, GenerationTask.char_count, GenerationTask.created_at,
               GenerationTask.updated_at, GenerationTask.params_json,
               GenerationTask.error_message, progress_tail)
        .where(GenerationTask.project_id == project_id)
        .order_by(GenerationTask.created_at.desc().nullslast(), GenerationTask.id.desc())
    )
    terminal = {"completed", "failed", "cancelled", "needs_review", "needs_repair"}
    if active_only:
        query = query.where(or_(GenerationTask.status.notin_(terminal), GenerationTask.status.is_(None)))
    filtered = status is not None or active_only
    batch_size = 128 if filtered else limit
    page_query = query

    items: list[dict] = []
    while True:
        # A long book can retain many cancelled-marker rows. Read bounded
        # metadata pages; full candidates and full progress are not list data.
        rows = (await db.execute(page_query.limit(batch_size))).all()
        for t in rows:
            effective = _effective_task_status(t)
            if active_only and effective in terminal:
                continue
            if status is not None and effective != status:
                continue
            items.append({
                **_async_task_identity(t), "status": effective, "char_count": t.char_count,
                "created_at": str(t.created_at), "updated_at": str(t.updated_at),
                "progress_text": t.progress_text or "", "error_message": t.error_message,
            })
            if len(items) >= limit:
                return items
        if not filtered or len(rows) < batch_size:
            return items
        last = rows[-1]
        if last.created_at is None:
            page_query = query.where(and_(GenerationTask.created_at.is_(None), GenerationTask.id < last.id))
        else:
            page_query = query.where(or_(
                GenerationTask.created_at < last.created_at,
                and_(GenerationTask.created_at == last.created_at, GenerationTask.id < last.id),
                GenerationTask.created_at.is_(None),
            ))


_TERMINAL_TASK_STATUSES = {"completed", "needs_review", "needs_repair", "failed", "cancelled"}


@router.post("/async/{task_id}/cancel")
async def cancel_async_generation(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel a pending/running generation task.

    Semantics ("cancelled" = "result will be ignored/not surfaced"):

    - Commits ``status='cancelled'`` and a durable marker while holding the
      task row lock. The marker remains authoritative for legacy writers;
      the chapter write fence also rejects writes after cancellation.
      This does not guarantee immediate remote model cancellation.
    - Best-effort Celery revoke (terminate=False, i.e. only prevents a
      not-yet-started task from running) using the ``celery_task_id`` stored
      in params_json at enqueue time; silently skipped if unavailable.
    - 404 if absent; 409 task_already_terminal for all terminal states,
      including needs_review and needs_repair.
    """
    from app.models.generation_task import GenerationTask

    try:
        async with asyncio.timeout(30):
            task = await db.get(GenerationTask, task_id, with_for_update=True, populate_existing=True)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            if _effective_task_status(task) in _TERMINAL_TASK_STATUSES:
                raise HTTPException(status_code=409, detail="task_already_terminal")
            params = task.params_json if isinstance(task.params_json, dict) else {}
            task.params_json = {**params, "cancelled_at": datetime.now(timezone.utc).isoformat()}
            task.status = "cancelled"
            await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.warning('Async cancellation unconfirmed (%s)', type(exc).__name__)
        raise HTTPException(503, detail={'code': 'generation_cancel_unconfirmed',
            'message': '取消结果未获确认，请重新读取原任务核对。', 'task_id': task_id}) from None

    celery_id = params.get("celery_task_id")
    if celery_id:
        try:
            from app.tasks import celery_app

            celery_app.control.revoke(str(celery_id), terminate=False)
        except Exception as revoke_err:  # pragma: no cover - broker may be down
            logger.warning(
                "Best-effort Celery revoke failed task_id=%s: %s", task_id, revoke_err
            )

    return {"task_id": str(task.id), "status": "cancelled"}
