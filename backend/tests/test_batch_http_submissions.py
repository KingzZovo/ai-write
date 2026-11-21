"""Actual batch HTTP, canonical claims and SQLite commit uncertainty offline."""
import asyncio
from copy import deepcopy
import json
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import pytest
import sqlalchemy as sa

from app.api import generate, rewrite
from app.db.session import get_db
from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Project, Volume
from app.services import batch_generator, batch_submissions
from app.services.batch_submissions import (
    BatchSubmissionError, batch_execution, batch_status, claim_batch, record_item, submit_batch,
)
from app.tasks.generation_tasks import _run_guarded_generation
from tests.test_batch_generation_submissions import batch, batch_worker, configs, selection, save_generated
from tests.test_generation_submissions import book, queued, worker


@pytest.fixture
def batch_http(batch_worker, monkeypatch):
    r = batch_worker
    monkeypatch.setattr(rewrite, 'async_session_factory', r.sessions)
    monkeypatch.setattr(batch_generator, 'BatchGenerator', batch)
    async def database():
        try: yield r.db
        finally: await r.db.rollback()
    app = FastAPI()
    app.include_router(rewrite.router)
    app.include_router(generate.router)
    app.dependency_overrides[get_db] = database
    r.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test')
    r.batch_request = {'request_id': str(uuid4()), 'project_id': str(r.pid), 'chapter_configs': configs(selection(r)),
                       'style_instruction': '金额按两千元，动作按先交钱后背人。'}
    return r


async def post(r, **changes):
    async with r.client() as client:
        return await client.post('/api/generate/batch', json={**r.batch_request, **changes})


def events(response):
    assert response.status_code == 200, response.text
    assert response.text.endswith('data: [DONE]\n\n')
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]


def persisted(r, model):
    r.session.expire_all()
    return list(r.session.scalars(sa.select(model)))


@pytest.mark.asyncio
async def test_http_retries_read_original_batch_without_new_tasks_or_model_calls(batch_http):
    r = batch_http
    first = events(await post(r))
    assert first[0]['status'] == 'accepted' and first[0]['job_id'] == r.batch_request['request_id']
    assert first[-1]['status'] == 'completed' and first[-1]['completed'] == 2
    tasks = persisted(r, GenerationTask)
    sources = {str(task.id): deepcopy(task.params_json) for task in tasks}
    assert len(tasks) == 2 and r.implementation.await_count == 2
    for chapter in selection(r): chapter.content_text = '批次结束以后作者又改过的稿。'
    r.session.commit()
    again = events(await post(r))
    assert again == first
    assert r.implementation.await_count == 2
    assert {str(task.id): task.params_json for task in persisted(r, GenerationTask)} == sources
    assert len(persisted(r, GenerationBatch)) == 1
    async with r.client() as client:
        saved = await client.get(f'/api/generate/batch/{r.batch_request["request_id"]}', params={'project_id': str(r.pid)})
    assert saved.status_code == 200 and saved.json()['completed'] == 2
    assert len(saved.json()['results']) == 2
    r.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change, status', [('request_id', 428), ('body', 428), ('stale', 409),
    ('foreign', 404), ('duplicate', 422), ('volume', 409), ('idx', 422), ('unknown_field', 422)])
async def test_rejected_http_batch_never_partly_creates_children(batch_http, change, status):
    r = batch_http
    if change == 'request_id': r.batch_request.pop('request_id')
    else:
        item = r.batch_request['chapter_configs'][-1]
        if change == 'body': item.pop('expected_content_text')
        elif change == 'stale': item['expected_content_text'] += '旧稿的差异'
        elif change == 'foreign': item['chapter_id'] = str(uuid4())
        elif change == 'duplicate': r.batch_request['chapter_configs'].append(deepcopy(item))
        elif change == 'volume': item['volume_id'] = str(uuid4())
        elif change == 'idx': item['chapter_idx'] = 99
        else: item['force'] = True
    response = await post(r)
    assert response.status_code == status, response.text
    for model in (GenerationBatch, GenerationBatchItem, GenerationTask): assert not persisted(r, model)
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['style', 'body', 'selection', 'project'])
async def test_batch_number_cannot_be_rebound_to_another_request(batch_http, change):
    r = batch_http
    await post(r)
    if change == 'style': r.batch_request['style_instruction'] = '另一组创作要求。'
    elif change == 'body': r.batch_request['chapter_configs'][0]['expected_content_text'] += '更改'
    elif change == 'selection': r.batch_request['chapter_configs'].pop()
    else: r.batch_request['project_id'] = str(uuid4())
    response = await post(r)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'batch_request_conflict'
    assert r.implementation.await_count == 2 and len(persisted(r, GenerationTask)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_unconfirmed_acceptance_returns_same_number_and_does_not_start_models(batch_http, mode):
    r = batch_http
    r.fail_commit, r.lose_ack = mode == 'before', mode == 'after'
    response = await post(r)
    assert response.status_code == 503 and response.json()['detail']['request_id'] == r.batch_request['request_id']
    assert 'private' not in response.text
    r.implementation.assert_not_awaited()
    assert len(persisted(r, GenerationBatch)) == int(mode == 'after')
    assert events(await post(r))[-1]['status'] == 'completed'
    assert len(persisted(r, GenerationTask)) == 2 and r.implementation.await_count == 2


@pytest.mark.asyncio
async def test_repeated_active_http_request_observes_without_taking_execution(batch_http):
    r = batch_http
    req = r.batch_request
    await submit_batch(r.db, r.pid, req['request_id'], req['chapter_configs'], req['style_instruction'])
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(task_id, *, session_factory):
        entered.set()
        await release.wait()
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            await save_generated(db, task)
    r.implementation.side_effect = hold
    async def run():
        async with r.sessions() as db:
            return await batch().run_submitted(str(r.pid), req['request_id'], db=db)
    executing = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        again = events(await post(r))
        assert again[-1]['status'] == 'running'
        assert r.implementation.await_count == 1 and len(persisted(r, GenerationTask)) == 2
    finally:
        release.set()
        await executing


@pytest.mark.asyncio
async def test_batch_children_cannot_be_launched_as_independent_tasks(batch_http):
    r = batch_http
    req = r.batch_request
    await submit_batch(r.db, r.pid, req['request_id'], req['chapter_configs'], req['style_instruction'])
    children = persisted(r, GenerationTask)
    for child in children: await _run_guarded_generation(str(child.id))
    r.implementation.assert_not_awaited()
    assert all(child.status == 'pending' for child in persisted(r, GenerationTask))
    child = children[0]
    async with r.client() as client:
        forged = await client.post('/api/generate/async', json={
            'request_id': str(child.id), 'project_id': str(r.pid), 'task_type': 'chapter',
            'chapter_id': child.params_json['chapter_id'], 'expected_content_text': req['chapter_configs'][0]['expected_content_text'],
            'params': {'batch_request_id': req['request_id']},
        })
    assert forged.status_code == 422
    assert events(await post(r))[-1]['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_unknown_claim_never_invokes_models_or_changes_original_request(batch_http, mode):
    r = batch_http
    req = r.batch_request
    await submit_batch(r.db, r.pid, req['request_id'], req['chapter_configs'])
    r.fail_commit, r.lose_ack = mode == 'before', mode == 'after'
    with pytest.raises(BatchSubmissionError, match='提交尚未确认'):
        await batch().run_submitted(str(r.pid), req['request_id'], db=r.db)
    r.implementation.assert_not_awaited()
    row = persisted(r, GenerationBatch)[0]
    assert row.status == ('pending' if mode == 'before' else 'running')
    if mode == 'after':
        result = await batch().run_submitted(str(r.pid), req['request_id'], db=r.db)
        assert result.status.value == 'running'
        r.implementation.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_keeps_completed_progress_and_does_not_restart_original_batch(batch_http):
    r = batch_http
    volume_id = selection(r)[0].volume_id
    r.session.add(Chapter(volume_id=volume_id, chapter_idx=3, global_idx=3, title='尚未开始',
        outline_json={'summary': '带弟弟看腿。'}, content_text='第三章原稿。', word_count=6))
    r.session.commit()
    r.batch_request['chapter_configs'] = configs(selection(r))
    entered, stopped = asyncio.Event(), asyncio.Event()
    calls = 0
    async def generate_one_then_hold(task_id, *, session_factory):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            try: await asyncio.Event().wait()
            finally: stopped.set()
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            await save_generated(db, task)
    r.implementation.side_effect = generate_one_then_hold
    response = await rewrite.batch_generate(rewrite.BatchGenerateRequest(**r.batch_request), db=r.db)
    stream = response.body_iterator
    await anext(stream)
    assert 'completed' in await anext(stream)
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError): await pending
    await stream.aclose()
    assert stopped.is_set()
    saved = await batch_status(r.db, r.pid, r.batch_request['request_id'])
    assert saved['status'] == 'interrupted' and saved['completed'] == 1
    assert [item['status'] for item in saved['results']] == ['completed', 'cancelled', 'cancelled']
    assert not any(task.status in ('pending', 'running') for task in persisted(r, GenerationTask))
    again = events(await post(r))
    assert again[-1]['status'] == 'interrupted' and calls == 2


@pytest.mark.asyncio
async def test_history_keeps_original_chapter_identity_after_chapter_deletion(batch_http):
    r = batch_http
    await post(r)
    original = await batch_status(r.db, r.pid, r.batch_request['request_id'])
    await r.db.rollback()
    r.session.execute(sa.delete(Chapter).where(Chapter.id == r.cid)); r.session.commit()
    assert await batch_status(r.db, r.pid, r.batch_request['request_id']) == original
    with pytest.raises(BatchSubmissionError) as failure:
        await batch_status(r.db, uuid4(), r.batch_request['request_id'])
    assert failure.value.http_status == 404


@pytest.mark.asyncio
async def test_status_reads_do_not_load_outline_or_prose(batch_http):
    r = batch_http
    await post(r)
    statements = []
    def capture(conn, cursor, statement, parameters, context, many): statements.append(statement)
    sa.event.listen(r.engine, 'before_cursor_execute', capture)
    try:
        await batch_status(r.db, r.pid, r.batch_request['request_id'])
    finally: sa.event.remove(r.engine, 'before_cursor_execute', capture)
    assert statements and all('outline_json' not in text and 'content_text' not in text and 'result_text' not in text for text in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize('checkpoint', [3, 4])
@pytest.mark.parametrize('after', [False, True])
async def test_uncertain_progress_never_restarts_an_accepted_batch(batch_http, monkeypatch, checkpoint, after):
    r = batch_http
    original, count = batch_submissions.commit_batch, 0
    async def interrupted_commit(db, request_id):
        nonlocal count
        count += 1
        if count == checkpoint:
            if after: await original(db, request_id)
            else: await db.rollback()
            raise BatchSubmissionError('batch_commit_unconfirmed', '进度提交尚未确认。', 503, request_id)
        await original(db, request_id)
    monkeypatch.setattr(batch_submissions, 'commit_batch', interrupted_commit)
    first = events(await post(r))
    assert first[-1]['status'] == 'unconfirmed'
    assert r.implementation.await_count == (0 if checkpoint == 3 else 1)
    assert len(persisted(r, GenerationTask)) == 2
    again = events(await post(r))
    assert again[-1]['status'] == 'running'
    assert r.implementation.await_count == (0 if checkpoint == 3 else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['volume', 'chapter', 'negative'])
async def test_ambiguous_or_invalid_order_does_not_pick_an_arbitrary_batch_sequence(batch_http, invalid):
    r = batch_http
    chapters = selection(r)
    if invalid == 'volume':
        r.session.add(Volume(project_id=r.pid, volume_idx=1, title='同号另一卷'))
    else: chapters[-1].chapter_idx = 1 if invalid == 'chapter' else -1
    r.session.commit()
    response = await post(r)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'batch_chapter_conflict'
    assert not persisted(r, GenerationTask)
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
async def test_observer_and_post_hook_failures_do_not_change_committed_results(batch_http):
    r = batch_http
    generator = batch()
    generator.hook_manager.run_post_hooks.side_effect = RuntimeError('test post-hook failure')
    def observer(job): raise RuntimeError('test UI disconnect')
    async with r.sessions() as db:
        result = await generator.generate_batch(str(r.pid), r.batch_request['chapter_configs'],
            request_id=r.batch_request['request_id'], db=db, on_progress=observer)
    assert result.status.value == 'completed' and result.completed_chapters == 2
    assert (await batch_status(r.db, r.pid, result.job_id))['completed'] == 2


@pytest.mark.asyncio
async def test_cancel_during_post_hook_keeps_original_completed_observation_after_author_edit(batch_http):
    r = batch_http
    generator = batch()
    entered = asyncio.Event()
    async def hook(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    generator.hook_manager.run_post_hooks.side_effect = hook
    async def run():
        async with r.sessions() as db:
            await generator.generate_batch(str(r.pid), r.batch_request['chapter_configs'],
                request_id=r.batch_request['request_id'], db=db)
    pending = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 3)
    first = selection(r)[0]
    first.status, first.content_text = 'draft', '作者在已完成章节上继续修改。'
    r.session.commit()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError): await pending
    saved = await batch_status(r.db, r.pid, r.batch_request['request_id'])
    assert saved['status'] == 'interrupted' and saved['completed'] == 1
    assert [row['status'] for row in saved['results']] == ['completed', 'cancelled']
    assert selection(r)[0].content_text == '作者在已完成章节上继续修改。'


@pytest.mark.asyncio
async def test_wrong_batch_execution_token_cannot_claim_or_fail_a_child(batch_http):
    r = batch_http
    req = r.batch_request
    await submit_batch(r.db, r.pid, req['request_id'], req['chapter_configs'])
    async with r.batch_lease(r.db, req['request_id']):
        token = await claim_batch(r.db, r.pid, req['request_id'])
        item = persisted(r, GenerationBatchItem)[0]
        await record_item(r.db, req['request_id'], token, item.task_id, 'generating')
        with batch_execution(req['request_id'], uuid4(), item.task_id):
            await _run_guarded_generation(str(item.task_id))
    r.implementation.assert_not_awaited()
    assert all(task.status == 'pending' for task in persisted(r, GenerationTask))


@pytest.mark.asyncio
async def test_missing_persisted_member_is_reported_instead_of_rebuilt(batch_http):
    r = batch_http
    await post(r)
    item = persisted(r, GenerationBatchItem)[0]
    r.session.delete(item); r.session.commit()
    async with r.client() as client:
        response = await client.get(f'/api/generate/batch/{r.batch_request["request_id"]}', params={'project_id': str(r.pid)})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'batch_manifest_incomplete'
    assert events(await post(r))[-1]['status'] == 'unconfirmed'
    assert r.implementation.await_count == 2


@pytest.mark.asyncio
async def test_five_million_character_selection_is_stored_once_and_polled_without_prose(batch_http, record_testsuite_property):
    from time import monotonic
    r = batch_http
    originals = selection(r)
    volume_id = originals[0].volume_id
    text = ('弟弟接过信封，又把钱数了一遍。' * 400)[:5063]
    for chapter in originals:
        chapter.content_text, chapter.word_count = text, len(text)
    for index in range(3, 1006):
        r.session.add(Chapter(volume_id=volume_id, chapter_idx=index, global_idx=index, title=f'交付{index}',
            outline_json={'summary': '核对两千元，再带弟弟去看腿。'}, content_text=text, word_count=len(text), status='draft'))
    r.session.commit()
    chapters = selection(r)
    assert len(chapters) == 1005 and sum(len(ch.content_text) for ch in chapters) > 5_000_000
    started = monotonic()
    await submit_batch(r.db, r.pid, r.batch_request['request_id'], configs(reversed(chapters)))
    record_testsuite_property('batch_submission_seconds', round(monotonic() - started, 3))
    assert len(persisted(r, GenerationTask)) == 1005
    statements = []
    def capture(conn, cursor, statement, parameters, context, many): statements.append(statement)
    sa.event.listen(r.engine, 'before_cursor_execute', capture)
    try: status = await batch_status(r.db, r.pid, r.batch_request['request_id'])
    finally: sa.event.remove(r.engine, 'before_cursor_execute', capture)
    assert status['total'] == 1005 and len(status['results']) == 1005
    assert status['results'][-1]['chapter'] == 1005
    assert len(statements) <= 3 and all('content_text' not in sql and 'outline_json' not in sql for sql in statements)
    assert len(json.dumps(status, ensure_ascii=False)) < 400_000
    r.implementation.assert_not_awaited()
