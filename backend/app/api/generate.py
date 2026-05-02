"""AI content generation endpoints (SSE streaming)."""

import json
import logging
import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Project, Chapter, Volume, Outline, WorldRule
from app.services.chapter_generator import ChapterGenerator
from app.services.outline_generator import OutlineGenerator

logger = logging.getLogger(__name__)
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
    use_scene_mode: bool = False
    # Hint for scene_planner; clamped to 3..6 by SceneOrchestrator. None = auto.
    n_scenes_hint: int | None = None
    # Override the chapter target word count for scene-mode planning.
    target_words: int | None = None
    # v1.5.0 C2: opt-in auto-revise loop. After the initial scene-mode
    # write completes and is saved, ChapterEvaluator scores the chapter;
    # if overall < revise_threshold, SceneOrchestrator is re-run with the
    # issues fed back as a revise instruction (up to max_revise_rounds).
    # Only effective when use_scene_mode=True (single-shot ChapterGenerator
    # cannot consume per-issue feedback meaningfully).
    auto_revise: bool = False
    # On the 0-10 evaluator scale (B1' baseline ~7.98). Below threshold = revise.
    revise_threshold: float = 7.0
    # Hard cap on rewrite rounds to bound LLM cost (3 total writes max at N=2).
    max_revise_rounds: int = 2


class GenerateOutlineRequest(BaseModel):
    project_id: str | None = None
    level: str  # book, volume, chapter
    user_input: str = ""
    parent_outline_id: str | None = None
    volume_idx: int | None = None
    chapter_idx: int | None = None
    style_id: str | None = None
    # v1.4.2 Task B: opt-in structured staged SSE stream for book outline.
    # Mirrors ?staged_stream=1 query param; body field wins when both set.
    staged_stream: bool | None = None


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
            iter([f"data: {json.dumps({'error': 'Project not found'})}\n\n"]),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    project_settings = project.settings_json or {}

    rules_result = await db.execute(
        select(WorldRule).where(WorldRule.project_id == req.project_id)
    )
    world_rules = [r.rule_text for r in rules_result.scalars().all()]

    book_outline_result = await db.execute(
        select(Outline).where(
            Outline.project_id == req.project_id,
            Outline.level == "book",
        )
    )
    book_outline = book_outline_result.scalar_one_or_none()
    book_summary = book_outline.content_json.get("main_plot", "") if book_outline and book_outline.content_json else ""

    chapter_outline: dict = {}
    previous_text = ""
    current_text = ""
    target_words: int | None = None

    if req.chapter_id:
        chapter = await db.get(Chapter, req.chapter_id)
        if chapter:
            chapter_outline = chapter.outline_json or {}
            current_text = chapter.content_text or ""
            target_words = chapter.target_word_count
            prev_result = await db.execute(
                select(Chapter).where(
                    Chapter.volume_id == chapter.volume_id,
                    Chapter.chapter_idx == chapter.chapter_idx - 1,
                )
            )
            prev_chapter = prev_result.scalar_one_or_none()
            if prev_chapter:
                previous_text = prev_chapter.content_text or ""

    # Fall back to project default for target_words
    if target_words is None and isinstance(project_settings.get("target_chapter_words"), int):
        target_words = int(project_settings["target_chapter_words"])

    # Resolve style: explicit style_id > manual text > auto-resolve
    resolved_style = req.style_instruction
    if not resolved_style and req.style_id:
        try:
            from app.models.project import StyleProfile
            from app.services.style_compiler import compile_style
            profile = await db.get(StyleProfile, req.style_id)
            if profile:
                resolved_style = compile_style(profile)
        except Exception as e:
            logger.warning("Style compile failed: %s", e)
    if not resolved_style:
        try:
            from app.services.style_runtime import resolve_style_prompt
            resolved_style = await resolve_style_prompt(db, req.project_id, req.chapter_id) or ""
        except Exception as e:
            logger.warning("Style resolve failed: %s", e)

    async def event_stream() -> AsyncGenerator[str, None]:
        collected_text: list[str] = []
        try:
            yield f"data: {json.dumps({'status': 'generating', 'message': 'Starting...'})}\n\n"

            effective_user_instruction = req.user_instruction or ""
            if target_words:
                effective_user_instruction = (
                    effective_user_instruction
                    + f"\n\n【本章目标字数】约 {target_words} 字（允许 ±15% 浮动）。"
                )

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

            # v1.5.0 C1: opt-in scene-staged streaming. SceneOrchestrator
            # plans 3-6 scene briefs (scene_planner) then streams each scene
            # 800-1200 chars (scene_writer). Falls back to ChapterGenerator's
            # single-shot "generation" prompt when use_scene_mode is False.
            stream_iter: AsyncGenerator[str, None]
            if req.use_scene_mode:
                from app.services.scene_orchestrator import SceneOrchestrator

                orchestrator = SceneOrchestrator()
                effective_target_words = req.target_words or target_words

                async def _on_scene_start(scene) -> None:  # type: ignore[no-untyped-def]
                    pass  # placeholder; SSE "scene" events can be added later

                stream_iter = orchestrator.orchestrate_chapter_stream(
                    project_id=req.project_id,
                    volume_id=resolved_volume_id,
                    chapter_idx=resolved_chapter_idx,
                    db=db,
                    chapter_id=req.chapter_id,
                    user_instruction=effective_user_instruction,
                    target_words=effective_target_words,
                    n_scenes_hint=req.n_scenes_hint,
                    on_scene_start=_on_scene_start,
                )
            else:
                generator = ChapterGenerator()
                stream_iter = generator.generate_stream(
                    project_id=req.project_id,
                    volume_id=resolved_volume_id,
                    chapter_idx=resolved_chapter_idx,
                    db=db,
                    chapter_id=req.chapter_id,
                    user_instruction=effective_user_instruction,
                )
            async for chunk in stream_iter:
                if chunk:
                    collected_text.append(chunk)
                yield f"data: {json.dumps({'text': chunk})}\n\n"

            # Auto-save chapter content to DB (Bug K fix: parity with outline auto-save).
            full_text = "".join(collected_text)
            if full_text:
                try:
                    from app.db.session import async_session_factory
                    async with async_session_factory() as save_db:
                        target_chapter = None
                        if req.chapter_id:
                            target_chapter = await save_db.get(Chapter, req.chapter_id)
                        if target_chapter is None and resolved_volume_id and resolved_chapter_idx is not None:
                            lookup = await save_db.execute(
                                select(Chapter).where(
                                    Chapter.volume_id == resolved_volume_id,
                                    Chapter.chapter_idx == resolved_chapter_idx,
                                )
                            )
                            target_chapter = lookup.scalar_one_or_none()
                        if target_chapter is not None:
                            target_chapter.content_text = full_text
                            target_chapter.word_count = len(full_text)
                            target_chapter.status = "completed"
                            await save_db.commit()
                            await save_db.refresh(target_chapter)
                            # v1.8.1: persistence actually committed; record success.
                            _inc_chapter_auto_save("chapter", "success", "ok")
                            yield f"data: {json.dumps({'status': 'saved', 'chapter_id': str(target_chapter.id), 'word_count': target_chapter.word_count})}\n\n"
                            # B2' (v1.5.0): kick entity-extraction task post-commit.
                            try:
                                from app.services.entity_dispatch import dispatch_for_chapter
                                await dispatch_for_chapter(
                                    target_chapter, save_db,
                                    caller="api.generate.stream_generate",
                                )
                            except Exception as dispatch_err:
                                logger.warning(
                                    "Entity dispatch after auto-save failed: %s", dispatch_err
                                )
                            # v1.7.4 P0-2: write chapter.summary so the next
                            # chapter's ContextPack.recent_summaries is non-empty.
                            try:
                                from app.services.chapter_summarizer import summarize_and_save_chapter
                                ok_sum, _sum_text = await summarize_and_save_chapter(
                                    chapter_id=target_chapter.id, db=save_db,
                                )
                                if ok_sum:
                                    logger.info(
                                        "Chapter summary written (chapter_id=%s len=%d)",
                                        target_chapter.id, len(_sum_text),
                                    )
                            except Exception as sum_err:
                                logger.warning(
                                    "Chapter summarize after auto-save failed: %s", sum_err
                                )
                        else:
                            logger.warning(
                                "Auto-save chapter: no target row (chapter_id=%s vol=%s idx=%s)",
                                req.chapter_id, resolved_volume_id, resolved_chapter_idx,
                            )
                            _inc_chapter_auto_save("chapter", "failure", "no_target_row")
                            yield f"data: {json.dumps({'status': 'save_failed', 'kind': 'chapter', 'reason': 'no_target_row', 'chapter_id': req.chapter_id, 'volume_id': str(resolved_volume_id) if resolved_volume_id else None, 'chapter_idx': resolved_chapter_idx})}\n\n"
                except Exception as save_err:
                    # v1.8.1 Bug L: do NOT silently swallow. asyncpg connection-closed,
                    # transaction-already-aborted, etc. used to leave chapters.content_text
                    # empty while SSE clients had received streamed text. Emit an SSE
                    # `save_failed` event so the frontend can surface it, log full traceback,
                    # and increment chapter_auto_save_total{outcome="failure"}.
                    logger.error(
                        "Auto-save chapter FAILED (chapter_id=%s vol=%s idx=%s): %s",
                        req.chapter_id,
                        resolved_volume_id,
                        resolved_chapter_idx,
                        save_err,
                        exc_info=True,
                    )
                    _inc_chapter_auto_save(
                        "chapter", "failure", type(save_err).__name__
                    )
                    try:
                        yield (
                            f"data: {json.dumps({'status': 'save_failed', 'kind': 'chapter', 'error': str(save_err)[:500], 'error_class': type(save_err).__name__})}\n\n"
                        )
                    except Exception:  # pragma: no cover - SSE pipe might already be closed
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
                and req.use_scene_mode
                and req.auto_revise
                and resolved_volume_id is not None
                and resolved_chapter_idx is not None
                and req.chapter_id
            ):
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
                        should_revise,
                    )
                    from app.services.chapter_evaluator import ChapterEvaluator
                    from app.services.scene_orchestrator import SceneOrchestrator

                    revise_chapter_id = req.chapter_id
                    current_text = full_text
                    revise_outline = chapter_outline or {}
                    max_rounds = max(0, int(req.max_revise_rounds))
                    threshold = float(req.revise_threshold)

                    for round_idx in range(1, max_rounds + 1):
                        # 1) Score the current saved version.
                        yield f"data: {json.dumps({'event': 'evaluating', 'round': round_idx})}\n\n"
                        evaluator = ChapterEvaluator()
                        eval_result = await evaluator.evaluate(
                            chapter_text=current_text,
                            chapter_outline=revise_outline,
                        )
                        _x4_inc_revise("scored")  # v1.6.0 X4 metric: revise round outcome
                        scored_payload = json.dumps({
                            "event": "scored",
                            "round": round_idx,
                            "overall": eval_result.overall,
                            "issues": len(eval_result.issues),
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
                                ))
                                await eval_db.commit()
                        except Exception as persist_err:
                            logger.warning(
                                "C2 auto-revise: failed to persist ChapterEvaluation row (round=%d): %s",
                                round_idx, persist_err,
                            )

                        # 2) Threshold gate.
                        if not should_revise(eval_result, threshold=threshold):
                            _x4_inc_revise("skipped")  # v1.6.0 X4 metric: revise round outcome
                            skipped_payload = json.dumps({
                                "event": "revise_skipped",
                                "reason": "score_above_threshold",
                                "overall": eval_result.overall,
                                "threshold": threshold,
                            })
                            yield f"data: {skipped_payload}\n\n"
                            break

                        # 3) Build revise instruction and rerun SceneOrchestrator.
                        revise_instr = issues_to_revise_instruction(
                            eval_result, round_idx=round_idx,
                        )
                        merged_instruction = merge_revise_into_user_instruction(
                            effective_user_instruction, revise_instr,
                        )
                        _x4_inc_revise("revised")  # v1.6.0 X4 metric: revise round outcome
                        revising_payload = json.dumps({
                            "event": "revising",
                            "round": round_idx,
                            "overall": eval_result.overall,
                            "threshold": threshold,
                        })
                        yield f"data: {revising_payload}\n\n"

                        revise_orchestrator = SceneOrchestrator()
                        revised_chunks: list[str] = []
                        revise_timed_out = False
                        try:
                            async with asyncio.timeout(900):  # 15min hard cap per round
                                async with async_session_factory() as revise_db:
                                    async for chunk in revise_orchestrator.orchestrate_chapter_stream(
                                        project_id=req.project_id,
                                        volume_id=resolved_volume_id,
                                        chapter_idx=resolved_chapter_idx,
                                        db=revise_db,
                                        chapter_id=revise_chapter_id,
                                        user_instruction=merged_instruction,
                                        target_words=effective_target_words,
                                        n_scenes_hint=req.n_scenes_hint,
                                        on_scene_start=_on_scene_start,
                                    ):
                                        if chunk:
                                            revised_chunks.append(chunk)
                                        yield f"data: {json.dumps({'text': chunk, 'revise_round': round_idx})}\n\n"
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
                            break

                        # 4) Overwrite chapter content with the revised version.
                        try:
                            async with async_session_factory() as save_db2:
                                ch2 = await save_db2.get(_Chapter, revise_chapter_id)
                                if ch2 is not None:
                                    ch2.content_text = revised_text
                                    ch2.word_count = len(revised_text)
                                    ch2.status = "completed"
                                    await save_db2.commit()
                                    saved_payload = json.dumps({
                                        "status": "saved",
                                        "chapter_id": revise_chapter_id,
                                        "word_count": len(revised_text),
                                        "revise_round": round_idx,
                                    })
                                    yield f"data: {saved_payload}\n\n"
                        except Exception as save2_err:
                            logger.warning(
                                "C2 auto-revise round %d save failed: %s",
                                round_idx, save2_err,
                            )
                            break
                        current_text = revised_text
                    else:
                        # for-else: ran out of rounds without breaking. Emit a
                        # final scored event for the last write so the UI sees
                        # the converged score even if we never met threshold.
                        try:
                            evaluator = ChapterEvaluator()
                            final_eval = await evaluator.evaluate(
                                chapter_text=current_text,
                                chapter_outline=revise_outline,
                            )
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
                                    ))
                                    await eval_db2.commit()
                            except Exception:
                                logger.warning("C2 auto-revise final eval persist failed", exc_info=True)

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
                        "C2 auto-revise loop failed: %s", revise_err, exc_info=True,
                    )
                    yield f"data: {json.dumps({'event': 'revise_error', 'error': str(revise_err)})}\n\n"

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Generation failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
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
    # Pre-fetch all DB data before creating the generator (same pattern as generate_chapter)
    book_outline: dict = {}
    volume_outline: dict = {}

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
                )
            )
            book_ol = book_result.scalar_one_or_none()
            if book_ol:
                book_outline = book_ol.content_json or {}

        if req.parent_outline_id:
            vol_ol = await db.get(Outline, req.parent_outline_id)
            if vol_ol:
                volume_outline = vol_ol.content_json or {}

    # Resolve style: explicit style_id > auto-resolve
    style_instruction = ""
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
    if anti_ai_instruction:
        enhanced_input += anti_ai_instruction

    async def event_stream() -> AsyncGenerator[str, None]:
        collected_text = []
        try:
            generator = OutlineGenerator()

            yield f"data: {json.dumps({'status': 'generating', 'level': req.level})}\n\n"

            if req.level == "book":
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
                    )
                    async for event in stage_iter:
                        if not isinstance(event, dict):
                            # Defensive: legacy stream fall-through.
                            collected_text.append(str(event))
                            yield f"data: {json.dumps({'text': str(event)})}\n\n"
                            continue
                        kind = event.get("event")
                        if kind == "stage_chunk":
                            delta = event.get("delta", "")
                            if delta:
                                collected_text.append(delta)
                        elif kind == "done":
                            full = event.get("full_outline", "")
                            if full:
                                # Replace the per-chunk accumulator with the
                                # canonical reassembled text so auto-save keeps
                                # the in-order 9-section document.
                                collected_text = [full]
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                else:
                    async for chunk in await generator.generate_book_outline(
                        user_input=enhanced_input, stream=True, staged=False
                    ):
                        collected_text.append(chunk)
                        yield f"data: {json.dumps({'text': chunk})}\n\n"

            elif req.level == "volume":
                async for chunk in await generator.generate_volume_outline(
                    book_outline=book_outline,
                    volume_idx=req.volume_idx or 1,
                    user_notes=req.user_input,
                    stream=True,
                ):
                    collected_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            elif req.level == "chapter":
                async for chunk in await generator.generate_chapter_outline(
                    book_outline=book_outline,
                    volume_outline=volume_outline,
                    chapter_idx=req.chapter_idx or 1,
                    user_notes=req.user_input,
                    stream=True,
                ):
                    collected_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            # Auto-save outline to DB
            full_text = "".join(collected_text)
            if full_text and req.project_id:
                try:
                    from app.db.session import async_session_factory
                    async with async_session_factory() as save_db:
                        outline = Outline(
                            project_id=req.project_id,
                            level=req.level,
                            parent_id=req.parent_outline_id,
                            content_json={"raw_text": full_text},
                        )
                        save_db.add(outline)
                        await save_db.commit()
                        await save_db.refresh(outline)
                        # v1.8.1: persistence actually committed; record success.
                        _inc_chapter_auto_save("outline", "success", "ok")
                        yield f"data: {json.dumps({'status': 'saved', 'outline_id': str(outline.id)})}\n\n"
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
                            f"data: {json.dumps({'status': 'save_failed', 'kind': 'outline', 'error': str(save_err)[:500], 'error_class': type(save_err).__name__})}\n\n"
                        )
                    except Exception:  # pragma: no cover - SSE pipe might already be closed
                        pass

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Outline generation failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# =========================================================================
# Async generation (background task mode — for mobile / persistent progress)
# =========================================================================


class AsyncGenerateRequest(BaseModel):
    project_id: str
    task_type: str = "outline_book"  # outline_book, outline_volume, outline_chapter, chapter
    user_input: str = ""
    style_id: str | None = None
    structure_book_id: str | None = None  # Optional: extract & use plot structure from this book
    enable_polish: bool = False  # Optional: second-pass anti-AI polishing
    chapter_id: str | None = None
    volume_idx: int | None = None
    chapter_idx: int | None = None


@router.post("/async")
async def start_async_generation(
    req: AsyncGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Start a background generation task. Returns task_id for polling."""
    from app.models.generation_task import GenerationTask

    task = GenerationTask(
        project_id=req.project_id,
        task_type=req.task_type,
        status="pending",
        params_json={
            "user_input": req.user_input,
            "style_id": req.style_id,
            "structure_book_id": req.structure_book_id,
            "enable_polish": req.enable_polish,
            "chapter_id": req.chapter_id,
            "volume_idx": req.volume_idx,
            "chapter_idx": req.chapter_idx,
        },
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)

    from app.tasks.knowledge_tasks import run_async_generation
    run_async_generation.delay(str(task.id))

    return {"task_id": str(task.id), "status": "pending"}


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

    return {
        "task_id": str(task.id),
        "task_type": task.task_type,
        "status": task.status,
        "char_count": task.char_count,
        "progress_text": task.progress_text or "",
        "result_text": task.result_text or "",
        "polished_text": task.polished_text or "",
        "error_message": task.error_message,
        "created_at": str(task.created_at),
    }


@router.get("/async/project/{project_id}")
async def list_project_tasks(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List generation tasks for a project."""
    from app.models.generation_task import GenerationTask

    result = await db.execute(
        select(GenerationTask)
        .where(GenerationTask.project_id == project_id)
        .order_by(GenerationTask.created_at.desc())
        .limit(10)
    )
    return [
        {
            "task_id": str(t.id),
            "task_type": t.task_type,
            "status": t.status,
            "char_count": t.char_count,
            "created_at": str(t.created_at),
        }
        for t in result.scalars().all()
    ]
