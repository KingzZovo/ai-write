"""Durable, replayable cognition extraction with source fencing."""
import asyncio
import logging
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)
from datetime import timedelta
from sqlalchemy import select, update, case, or_, and_
from sqlalchemy.dialects.postgresql import insert
from app.models.cognition import ChapterCognition as Record
from app.services.entity_sources import digest
from app.services.cognition_reader import resolve_cognition_source, load_revisions, validate_decision
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_projection import entity_project_lock, EntityProjectionBusy, now
from app.services.character_cognition import _parse_changes, validate_changes
from app.services.provider_response_guard import require_normal_finish

MAX_EXTRACTION_ATTEMPTS = 4  # Initial call + the original Celery task's 3 retries.


async def enqueue_cognition(db, source):
    record_id = uuid4()
    reopen = and_(Record.status == 'obsolete', Record.source_mode == 'auto')
    await db.execute(insert(Record).values(id=record_id, cycle_id=record_id,
        project_id=source.project_id, chapter_id=source.chapter_id,
        source_hash=source.source_hash, source_json=source.receipt, status='pending')
        .on_conflict_do_update(constraint='uq_chapter_cognition_source', set_={
            'status': case((reopen, 'pending'), else_=Record.status),
            'cycle_id': case((reopen, record_id), else_=Record.cycle_id),
            'attempt_token': case((reopen, None), else_=Record.attempt_token),
            'delivery_id': case((reopen, None), else_=Record.delivery_id),
            'attempt_count': case((reopen, 0), else_=Record.attempt_count),
            'updated_at': case((reopen, now()), else_=Record.updated_at),
        }))


async def _check_attempt(source, record_id, cycle_id, token, *, sessions, lease):
    """Revalidate before each call; no author row lock spans model IO."""
    await lease.check()
    async with sessions() as db:
        current = await resolve_cognition_source(db, source.project_id, source.chapter_id)
        row = await db.get(Record, record_id)
        if row is None:
            return {'status': 'obsolete'}
        if (row.source_mode != 'auto' or row.cycle_id != cycle_id
                or row.attempt_token != token or row.status != 'running'):
            return {'status': 'superseded'}
        if current != source:
            await lease.check()
            row.status = 'obsolete'
            row.attempt_token = None
            row.updated_at = now()
            await db.commit()
            return {'status': 'obsolete'}
        revisions = await load_revisions(db, source.project_id, {source.chapter_id: row})
        validate_decision(row, source, revisions.get(row.author_revision_id))
    return None


async def run_cognition_extraction(source, *, sessions, engine, delivery_id=None, recovery=False, cycle_id=None):
    """Run one attempt in the accepted cycle, never allocate a retry budget.

    Periodic recovery repairs missing acknowledgements, not known model
    failures. A delayed delivery cannot acquire a new paid retry budget.
    """
    expected_cycle = UUID(str(cycle_id)) if cycle_id is not None else None
    async with sessions() as db:
        current = await resolve_cognition_source(db, source.project_id, source.chapter_id)
        if current != source:
            fence = Record.cycle_id == expected_cycle if expected_cycle else Record.cycle_id == Record.id
            await db.execute(update(Record).where(Record.project_id == source.project_id,
                Record.chapter_id == source.chapter_id, Record.source_hash == source.source_hash, fence,
                Record.source_mode == 'auto', Record.status.in_(['pending', 'queued', 'running'])).values(
                status='obsolete', attempt_token=None, updated_at=now()))
            await db.commit()
            return {'status': 'obsolete'}
        row = await db.scalar(select(Record).where(Record.chapter_id == source.chapter_id,
                                                    Record.source_hash == source.source_hash))
        if row is None:
            if expected_cycle is not None:
                return {'status': 'obsolete'}
            await enqueue_cognition(db, source)
            row = await db.scalar(select(Record).where(Record.chapter_id == source.chapter_id,
                                                        Record.source_hash == source.source_hash))
        # Old three-argument messages are valid only in the original cycle.
        if row.source_mode != 'auto' or row.cycle_id != (expected_cycle or row.id):
            return {'status': 'superseded'}
        if row.status == 'obsolete':
            return {'status': 'obsolete'}
        record_id, claimed_cycle = row.id, row.cycle_id
        await db.commit()
    async with entity_project_lock(engine, source.project_id) as lease:
        if not lease:
            raise EntityProjectionBusy('another fact worker owns this project')
        await lease.check()
        async with sessions() as db:
            current = await resolve_cognition_source(db, source.project_id, source.chapter_id)
            row = await db.get(Record, record_id)
            if row is None:
                return {'status': 'obsolete'}
            if row.source_mode != 'auto' or row.cycle_id != claimed_cycle:
                return {'status': 'superseded'}
            if current != source:
                row.status = 'obsolete'; row.attempt_token = None; row.updated_at = now(); await db.commit()
                return {'status': 'obsolete'}
            revisions = await load_revisions(db, source.project_id, {source.chapter_id: row})
            try:
                validate_decision(row, source, revisions.get(row.author_revision_id))
            except RequiredContextUnavailable:
                row.status = 'failed'; row.last_error = 'AuthorDecisionInvalid'; row.updated_at = now()
                await db.commit()
                return {'status': 'skipped', 'reason': 'author_decision_invalid'}
            if (row.status == 'failed'
                    and (not delivery_id or row.delivery_id != delivery_id)):
                return {'status': 'skipped', 'reason': 'model_retry_requires_original_task'}
            if (row.status == 'completed' and row.source_json == source.receipt
                    and row.payload_hash == digest([source.source_hash, row.changes_json])):
                try:
                    validate_changes(row.changes_json, source.content)
                except ValueError:
                    pass
                else:
                    return {'status': 'completed', 'reused': True}
            # An unacknowledged process exit has also consumed an attempt.
            # Re-delivery of the same broker message cannot reset this count.
            attempts = row.attempt_count
            if attempts >= MAX_EXTRACTION_ATTEMPTS:
                await lease.check()
                row.status = 'failed'
                row.last_error = row.last_error or 'CognitionRetryExhausted'
                row.attempt_token = None
                row.updated_at = now()
                await db.commit()
                return {'status': 'skipped', 'reason': 'model_retry_budget_exhausted'}
            token = uuid4()
            await lease.check()
            row.attempt_token = token
            row.source_json = source.receipt
            row.delivery_id = delivery_id or str(uuid4())
            row.attempt_count = attempts + 1
            row.status = 'running'; row.updated_at = now(); row.last_error = None
            await db.commit()
        try:
            from app.services.prompt_registry import run_text_prompt
            user = '【完整章节正文】\n' + source.content
            async with asyncio.timeout(180):
                for attempt in range(2):
                    stale = await _check_attempt(source, record_id, claimed_cycle, token, sessions=sessions, lease=lease)
                    if stale:
                        return stale
                    await lease.check()
                    result = await run_text_prompt(task_type='chapter_cognition', user_content=user,
                        db=None, project_id=source.project_id,
                        chapter_id=source.chapter_id, temperature=0.2)
                    require_normal_finish(getattr(result, 'finish_reason', None))
                    try:
                        changes = _parse_changes(result.text, source.content)
                        break
                    except ValueError as exc:
                        offenders = getattr(exc, 'offending_evidence', None) or []
                        # 把点名到的依据记下来:没有它就只能看到一句"无法逐字定位",
                        # 无法判断该收紧判据还是加强提示(2026-11-14 与本次都卡在这里)。
                        if offenders:
                            logger.warning(
                                "Cognition evidence unlocatable chapter=%s attempt=%s/%s offenders=%s",
                                source.chapter_id, attempt + 1, 3,
                                [item[:80] for item in offenders[:3]],
                            )
                        if attempt:
                            raise
                        # 逐字依据不合格时点名具体哪几条找不到:实测只说"重新输出",
                        # 模型仍会继续改写;点名之后才会回正文里复制。
                        named = ''.join(f'- {item[:60]}\n' for item in offenders)
                        hint = ('下面这些依据在正文中找不到,属于改写或拼接。'
                                '请回到正文里复制连续原文,不要改写、不要加省略号:\n' + named
                                if named else '')
                        user = ('前次输出格式或逐字依据不合格。' + hint +
                                '重新读取完整正文,只输出符合约定的数组;'
                                '没有变化返回 [],不能省略正文中的已明确变化。\n【完整章节正文】\n' + source.content)
            await lease.check()
            async with sessions() as db:
                current = await resolve_cognition_source(db, source.project_id, source.chapter_id)
                if current != source:
                    await db.execute(update(Record).where(Record.id == record_id, Record.cycle_id == claimed_cycle,
                        Record.source_mode == 'auto', Record.attempt_token == token).values(
                        status='obsolete', attempt_token=None, updated_at=now()))
                    await db.commit()
                    return {'status': 'obsolete'}
                await lease.check()
                written = await db.scalar(update(Record).where(Record.id == record_id, Record.cycle_id == claimed_cycle,
                    Record.source_mode == 'auto', Record.attempt_token == token,
                    Record.status == 'running').values(
                    changes_json=changes, payload_hash=digest([source.source_hash, changes]),
                    status='completed', last_error=None, updated_at=now()).returning(Record.id))
                await db.commit()
                if written is None:
                    return {'status': 'superseded'}
            return {'status': 'completed', 'changes': len(changes)}
        except (Exception, asyncio.CancelledError) as exc:
            async with sessions() as db:
                current = await resolve_cognition_source(db, source.project_id, source.chapter_id)
                status = 'failed' if current == source else 'obsolete'
                await db.execute(update(Record).where(Record.id == record_id,
                    Record.source_mode == 'auto', Record.cycle_id == claimed_cycle,
                    Record.attempt_token == token, Record.status == 'running').values(
                    status=status, last_error=type(exc).__name__,
                    attempt_token=token if status == 'failed' else None, updated_at=now()))
                await db.commit()
            if status == 'obsolete' and not isinstance(exc, asyncio.CancelledError):
                return {'status': 'obsolete'}
            raise


async def replay_pending_cognition(sessions, *, dispatch=None, limit=20):
    from app.services.cognition_backfill import advance_backfills
    backfill_processed = await advance_backfills(sessions)
    if dispatch is None:
        from app.tasks.entity_tasks import extract_chapter_cognition
        dispatch = lambda pid, cid, shash, cycle: extract_chapter_cognition.delay(pid, cid, shash, cycle)
    due = and_(Record.source_mode == 'auto', or_(Record.status == 'pending',
        and_(Record.status.in_(['running','queued']), Record.updated_at < now() - timedelta(minutes=5))))
    async with sessions() as db:
        rows = (await db.execute(select(Record.id, Record.project_id, Record.chapter_id, Record.source_hash, Record.cycle_id)
            .where(due)
            .order_by(Record.updated_at).limit(min(max(limit, 1), 20)))).all()
    sent = 0
    for row in rows:
        async with sessions() as db:
            claimed = await db.scalar(update(Record).where(Record.id == row.id,
                Record.cycle_id == row.cycle_id, due)
                .values(status='queued', updated_at=now()).returning(Record.id))
            await db.commit()
        if not claimed:
            continue
        try:
            dispatch(str(row.project_id), str(row.chapter_id), row.source_hash, str(row.cycle_id))
            sent += 1
        except Exception:
            # The committed queue receipt expires and is retried even if
            # publishing fails or the process crashes before reaching here.
            continue
    return {'examined': len(rows), 'dispatched': sent, 'backfill_processed': backfill_processed}
