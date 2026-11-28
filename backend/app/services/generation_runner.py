"""v0.7 — Chapter generation state machine runner.

Pipeline: planning -> drafting -> critic -> [rewrite] -> finalize -> done.
Every phase persists a checkpoint on the GenerationRun row so the run can
resume after a crash.

Usage:
    result = await start_run(project_id=..., chapter_id=..., db=...,
                             request_id=..., expected_content_text=...)

    # resume after a crash
    await execute_run(run_id=result['id'], db=..., resume=True)
"""

from __future__ import annotations

import logging
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_run import (
    PHASE_AWAITING_REVIEW,
    PHASE_CRITIC,
    PHASE_DONE,
    PHASE_DRAFTING,
    PHASE_FAILED,
    PHASE_FINALIZE,
    PHASE_PLANNING,
    PHASE_REWRITE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_NEEDS_REVIEW,
    CriticReport,
    GenerationRun,
)
from app.services.context_pack import ContextPackBuilder, fetch_writing_rules
from app.services.context_errors import RequiredContextUnavailable
from app.services.critic_service import run_critic
from app.services.generation_execution import (
    GenerationExecutionUnavailable, GenerationRunInvalid, GenerationRunUnavailable,
    generation_execution_session, generation_read_session,
)
from app.services import generation_run_submissions as submissions
from app.services import generation_run_attempts as attempts
from app.services.generation_run_views import (
    checkpoint_payload, checkpoint_text, run_review_status, run_stop_reason,
)
from app.services.production.control import PipelineConflict
from app.services.prompt_registry import run_text_prompt
from app.services.review_contract import (
    COMPLETE, NORMAL_FINISHES, critic_next_action, incomplete_critic_report,
)
from app.services.tool_registry import run_tool
from app.services import run_bus

logger = logging.getLogger(__name__)


async def start_run(
    *,
    project_id: str,
    chapter_id: str | None,
    db: AsyncSession,
    max_rewrite_count: int = 2,
    request_id: str | uuid.UUID | None = None,
    expected_content_text: str | None = None,
) -> dict[str, Any]:
    """Execute a new fixed request; retries only return the original state."""
    _validate_rewrite_budget(max_rewrite_count)
    if chapter_id is None:
        raise GenerationRunInvalid('请选择要生成的章节。')
    intent = submissions.run_submission_intent(
        request_id=request_id, project_id=project_id, chapter_id=chapter_id,
        max_rewrite_count=max_rewrite_count, expected_content_text=expected_content_text,
    )
    try:
        async with generation_read_session(db) as reader:
            existing = await submissions.find_submitted_run(reader, intent)
            if existing is not None:
                return _run_snapshot(existing)
        try:
            async with generation_execution_session(db, intent.project_id) as execution:
                # The initial reader may have missed a request accepted
                # immediately before we acquired project execution.
                existing = await submissions.find_submitted_run(execution, intent)
                if existing is not None:
                    return _run_snapshot(existing)
                await _validate_run_target(execution, intent.project_id, intent.chapter_id)
                receipt = await submissions.prepare_run_submission(execution, intent)
                run = await _create_run_locked(
                    project_id=project_id, chapter_id=chapter_id, db=execution,
                    max_rewrite_count=max_rewrite_count, run_id=intent.request_id, submission=receipt,
                )
                await _execute_run_locked(run_id=str(run.id), db=execution)
                await execution.refresh(run)
                result = _run_snapshot(run)
            return result
        except PipelineConflict:
            # A concurrent request may have committed its identity between
            # our initial read and failed lock attempt. Observation needs no
            # execution lock and must not resume a running/failed request.
            async with generation_read_session(db) as reader:
                existing = await submissions.find_submitted_run(reader, intent)
                if existing is not None:
                    return _run_snapshot(existing)
            raise
    except GenerationExecutionUnavailable as exc:
        exc.run_id = str(intent.request_id)
        raise


def _validate_rewrite_budget(max_rewrite_count):
    if type(max_rewrite_count) is not int or not 0 <= max_rewrite_count <= 8:
        raise GenerationRunInvalid('invalid_rewrite_budget')


async def _validate_run_target(db, project_id, chapter_id):
    from app.models.pipeline import PipelineRun
    from app.models.project import Chapter, Project, Volume
    from app.services.production.control import ACTIVE_STATES, PipelineConflict

    if chapter_id is None:
        raise GenerationRunInvalid('请选择要生成的章节。')
    pid, cid = uuid.UUID(str(project_id)), uuid.UUID(str(chapter_id))
    target = await db.scalar(select(Chapter.id)
        .join(Volume, Chapter.volume_id == Volume.id)
        .join(Project, Volume.project_id == Project.id)
        .where(Chapter.id == cid, Project.id == pid, Project.deleted_at.is_(None)))
    if target is None:
        raise GenerationRunUnavailable('作品或章节不存在、已删除，或章节不属于当前作品。')
    active = await db.scalar(select(PipelineRun.id).where(
        PipelineRun.project_id == pid, PipelineRun.state.in_(ACTIVE_STATES),
    ).limit(1))
    if active is not None:
        raise PipelineConflict('本书已有整书任务，请先暂停后再生成单章。')
    await db.commit()


async def _create_run_locked(
    *, project_id: str, chapter_id: str | None, db: AsyncSession,
    max_rewrite_count: int = 2, run_id: uuid.UUID | None = None,
    submission=None,
) -> GenerationRun:
    """Internal checkpoint creation; callers must already own execution."""
    _validate_rewrite_budget(max_rewrite_count)
    run = GenerationRun(
        id=run_id or uuid.uuid4(),
        project_id=uuid.UUID(str(project_id)),
        chapter_id=uuid.UUID(str(chapter_id)) if chapter_id else None,
        phase=PHASE_PLANNING,
        status=STATUS_RUNNING,
        checkpoint_data={},
        max_rewrite_count=max_rewrite_count,
    )
    from app.services.bvsr import bvsr_n, is_bvsr_enabled
    enabled = bool(chapter_id) and is_bvsr_enabled()
    run.checkpoint_data = {attempts.KEY: attempts.initial_journal(
        run, bvsr_enabled=enabled, variant_count=bvsr_n() if enabled else 1,
    )}
    if submission is not None:
        db.add(submission)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _checkpoint_set(run: GenerationRun, phase: str, data: Any) -> None:
    cp = deepcopy(checkpoint_payload(run.checkpoint_data))
    cp[phase] = deepcopy(data)
    attempts.checkpoint_updated(cp, phase)
    run.checkpoint_data = cp
    run.updated_at = datetime.now(timezone.utc)


async def _resolve_chapter_coords(db: AsyncSession, chapter_id: Any) -> tuple[str, int]:
    """Map a chapter_id to the ``(volume_id, chapter_idx)`` pair required by
    the current ``ContextPackBuilder.build()`` signature.

    Task A3: the v0.8 runner was wired against the old
    ``build(project_id, chapter_id)`` signature; the builder has since moved
    to ``build(project_id, volume_id, chapter_idx)``. ``chapter_idx`` here is
    the volume-local DB value — build() resolves the book-global index
    internally (Task A2), so the runner must not convert it.
    """
    from app.models.project import Chapter

    chapter = await db.get(Chapter, chapter_id)
    if chapter is None:
        raise ValueError(f"Chapter {chapter_id} not found")
    return str(chapter.volume_id), int(chapter.chapter_idx)


async def _phase_planning(
    run: GenerationRun, db: AsyncSession, *, candidate_text: str | None = None,
) -> dict[str, Any]:
    """Build current source material for drafting or an actual candidate."""
    if not run.chapter_id:
        raise ValueError(
            "GenerationRun has no chapter_id; ContextPackBuilder.build() "
            "requires a target chapter (volume_id + chapter_idx)"
        )
    volume_id, chapter_idx = await _resolve_chapter_coords(db, run.chapter_id)
    builder = ContextPackBuilder(db=db)
    pack = await builder.build(
        project_id=str(run.project_id),
        volume_id=volume_id,
        chapter_idx=chapter_idx,
    )
    # v0.8 ContextPack v3: 4th recall path — writing_rules scoped to genre_profile.
    try:
        pack.writing_rules = await fetch_writing_rules(db, str(run.project_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("writing_rules fetch skipped: %s", exc)
        pack.writing_rules = []
    # This runner owns its phase transaction. Publish inputs before any
    # deferred embeddings; the builder must never decide to commit for it.
    await db.commit()
    if candidate_text is not None:
        pack.current_content = candidate_text
    await pack.resolve_cognition(db=db, user_instruction=candidate_text or '')
    pack_snapshot = {
        "system_prompt": pack.to_system_prompt(),
        "rag_snippets": list(getattr(pack, "rag_snippets", []) or [])[:20],
        "style_samples": list(getattr(pack, "style_samples", []) or [])[:5],
        "writing_rules": list(getattr(pack, "writing_rules", []) or [])[:10],
        "meta_project_id": str(run.project_id),
    }
    return {"pack": pack_snapshot}


async def _phase_drafting(
    run: GenerationRun,
    db: AsyncSession,
    planning_data: dict[str, Any],
) -> dict[str, Any]:
    pack = planning_data.get("pack", {})
    if not isinstance(pack.get('system_prompt'), str) or not pack['system_prompt']:
        raise ValueError('generation_context_requires_replanning')
    context_block = "\n".join(pack.get("rag_snippets") or [])

    # v0.8 Agent tool loop (max 3 rounds, default off)
    tool_context = ""
    if os.getenv("AGENT_TOOL_LOOP_ENABLED", "false").lower() in ("true", "1", "yes"):
        tool_context = await _run_tool_loop(
            db,
            project_id=str(run.project_id),
            chapter_id=str(run.chapter_id) if run.chapter_id else None,
            context_block=context_block,
        )

    # The bounded system prompt already contains RAG and writing rules.
    # Repeating raw snapshots here would escape that budget a second time.
    parts = []
    if tool_context:
        parts.append(f"<工具核实>\n{tool_context}\n</工具核实>")
    parts.append("请撰写本章正文。")
    user_content = "\n\n".join(parts)
    result = await run_text_prompt(
        "generation",
        user_content,
        db,
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
        extra_system=pack['system_prompt'],
    )
    return _draft_delivery(result)


def _draft_delivery(result: Any) -> dict[str, Any]:
    text = getattr(result, 'text', '')
    if not isinstance(text, str):
        text = ''
    complete = bool(text.strip()) and getattr(result, 'finish_reason', None) in NORMAL_FINISHES
    return {'text': text, 'delivery_status': COMPLETE if complete else 'incomplete'}


async def _run_tool_loop(
    db: AsyncSession,
    *,
    project_id: str,
    chapter_id: str | None,
    context_block: str,
    max_rounds: int = 3,
) -> str:
    """Minimal v0.8 tool loop.

    Runs up to ``max_rounds`` tool calls per drafting attempt and returns a
    flat text block that the main generation prompt can read. The concrete
    calls here are intentionally conservative so the feature can be enabled
    safely even without careful per-project tuning.
    """
    fragments: list[str] = []
    rounds = 0

    # Round 1: suggest_beat for current progress (best-effort 0.5 if unknown).
    if rounds < max_rounds:
        try:
            beat = await run_tool(
                "suggest_beat",
                {"chapter_progress": 0.5},
                db,
                project_id=project_id,
                chapter_id=chapter_id,
            )
            if isinstance(beat, dict) and beat.get("beat_title"):
                fragments.append(
                    f"beat: {beat.get('beat_title')} — {beat.get('beat_description','')}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("tool suggest_beat skipped: %s", exc)
        rounds += 1

    # Round 2+: check_character_fact for characters detected in context.
    try:
        from sqlalchemy import select as _sel
        from app.models.project import Character

        chars = (
            await db.execute(_sel(Character.name).where(Character.project_id == project_id))
        ).scalars().all()
        mentioned = [n for n in chars if n and n in (context_block or "")]
        for name in mentioned[: max_rounds - rounds]:
            fact = await run_tool(
                "check_character_fact",
                {"character_name": name},
                db,
                project_id=project_id,
                chapter_id=chapter_id,
            )
            if isinstance(fact, dict):
                loc = fact.get("location", "") or "?"
                lvl = fact.get("power_level", "") or "?"
                fragments.append(f"char[{name}]: 位置={loc} 实力={lvl}")
            rounds += 1
            if rounds >= max_rounds:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool check_character_fact loop skipped: %s", exc)

    return "\n".join(fragments)


async def _phase_critic(
    run: GenerationRun,
    db: AsyncSession,
    draft_text: str,
    round_num: int,
) -> dict[str, Any]:
    prepared = await _phase_planning(run, db, candidate_text=draft_text)
    pack_summary = prepared['pack']['system_prompt']
    critic_out = await run_critic(
        draft=draft_text,
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
        db=db,
        pack_summary=pack_summary,
    )
    # The caller commits report + call receipt + checkpoint together. A
    # standalone report commit would leave recovery unable to prove delivery.
    return critic_out


async def _phase_rewrite(
    run: GenerationRun,
    db: AsyncSession,
    draft_text: str,
    critic_report: dict[str, Any],
) -> dict[str, Any]:
    issues = critic_report.get("issues") or []
    prepared = await _phase_planning(run, db, candidate_text=draft_text)
    context = prepared['pack']['system_prompt']
    issue_lines = "\n".join(
        f"- [{i.get('severity','?')}] {i.get('category','?')}: {i.get('desc','')}"
        for i in issues
        if i.get("severity") == "hard"
    )
    user_content = (
        f"<原初稿>\n{draft_text}\n</原初稿>\n\n"
        f"<必须修正的问题>\n{issue_lines}\n</必须修正的问题>\n\n"
        "保持原有情节主线，仅修正上述问题后重写全章。"
    )
    result = await run_text_prompt(
        "rewrite",
        user_content,
        db,
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
        extra_system=context,
    )
    return _draft_delivery(result)


def _run_result(run: GenerationRun) -> dict[str, Any]:
    cp = checkpoint_payload(run.checkpoint_data)
    draft = checkpoint_text(cp)
    final_text = checkpoint_text(cp, PHASE_FINALIZE, 'final_text') if run.status == STATUS_DONE else None
    return {
        'status': run.status, 'phase': run.phase, 'rewrite_count': run.rewrite_count,
        'final_text': final_text or draft,
        'review_status': run_review_status(cp),
        'editorial_status': 'needs_author_review',
        'stop_reason': run_stop_reason(cp) if run.status == STATUS_NEEDS_REVIEW else None,
    }


def _run_snapshot(run: GenerationRun) -> dict[str, Any]:
    return {
        **_run_result(run), 'id': run.id, 'project_id': run.project_id,
        'chapter_id': run.chapter_id, 'max_rewrite_count': run.max_rewrite_count,
        'checkpoint_data': deepcopy(checkpoint_payload(run.checkpoint_data)), 'last_error': run.last_error,
    }


async def _await_author_review(run: GenerationRun, db: AsyncSession, reason: str) -> dict[str, Any]:
    run.phase = PHASE_AWAITING_REVIEW
    run.status = STATUS_NEEDS_REVIEW
    _checkpoint_set(run, PHASE_AWAITING_REVIEW, {'reason': reason, 'resume_phase': PHASE_CRITIC})
    await db.commit()
    await run_bus.publish(str(run.id), agent='runner', event='run.needs_review', payload={'reason': reason})
    return _run_result(run)


async def _reserve_attempt(run, db, kind, slot, inputs):
    attempt_id = attempts.reserve(run, kind, slot, inputs)
    # PhysicalConnectionGuard makes a failed/unconfirmed commit stop before
    # the model; reservations and rewrite consumption share this transaction.
    await db.commit()
    return attempt_id


def _record_review(run, db, attempt_id, report, round_num):
    attempts.finish(run, attempt_id, report)
    db.add(CriticReport(
        id=uuid.UUID(attempt_id), run_id=run.id, round=round_num,
        issues_json=report.get('issues', []),
        review_json={k: v for k, v in report.items() if k != 'issues'},
    ))


async def _collect_variants(run: GenerationRun, db: AsyncSession, data: dict, *, refresh_reviews: bool = False) -> dict:
    """Checkpoint each delivered draft before evaluating it; persist every one.

    Checkpoints retain candidates even if a later context refresh or database
    write fails. The selected draft is only a starting point for revision.
    """
    from app.services.bvsr import VariantCandidate, persist_variants, rank

    saved = deepcopy(data.get('variants') or [])
    if not saved:
        saved = [{'variant_idx': 0, 'text': data['text'], 'critic_report': None,
                  'delivery_status': data.get('delivery_status', COMPLETE)}]
    requested = attempts.require_journal(run)['variant_count']

    async def checkpoint() -> None:
        data['variants'] = list(saved)
        data['bvsr'] = {'requested_count': requested, 'n': len(saved), 'winner_idx': None}
        _checkpoint_set(run, PHASE_DRAFTING, dict(data))
        await db.commit()

    await checkpoint()
    for index in range(requested):
        entry = next((v for v in saved if v['variant_idx'] == index), None)
        if entry is None:
            prepared = await _phase_planning(run, db)
            attempt_id = await _reserve_attempt(run, db, 'variant_draft', index, prepared)
            delivered = await _phase_drafting(run, db, prepared)
            attempts.finish(run, attempt_id, delivered)
            entry = {'variant_idx': index, 'text': delivered.get('text') or '',
                     'critic_report': None, 'delivery_status': delivered.get('delivery_status', COMPLETE)}
            saved.append(entry)
            await checkpoint()
        # Refresh all reviews on resume, retaining their previous receipts
        # until replacements are committed. A budget/source failure must not
        # erase the last report the author could inspect.
        if entry.get('critic_report') is not None and not refresh_reviews:
            continue
        if entry.get('delivery_status', COMPLETE) != COMPLETE:
            report = incomplete_critic_report(entry['text'], code='draft_incomplete')
        else:
            # Service/format failures return an incomplete report. Source and
            # persistence failures propagate; neither is a zero-issue review.
            attempt_id = await _reserve_attempt(run, db, 'variant_critic', index, {'text': entry['text']})
            report = await _phase_critic(run, db, entry['text'], 0)
            _record_review(run, db, attempt_id, report, 0)
        entry['critic_report'] = report
        await checkpoint()

    candidates = [VariantCandidate(v['variant_idx'], v['text'], v['critic_report'] or {}) for v in saved]
    ranked = rank(candidates)
    winner = ranked[0] if ranked else None
    stored = await persist_variants(
        db, chapter_id=run.chapter_id, run_id=run.id, candidates=candidates,
        winner_idx=winner.variant_idx if winner else None, commit=False, refresh_existing=refresh_reviews,
    )
    author_choice = next((row for row in stored if row.selected_by_user), None)
    if author_choice is not None:
        winner = next(c for c in candidates if c.variant_idx == author_choice.variant_idx)
    data['bvsr'] = {
        'requested_count': requested, 'n': len(candidates), 'reviewed_count': len(ranked),
        'winner_idx': winner.variant_idx if winner else None,
        'winner_score': winner.score if winner else None,
    }
    if winner is not None:
        data['text'] = winner.content_text
        data['delivery_status'] = next(v for v in saved if v['variant_idx'] == winner.variant_idx).get('delivery_status', COMPLETE)
    return data


async def execute_run(*, run_id: str, db: AsyncSession, resume: bool = False) -> dict[str, Any]:
    """Resume with a fresh locked read; completed results need only a read."""
    identity = uuid.UUID(str(run_id))
    try:
        async with generation_read_session(db) as reader:
            run = await _load_run(reader, identity)
            if run.status == STATUS_DONE or (not resume and run.status in {STATUS_NEEDS_REVIEW, STATUS_FAILED}):
                return _run_snapshot(run)
            project_id = run.project_id
        async with generation_execution_session(db, project_id) as execution:
            # Another actor may have finished after the first read and before
            # we acquired the lock. Never execute an identity-map snapshot.
            run = await _load_run(execution, identity)
            if run.project_id != project_id:
                raise GenerationRunUnavailable('生成任务归属已变化，请重新读取。')
            if run.status == STATUS_DONE or (not resume and run.status in {STATUS_NEEDS_REVIEW, STATUS_FAILED}):
                return _run_snapshot(run)
            await _validate_run_target(execution, project_id, run.chapter_id)
            await submissions.require_run_submission_source(execution, run)
            await _execute_run_locked(run_id=str(identity), db=execution, resume=resume)
            await execution.refresh(run)
            result = _run_snapshot(run)
        return result
    except GenerationExecutionUnavailable as exc:
        exc.run_id = str(identity)
        raise


async def _load_run(db, identity):
    from app.models.generation_run import GenerationRunSubmission
    run = await db.get(GenerationRun, identity, populate_existing=True)
    if run is not None:
        return run
    if await db.get(GenerationRunSubmission, identity) is not None:
        # A just-accepted run and receipt can become visible between these
        # READ COMMITTED queries. A newly seen receipt is not proof of deletion.
        run = await db.get(GenerationRun, identity, populate_existing=True)
        if run is not None:
            return run
        raise submissions.deleted_run_error(identity)
    raise GenerationRunUnavailable('生成任务不存在。')


async def read_run(*, run_id: str, db: AsyncSession) -> dict[str, Any]:
    """Read old/new checkpoints, or the retained tombstone after deletion."""
    identity = uuid.UUID(str(run_id))
    try:
        async with generation_read_session(db) as reader:
            run = await _load_run(reader, identity)
            return _run_snapshot(run)
    except GenerationExecutionUnavailable as exc:
        exc.run_id = str(identity)
        raise


async def _execute_run_locked(*, run_id: str, db: AsyncSession, resume: bool = False) -> dict[str, Any]:
    """State-machine core, only for callers already owning execution.

    Production callers use start_run/execute_run and their pinned session.
    """
    run = await db.get(GenerationRun, run_id)
    if run is None:
        raise ValueError(f'GenerationRun {run_id} not found')
    if run.status == STATUS_DONE:
        return _run_result(run)
    if run.status in {STATUS_NEEDS_REVIEW, STATUS_FAILED} and not resume:
        return _run_result(run)

    try:
        journal = attempts.require_journal(run)
        if resume:
            await attempts.reconcile_interrupted(run, db)
        elif journal['attempts'] and journal['attempts'][-1]['status'] == 'started':
            # Only the explicit resume path may reconcile an unknown call.
            return _run_result(run)
    except attempts.AttemptReviewRequired as exc:
        return await _await_author_review(run, db, exc.reason)

    cp = deepcopy(run.checkpoint_data or {})
    rid = str(run.id)
    if resume:
        if run.phase == PHASE_FAILED:
            run.phase = (cp.get('failure') or {}).get('phase') or (
                PHASE_CRITIC if cp.get(PHASE_DRAFTING, {}).get('text') else PHASE_PLANNING
            )
        if run.phase == PHASE_AWAITING_REVIEW:
            # A cut-off delivery needs an author decision/new generation, not
            # a reviewer blessing the fragment as a complete chapter.
            reason = (cp.get(PHASE_AWAITING_REVIEW) or {}).get('reason')
            if reason == 'draft_incomplete' or reason in attempts.STOP_REASONS:
                return _run_result(run)
            run.phase = PHASE_DRAFTING if reason == 'variant_reviews_incomplete' else PHASE_CRITIC
        elif run.phase in {PHASE_REWRITE, PHASE_FINALIZE}:
            # A prior receipt binds the text, not the sources at this restart.
            run.phase = PHASE_CRITIC
        run.status = STATUS_RUNNING
        run.last_error = ''

    await run_bus.publish(rid, agent='runner', event='run.start', payload={'phase': run.phase, 'resume': resume})
    failure_phase = run.phase
    try:
        if run.phase == PHASE_PLANNING:
            failure_phase = PHASE_PLANNING
            await run_bus.publish(rid, agent='runner', event='phase.start', payload={'phase': PHASE_PLANNING})
            data = await _phase_planning(run, db)
            _checkpoint_set(run, PHASE_PLANNING, data)
            cp[PHASE_PLANNING] = data
            run.phase = PHASE_DRAFTING
            await db.commit()
            await run_bus.publish(rid, agent='runner', event='phase.end', payload={'phase': PHASE_PLANNING})
        elif run.phase == PHASE_DRAFTING:
            failure_phase = PHASE_DRAFTING
            data = await _phase_planning(run, db)
            _checkpoint_set(run, PHASE_PLANNING, data)
            cp[PHASE_PLANNING] = data
            await db.commit()

        if run.phase == PHASE_DRAFTING:
            failure_phase = PHASE_DRAFTING
            await run_bus.publish(rid, agent='runner', event='phase.start', payload={'phase': PHASE_DRAFTING})
            data = dict(cp.get(PHASE_DRAFTING) or {})
            if not data.get('text'):
                attempt_id = await _reserve_attempt(run, db, 'draft', 0, cp.get(PHASE_PLANNING, {}))
                data = await _phase_drafting(run, db, cp.get(PHASE_PLANNING, {}))
                attempts.finish(run, attempt_id, data)
            _checkpoint_set(run, PHASE_DRAFTING, data)
            await db.commit()
            cp[PHASE_DRAFTING] = data
            if data.get('delivery_status', COMPLETE) != COMPLETE:
                return await _await_author_review(run, db, 'draft_incomplete')

            if run.chapter_id and attempts.require_journal(run)['bvsr_enabled']:
                data = await _collect_variants(run, db, data, refresh_reviews=resume)
                _checkpoint_set(run, PHASE_DRAFTING, data)
                cp[PHASE_DRAFTING] = data
                if data['bvsr']['winner_idx'] is None:
                    _checkpoint_set(run, PHASE_CRITIC, {'report': data['variants'][0]['critic_report'], 'round': 0})
                    return await _await_author_review(run, db, 'variant_reviews_incomplete')
            run.phase = PHASE_CRITIC
            await db.commit()
            await run_bus.publish(rid, agent='runner', event='phase.end', payload={'phase': PHASE_DRAFTING, 'bvsr': data.get('bvsr')})

        draft_data = cp.get(PHASE_DRAFTING) or {}
        draft_text = draft_data.get('text') or ''
        if run.phase in {PHASE_CRITIC, PHASE_REWRITE, PHASE_FINALIZE} and (
            not draft_text.strip() or draft_data.get('delivery_status', COMPLETE) != COMPLETE
        ):
            # Check the committed delivery even when a failure hid the usual
            # awaiting_review transition (for example, a notification failure).
            return await _await_author_review(run, db, 'draft_incomplete')
        # Old finalize/rewrite checkpoints cannot certify their current draft.
        if run.phase in {PHASE_FINALIZE, PHASE_REWRITE}:
            report = (cp.get(PHASE_CRITIC) or {}).get('report')
            expected = 'finalize' if run.phase == PHASE_FINALIZE else 'rewrite'
            if critic_next_action(report, draft_text, run.rewrite_count, run.max_rewrite_count) != expected:
                run.phase = PHASE_CRITIC

        while run.phase in {PHASE_CRITIC, PHASE_REWRITE}:
            if run.phase == PHASE_CRITIC:
                failure_phase = PHASE_CRITIC
                round_num = run.rewrite_count + 1
                await run_bus.publish(rid, agent='runner', event='phase.start', payload={'phase': PHASE_CRITIC, 'round': round_num})
                attempt_id = await _reserve_attempt(run, db, 'critic', run.rewrite_count, {'text': draft_text})
                report = await _phase_critic(run, db, draft_text, round_num)
                _record_review(run, db, attempt_id, report, round_num)
                cp[PHASE_CRITIC] = {'report': report, 'round': round_num}
                _checkpoint_set(run, PHASE_CRITIC, cp[PHASE_CRITIC])
                action = critic_next_action(report, draft_text, run.rewrite_count, run.max_rewrite_count)
                if action not in {'rewrite', 'finalize'}:
                    return await _await_author_review(run, db, action)
                run.phase = PHASE_REWRITE if action == 'rewrite' else PHASE_FINALIZE
                await db.commit()
                await run_bus.publish(rid, agent='runner', event='phase.end',
                                      payload={'phase': PHASE_CRITIC, 'round': round_num, 'next': run.phase})

            if run.phase == PHASE_REWRITE:
                failure_phase = PHASE_REWRITE
                await run_bus.publish(rid, agent='runner', event='phase.start', payload={'phase': PHASE_REWRITE, 'round': run.rewrite_count + 1})
                prior_count = run.rewrite_count
                attempt_id = await _reserve_attempt(run, db, 'rewrite', prior_count,
                    {'text': draft_text, 'report': cp[PHASE_CRITIC]['report']})
                delivered = await _phase_rewrite(run, db, draft_text, cp[PHASE_CRITIC]['report'])
                attempts.finish(run, attempt_id, delivered)
                history = list((run.checkpoint_data or {}).get('draft_history') or [])
                history.append({'text': draft_text, 'rewrite_count': prior_count})
                _checkpoint_set(run, 'draft_history', history)
                # Keep variant history and the latest delivered text even when
                # a revision is cut off. An empty response preserves the prior
                # manuscript but records that no complete revision arrived.
                draft_text = delivered.get('text') or draft_text
                cp[PHASE_DRAFTING] = {**cp.get(PHASE_DRAFTING, {}), **delivered, 'text': draft_text}
                _checkpoint_set(run, PHASE_DRAFTING, cp[PHASE_DRAFTING])
                run.phase = PHASE_CRITIC
                await db.commit()
                await run_bus.publish(rid, agent='runner', event='phase.end', payload={'phase': PHASE_REWRITE, 'round': run.rewrite_count})
                if delivered.get('delivery_status', COMPLETE) != COMPLETE:
                    return await _await_author_review(run, db, 'draft_incomplete')

        if run.phase == PHASE_FINALIZE:
            failure_phase = PHASE_FINALIZE
            await run_bus.publish(rid, agent='runner', event='phase.start', payload={'phase': PHASE_FINALIZE})
            _checkpoint_set(run, PHASE_FINALIZE, {'final_text': draft_text, 'editorial_status': 'needs_author_review'})
            run.phase = PHASE_DONE
            run.status = STATUS_DONE
            await db.commit()
            await run_bus.publish(rid, agent='runner', event='phase.end', payload={'phase': PHASE_FINALIZE})
            await run_bus.publish(rid, agent='runner', event='run.done', payload={'status': run.status, 'editorial_status': 'needs_author_review'})
        return _run_result(run)
    except attempts.AttemptReviewRequired as exc:
        return await _await_author_review(run, db, exc.reason)
    except GenerationExecutionUnavailable:
        # The old actor must not write a failed status after losing execution
        # or an unconfirmed commit; the last confirmed checkpoint is readable.
        raise
    except Exception as exc:
        # 2026-11-28:此前只记异常类名,于是 UI 只看到兜底文案「生成流程未完成」,
        # 而 RequiredContextUnavailable 底下有十几个不同错误码(认知未就绪、
        # 世界规则缺失、预算超限……),排查必须先进日志。把码一起记下来。
        detail = ' '.join(str(exc).split())[:200]
        logger.warning('generation run failed (%s: %s)', type(exc).__name__, detail)
        # Only previously committed candidates are claimed as retained. A
        # failed session must be rolled back before recording terminal state.
        try:
            await db.rollback()
            run = await db.get(GenerationRun, run_id)
            if run is None:
                raise GenerationRunUnavailable('生成任务已不存在。')
            if run.status == STATUS_DONE:
                # A failed progress notification cannot undo a saved result.
                return _run_result(run)
            _checkpoint_set(run, 'failure', {'phase': failure_phase, 'code': 'generation_run_failed',
                                             'reason': type(exc).__name__, 'detail': detail})
            run.status = STATUS_FAILED
            run.phase = PHASE_FAILED
            run.last_error = '生成流程未完成；已保存的候选稿可在检查点中查看。'
            await db.commit()
        except (GenerationExecutionUnavailable, GenerationRunUnavailable):
            raise
        except Exception as storage_exc:
            logger.warning('generation failure status could not be saved (%s)', type(storage_exc).__name__)
            raise GenerationExecutionUnavailable() from None
        try:
            await run_bus.publish(rid, agent='runner', event='run.failed', payload={'error': 'generation_run_failed'})
        except Exception:
            pass
        return {'status': STATUS_FAILED, 'error': '生成流程未完成，请查看已保存候选后重试。'}
