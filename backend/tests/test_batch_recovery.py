"""Recover real SQLite transactions; only the PG lease and model are replaced."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, ChapterVersion
from app.services import batch_generator
from app.services.batch_execution import StaleBatchExecution, MAX_ITEM_ATTEMPTS, batch_execution
from app.services.batch_receipts import receipt_matches
from app.services.batch_submissions import BatchSubmissionError, claim_batch, record_item, submit_batch
from app.services.batch_recovery import recover_batch
from app.services.generation_submissions import claim_generation_submission, generation_task_results
from app.services.production.fencing import standalone_write_fence
from app.tasks.generation_tasks import _run_guarded_generation
from tests.test_batch_generation_submissions import batch, batch_worker, configs, selection, save_generated
from tests.test_generation_submissions import book, queued, worker

NOW = datetime.now(timezone.utc)
DRAFT = '沈青把两千元塞进弟弟手里，等他接稳了，才蹲下身。'


def stored(r, model):
    r.session.expire_all()
    query = sa.select(model)
    if model is GenerationBatchItem:
        query = query.order_by(model.position)
    return list(r.session.scalars(query))


async def expire(r, rid):
    await r.db.rollback()
    with r.engine.begin() as connection:
        connection.execute(sa.update(GenerationBatch).where(GenerationBatch.id == rid)
            .values(heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=120)))


async def abandoned(r, mode='empty'):
    rid = uuid4()
    await submit_batch(r.db, r.pid, rid, configs(selection(r)))
    original = r.implementation.side_effect
    async with r.batch_lease(r.db, rid):
        worker_id = await claim_batch(r.db, r.pid, rid)
        item = stored(r, GenerationBatchItem)[0]
        await r.db.rollback()
        if mode != 'before_item':
            await record_item(r.db, rid, worker_id, item.task_id, 'generating')
            async def draft(task_id, *, session_factory):
                async with session_factory() as db:
                    task = await db.get(GenerationTask, task_id)
                    if mode in ('saved', 'complete', 'observed'):
                        chapter = await db.get(Chapter, task.params_json['chapter_id'])
                        chapter.content_text, chapter.word_count, chapter.status = DRAFT, len(DRAFT), 'completed'
                        if mode != 'saved':
                            task.status, task.result_text = 'completed', DRAFT
                        await db.commit()
            r.implementation.side_effect = draft
            with batch_execution(rid, worker_id, item.task_id):
                await _run_guarded_generation(str(item.task_id))
            if mode == 'observed':
                await record_item(r.db, rid, worker_id, item.task_id, 'completed', len(DRAFT))
    r.implementation.side_effect = original
    r.implementation.reset_mock()
    await expire(r, rid)
    return rid, item.task_id, worker_id


async def resume(r, rid):
    async with r.sessions() as db:
        return await batch().run_submitted(r.pid, rid, db=db, recover=True)


@pytest.mark.asyncio
async def test_manuscript_receipt_commits_and_rolls_back_with_the_entire_body(batch_worker):
    r = batch_worker
    rid = uuid4()
    await submit_batch(r.db, r.pid, rid, configs(selection(r)))
    before = selection(r)[0].content_text
    async with r.batch_lease(r.db, rid):
        wid = await claim_batch(r.db, r.pid, rid)
        item = stored(r, GenerationBatchItem)[0]
        await r.db.rollback()
        await record_item(r.db, rid, wid, item.task_id, 'generating')
        with batch_execution(rid, wid, item.task_id):
            async with r.sessions() as db:
                assert await claim_generation_submission(db, item.task_id)
                chapter = await db.get(Chapter, item.chapter_id)
                with standalone_write_fence(r.pid, chapter.id, chapter.updated_at, item.task_id), generation_task_results(item.task_id):
                    chapter.content_text, chapter.status = DRAFT, 'completed'
                    await db.flush()
                    proof = await db.get(GenerationBatchItem, item.task_id, populate_existing=True)
                    assert receipt_matches(proof, chapter)
                    with Session(r.engine) as observer:
                        assert observer.get(Chapter, item.chapter_id).content_text == before
                        assert observer.get(GenerationBatchItem, item.task_id).saved_receipt_json is None
                    await db.rollback()
                assert (await db.get(Chapter, item.chapter_id)).content_text == before
                assert (await db.get(GenerationBatchItem, item.task_id)).saved_receipt_json is None
                chapter = await db.get(Chapter, item.chapter_id)
                with standalone_write_fence(r.pid, chapter.id, chapter.updated_at, item.task_id), generation_task_results(item.task_id):
                    chapter.content_text, chapter.status = DRAFT, 'completed'
                    await db.commit()
    assert stored(r, Chapter)[0].content_text == DRAFT
    assert stored(r, GenerationBatchItem)[0].saved_receipt_json['word_count'] == len(DRAFT)


@pytest.mark.asyncio
async def test_body_saved_before_task_finish_is_kept_for_review_and_next_chapter_stops(batch_worker):
    r = batch_worker
    rid, task_id, _ = await abandoned(r, 'saved')
    proof = deepcopy(stored(r, GenerationBatchItem)[0].saved_receipt_json)
    job = await resume(r, rid)
    assert job.status.value == 'needs_review' and job.completed_chapters == 0
    assert [item.status for item in job.results] == ['needs_review', 'cancelled']
    assert selection(r)[0].content_text == DRAFT
    assert stored(r, GenerationBatchItem)[0].saved_receipt_json == proof
    assert r.session.get(GenerationTask, task_id).status == 'needs_review'
    r.implementation.assert_not_awaited()
    await resume(r, rid)
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
async def test_finished_task_repairs_missing_progress_without_regenerating_the_saved_chapter(batch_worker):
    r = batch_worker
    rid, task_id, old_worker = await abandoned(r, 'complete')
    job = await resume(r, rid)
    assert job.status.value == 'completed' and job.completed_chapters == 2
    assert r.implementation.await_count == 1
    assert r.implementation.await_args.args[0] != str(task_id)
    assert selection(r)[0].content_text == DRAFT
    parent = stored(r, GenerationBatch)[0]
    assert parent.worker_id != old_worker and parent.recovery_count == 1
    first = stored(r, GenerationBatchItem)[0]
    assert first.attempt_count == 1 and first.attempts_json[-1]['recovery']['outcome'] == 'delivered'


@pytest.mark.asyncio
async def test_completed_observation_remains_historical_after_author_edit(batch_worker):
    r = batch_worker
    rid, task_id, _ = await abandoned(r, 'observed')
    first = selection(r)[0]
    first.content_text, first.status = '作者在已完成稿上补写的新句子。', 'draft'
    r.session.commit()
    job = await resume(r, rid)
    assert job.results[0].status == 'completed' and job.results[0].word_count == len(DRAFT)
    assert selection(r)[0].content_text == '作者在已完成稿上补写的新句子。'
    assert r.implementation.await_count == 1
    assert r.implementation.await_args.args[0] != str(task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['empty', 'before_item'])
async def test_no_delivery_reuses_original_request_and_sources_with_bounded_attempts(batch_worker, mode):
    r = batch_worker
    rid, task_id, _ = await abandoned(r, mode)
    original = {task.id: deepcopy(task.params_json) for task in stored(r, GenerationTask)}
    job = await resume(r, rid)
    assert job.status.value == 'completed' and r.implementation.await_count == 2
    assert {task.id: task.params_json for task in stored(r, GenerationTask)} == original
    assert len(stored(r, GenerationTask)) == 2
    first = stored(r, GenerationBatchItem)[0]
    assert first.attempt_count == (2 if mode == 'empty' else 1)
    if mode == 'empty':
        assert first.attempts_json[0]['recovery']['outcome'] == 'retry_without_delivery'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('content_text', '作者后来写的新稿。'),
    ('outline_json', {'summary': '改成先求医。'}), ('title', '新的章名'), ('chapter_idx', 3)])
async def test_author_change_blocks_recovery_without_refreshing_the_submission_source(batch_worker, field, value):
    r = batch_worker
    rid, task_id, _ = await abandoned(r)
    source = deepcopy(r.session.get(GenerationTask, task_id).params_json['submission'])
    setattr(selection(r)[0], field, value)
    r.session.commit()
    job = await resume(r, rid)
    assert job.status.value == 'needs_review'
    assert r.session.get(GenerationTask, task_id).params_json['submission'] == source
    r.session.expire_all()
    assert getattr(r.session.get(Chapter, r.cid), field) == value
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('progress_text', '已经输出的半段正文。' * 2000),
    ('result_text', '尚未保存的整段结果。'), ('polished_text', '独立润色候选。'), ('char_count', 1)])
async def test_any_retained_output_is_preserved_and_never_discarded_for_a_retry(batch_worker, field, value):
    r = batch_worker
    rid, task_id, _ = await abandoned(r)
    task = r.session.get(GenerationTask, task_id)
    setattr(task, field, value)
    r.session.commit()
    job = await resume(r, rid)
    assert job.status.value == 'needs_review' and job.results[1].status == 'cancelled'
    r.session.expire_all()
    assert getattr(r.session.get(GenerationTask, task_id), field) == value
    assert stored(r, GenerationBatchItem)[0].attempt_count == 1
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
async def test_attempt_budget_is_never_reset_by_process_recovery(batch_worker):
    r = batch_worker
    rid, task_id, wid = await abandoned(r)
    item = stored(r, GenerationBatchItem)[0]
    item.attempt_count = MAX_ITEM_ATTEMPTS
    item.attempts_json = [{'worker_id': str(wid), 'started_at': NOW.isoformat()} for _ in range(MAX_ITEM_ATTEMPTS)]
    r.session.commit()
    assert (await resume(r, rid)).status.value == 'needs_review'
    assert stored(r, GenerationBatchItem)[0].attempt_count == MAX_ITEM_ATTEMPTS
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['legacy', 'missing_heartbeat', 'fresh', 'cancelled'])
async def test_unknown_live_or_deliberately_cancelled_executions_are_not_resurrected(batch_worker, case):
    r = batch_worker
    rid, task_id, _ = await abandoned(r)
    row = stored(r, GenerationBatch)[0]
    if case == 'legacy': row.execution_version = 0
    elif case == 'missing_heartbeat': row.heartbeat_at = None
    elif case == 'fresh': row.heartbeat_at = datetime.now(timezone.utc)
    else: row.status = 'interrupted'
    original_worker = row.worker_id
    r.session.commit()
    await resume(r, rid)
    assert stored(r, GenerationBatch)[0].worker_id == original_worker
    assert stored(r, GenerationBatchItem)[0].attempt_count == 1
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('lock', ['batch', 'project'])
async def test_timeout_does_not_override_an_actually_held_execution_lock(batch_worker, lock):
    r = batch_worker
    rid, task_id, wid = await abandoned(r)
    if lock == 'batch': r.batch_held.add(str(rid))
    else: r.writer_lock.return_value = False
    job = await resume(r, rid)
    assert job.status.value == 'running'
    assert stored(r, GenerationBatch)[0].worker_id == wid
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('write', ['body', 'result', 'bulk_failure', 'version'])
async def test_replaced_nonce_fences_all_old_task_writes_including_fresh_sessions(batch_worker, write):
    r = batch_worker
    rid = uuid4()
    await submit_batch(r.db, r.pid, rid, configs(selection(r)))
    async with r.batch_lease(r.db, rid):
        wid = await claim_batch(r.db, r.pid, rid)
        item = stored(r, GenerationBatchItem)[0]
        await r.db.rollback()
        await record_item(r.db, rid, wid, item.task_id, 'generating')
        with batch_execution(rid, wid, item.task_id), generation_task_results(item.task_id):
            # Raw test-owned connection emulates an already committed takeover.
            with r.engine.begin() as connection:
                connection.execute(sa.update(GenerationBatch).where(GenerationBatch.id == rid).values(worker_id=uuid4()))
            async with r.sessions() as db:
                chapter = await db.get(Chapter, item.chapter_id)
                original = chapter.content_text
                with standalone_write_fence(r.pid, chapter.id, chapter.updated_at, item.task_id):
                    with pytest.raises(StaleBatchExecution):
                        if write == 'body': chapter.content_text = '不应覆盖的旧执行正文。'
                        elif write == 'result': (await db.get(GenerationTask, item.task_id)).result_text = '迟到结果。'
                        elif write == 'version': db.add(ChapterVersion(chapter_id=chapter.id, content_text='迟到候选。'))
                        else: await db.execute(sa.update(GenerationTask).where(GenerationTask.id == item.task_id).values(status='failed'))
                        await db.commit()
                    await db.rollback()
                    assert (await db.get(Chapter, item.chapter_id)).content_text == original
    assert stored(r, GenerationBatchItem)[0].saved_receipt_json is None
    assert r.session.get(GenerationTask, item.task_id).status == 'pending'


@pytest.mark.asyncio
async def test_lost_batch_heartbeat_drains_the_worker_and_keeps_recoverable_progress(batch_worker, monkeypatch):
    r = batch_worker
    monkeypatch.setattr(batch_generator, 'HEARTBEAT_SECONDS', .02)
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def wait(task_id, *, session_factory):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    r.implementation.side_effect = wait
    rid = uuid4()
    async def run():
        async with r.sessions() as db:
            return await batch().generate_batch(r.pid, configs(selection(r)), db=db, request_id=rid)
    pending = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 3)
    r.batch_leases[-1].connection.execute.side_effect = RuntimeError('test lease disconnected')
    with pytest.raises(StaleBatchExecution):
        await asyncio.wait_for(pending, 3)
    assert stopped.is_set() and not r.batch_held
    assert stored(r, GenerationBatch)[0].status == 'running'
    assert all(item.saved_receipt_json is None for item in stored(r, GenerationBatchItem))


@pytest.mark.asyncio
async def test_second_crash_after_recovery_but_before_chapter_start_does_not_consume_an_attempt(batch_worker):
    r = batch_worker
    rid, task_id, _ = await abandoned(r)
    async with r.batch_lease(r.db, rid):
        assert await recover_batch(r.db, r.pid, rid) is not None
    assert stored(r, GenerationBatchItem)[0].attempt_count == 1
    await expire(r, rid)
    job = await resume(r, rid)
    assert job.status.value == 'completed' and r.implementation.await_count == 2
    assert stored(r, GenerationBatchItem)[0].attempt_count == 2
    assert stored(r, GenerationBatch)[0].recovery_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['needs_review', 'error', 'cancelled'])
async def test_previously_recorded_review_or_failure_stops_waiting_chapters_during_recovery(batch_worker, status):
    r = batch_worker
    rid, task_id, _ = await abandoned(r, 'observed')
    stored(r, GenerationBatchItem)[0].status = status
    r.session.commit()
    job = await resume(r, rid)
    assert job.status.value == 'needs_review'
    assert [item.status for item in job.results] == [status, 'cancelled']
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('started', [False, True])
async def test_missing_manifest_member_is_quarantined_without_recreating_a_task(batch_worker, started):
    r = batch_worker
    if started:
        rid, _, _ = await abandoned(r)
    else:
        rid = uuid4()
        await submit_batch(r.db, r.pid, rid, configs(selection(r)))
    missing = stored(r, GenerationBatchItem)[-1]
    r.session.delete(missing)
    r.session.commit()
    with pytest.raises(BatchSubmissionError, match='清单不完整'):
        await resume(r, rid)
    assert stored(r, GenerationBatch)[0].status == 'needs_review'
    assert len(stored(r, GenerationTask)) == 2
    assert len(stored(r, GenerationBatchItem)) == 1
    assert all(task.status not in ('pending', 'running') for task in stored(r, GenerationTask))
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
async def test_five_million_character_recovery_checks_all_unstarted_sources(batch_worker, record_testsuite_property):
    from time import monotonic
    r = batch_worker
    originals = selection(r)
    volume_id = originals[0].volume_id
    text = ('弟弟收下两千元，把信封放回内袋，腾出手扶住墙。' * 220)[:5050]
    for chapter in originals:
        chapter.content_text, chapter.word_count = text, len(text)
    for index in range(3, 1006):
        r.session.add(Chapter(volume_id=volume_id, chapter_idx=index, global_idx=index, title=f'第{index}章',
            content_text=text, word_count=len(text), outline_json={'summary': '先收好钱，再扶墙站稳。'}))
    r.session.commit()
    assert sum(len(ch.content_text) for ch in selection(r)) > 5_000_000
    rid, _, _ = await abandoned(r, 'before_item')
    started = monotonic()
    async with r.batch_lease(r.db, rid):
        assert await recover_batch(r.db, r.pid, rid) is not None
    record_testsuite_property('batch_recovery_seconds', round(monotonic() - started, 3))
    items = stored(r, GenerationBatchItem)
    assert len(items) == 1005 and all(item.status == 'pending' and item.attempt_count == 0 for item in items)
    assert len(stored(r, GenerationTask)) == 1005
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('recovering', [False, True])
async def test_actual_summary_cas_does_not_invalidate_a_saved_manuscript_receipt(batch_worker, monkeypatch, recovering):
    from app.services import chapter_summarizer
    from unittest.mock import AsyncMock
    r = batch_worker
    text = DRAFT * 25
    summary = '沈青交付两千元，弟弟收好钱后扶墙站起。'
    monkeypatch.setattr(chapter_summarizer, 'summarize_chapter_text', AsyncMock(return_value=summary))
    async def generate(task_id, *, session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            chapter = await db.get(Chapter, task.params_json['chapter_id'])
            chapter.content_text, chapter.status = text, 'completed'
            await db.commit()
            assert (await chapter_summarizer.summarize_and_save_chapter(chapter_id=chapter.id,
                db=db, overwrite=True, project_vectors=False)) == (True, summary)
            task.result_text, task.status = text, 'completed'
            await db.commit()
    r.implementation.side_effect = generate
    rid = uuid4()
    await submit_batch(r.db, r.pid, rid, configs(selection(r)))
    if recovering:
        async with r.batch_lease(r.db, rid):
            wid = await claim_batch(r.db, r.pid, rid)
            first = stored(r, GenerationBatchItem)[0]
            await r.db.rollback()
            await record_item(r.db, rid, wid, first.task_id, 'generating')
            with batch_execution(rid, wid, first.task_id):
                await _run_guarded_generation(str(first.task_id))
        await expire(r, rid)
        r.implementation.reset_mock()
        job = await resume(r, rid)
        assert r.implementation.await_count == 1
    else:
        async with r.sessions() as db:
            job = await batch().run_submitted(r.pid, rid, db=db)
    assert job.status.value == 'completed' and job.completed_chapters == 2
    for item, chapter in zip(stored(r, GenerationBatchItem), selection(r)):
        assert chapter.summary == summary and receipt_matches(item, chapter)


@pytest.mark.asyncio
async def test_repeated_recovery_appends_the_original_interruption_history(batch_worker):
    r = batch_worker
    rid, _, _ = await abandoned(r)
    async with r.batch_lease(r.db, rid):
        await recover_batch(r.db, r.pid, rid)
    first = deepcopy(stored(r, GenerationBatchItem)[0].attempts_json[0]['recoveries'])
    assert len(first) == 1 and first[0]['task_status'] == 'running'
    await expire(r, rid)
    async with r.batch_lease(r.db, rid):
        await recover_batch(r.db, r.pid, rid)
    attempt = stored(r, GenerationBatchItem)[0].attempts_json[0]
    assert len(attempt['recoveries']) == 2 and attempt['recoveries'][:1] == first
    assert attempt['recoveries'][1]['task_status'] == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('summary', '作者重写的摘要。'), ('title', '作者新的章名'),
    ('outline_json', {'summary': '作者改变了此章安排。'}), ('content_text', '作者接着改过的正文。')])
async def test_manuscript_receipt_still_rejects_author_revisions_after_delivery(batch_worker, field, value):
    r = batch_worker
    rid, _, _ = await abandoned(r, 'complete')
    setattr(selection(r)[0], field, value)
    r.session.commit()
    job = await resume(r, rid)
    assert job.status.value == 'needs_review' and job.results[1].status == 'cancelled'
    r.session.expire_all()
    assert getattr(r.session.get(Chapter, r.cid), field) == value
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('history', [{'unexpected': 'keep'}, 'unknown history', ['unknown entry']])
async def test_malformed_attempt_history_is_kept_for_review_without_crashing_recovery(batch_worker, history):
    r = batch_worker
    rid, _, _ = await abandoned(r)
    item = stored(r, GenerationBatchItem)[0]
    item.attempts_json = history
    r.session.commit()
    assert (await resume(r, rid)).status.value == 'needs_review'
    assert stored(r, GenerationBatchItem)[0].attempts_json == history
    r.implementation.assert_not_awaited()
