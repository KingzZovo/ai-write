"""Real async-generation control flow with offline model/derived-service replies.

Claims, source checks, all task/manuscript saves, rollbacks and receipts use
real SQLAlchemy transactions. These tests verify delivery, not prose quality.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.generation_task import GenerationTask
from app.models.project import Character, Chapter, ChapterEvaluation, ChapterVersion, Outline
from app.services.fact_contract import (
    build_fact_contract as actual_build_fact_contract,
    validate_text_against_fact_contract as actual_validate_fact_contract,
)
from app.services import generation_task_execution as ownership
from app.services.chapter_evaluator import EvaluationResult
from app.services.generation_execution import GenerationExecutionUnavailable
from app.services.production.control import content_hash
from app.tasks import generation_tasks
from tests.test_generation_task_execution import (
    DELIVERED, ORIGINAL, body, run, snapshot, submit, task_book,
)


@pytest.fixture
def core_task(task_book, monkeypatch):
    r = task_book
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', r.core)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=NS(generate=AsyncMock())))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    monkeypatch.setattr(generation_tasks, '_build_chinese_prose_preflight_prompt', AsyncMock(return_value=('', {})))
    monkeypatch.setattr('app.services.evaluation_neighbors.load_evaluation_neighbors',
        AsyncMock(return_value=NS(previous_context='', following_context='')))
    monkeypatch.setattr('app.services.style_stat.load_style_stats_text', AsyncMock(return_value=''))
    monkeypatch.setattr('app.services.style_stat.dispatch_style_recompute', lambda *a, **k: None)
    r.model = AsyncMock(return_value=DELIVERED)
    monkeypatch.setattr('app.services.chapter_generator.ChapterGenerator.generate', r.model)
    r.evaluate = AsyncMock(return_value=EvaluationResult(overall=9))
    monkeypatch.setattr('app.services.chapter_evaluator.ChapterEvaluator.evaluate', r.evaluate)
    r.reads = []
    async def context(*args, sessions, **kwargs):
        r.reads.append(sessions)
        # The read must use its own connection and leave no transaction on
        # the owned write connection before the evaluator starts.
        assert sessions is r.sessions
        async with sessions() as db:
            assert (await db.get(Chapter, r.cid)).content_text == ORIGINAL
        return ''
    monkeypatch.setattr('app.services.cognition_retrieval.load_candidate_context', context)
    r.quality = AsyncMock(return_value=NS(status='passed', final_text=DELIVERED,
        rewrite_rounds=0, to_safe_metadata=lambda: {'status': 'passed'}))
    monkeypatch.setattr(generation_tasks, 'apply_chapter_quality_gate', r.quality)
    r.fact = NS(ok=True, to_dict=lambda: {'ok': True})
    monkeypatch.setattr('app.services.fact_contract.build_fact_contract', AsyncMock(return_value={}))
    monkeypatch.setattr('app.services.fact_contract.validate_text_against_fact_contract', lambda *a: r.fact)
    r.summary = AsyncMock()
    monkeypatch.setattr('app.services.chapter_summarizer.summarize_and_save_chapter', r.summary)
    monkeypatch.setattr('app.services.character_cognition.extract_and_update', AsyncMock())
    r.dispatch = AsyncMock()
    monkeypatch.setattr(generation_tasks, '_dispatch_chapter_entities', r.dispatch)
    monkeypatch.setattr(generation_tasks, '_backfill_volume_summary_safe', AsyncMock())
    return r


def chapter_task(r, **params):
    return submit(r, force_direct_chapter=True, auto_revise=False, **params)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['completed', 'quality_review', 'quality_error', 'fact_review', 'fact_error'])
async def test_core_final_chapter_paths_save_on_the_owned_connection_with_receipts(core_task, monkeypatch, outcome):
    r = core_task
    identity = chapter_task(r)
    before = snapshot(r, identity)['params_json']
    if outcome == 'quality_review':
        r.quality.return_value.status = 'blocked'
        r.quality.return_value.warning_reason = 'test_missing_event'
    elif outcome == 'quality_error':
        r.quality.side_effect = RuntimeError('offline gate failed')
    elif outcome == 'fact_review':
        r.fact.ok = False
    elif outcome == 'fact_error':
        monkeypatch.setattr('app.services.fact_contract.build_fact_contract', AsyncMock(side_effect=RuntimeError('offline fact read failed')))
    await run(identity)
    state = snapshot(r, identity)
    expected = 'completed' if outcome == 'completed' else 'needs_review'
    assert state['status'] == expected and body(r) == DELIVERED
    assert state['execution_params_json'] == before and len(r.reads) == 1
    assert state['saved_manuscript_json']['body_hash'] == content_hash(DELIVERED)
    assert state['saved_manuscript_json']['status'] == expected
    assert len({id(db.bind) for db in r.runtime.sessions}) == 1
    with Session(r.engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(ChapterVersion)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(ChapterEvaluation)) == 1
    r.model.assert_awaited_once()
    r.dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    '导师：沈听澜。另一位导师：陈川。陈川扶住门框，先让学生进去。',
    '沈知夏是暗面学院外勤导师成员，负责回收失控觉醒者。',
])
async def test_core_records_real_identity_observations_without_rejecting_or_rewriting_shared_roles(core_task, monkeypatch, text):
    r = core_task
    Character.__table__.create(r.engine)
    with Session(r.engine) as db:
        outline = Outline(project_id=r.pid, level='book', is_confirmed=1,
                          content_json={'entity_registry': {'mentor': '沈听澜'}})
        db.add(outline)
        db.flush()
        oid = str(outline.id)
        db.commit()
    monkeypatch.setattr('app.services.fact_contract.build_fact_contract', actual_build_fact_contract)
    monkeypatch.setattr('app.services.fact_contract.validate_text_against_fact_contract', actual_validate_fact_contract)
    r.model.return_value = text
    r.quality.return_value.final_text = text
    identity = chapter_task(r)
    await run(identity)
    state = snapshot(r, identity)
    report = state['params_json']['fact_contract_gate']
    # Other review replies are fixtures: completed here tests this lexical
    # observer's lack of a veto, never the literary quality of the manuscript.
    assert state['status'] == 'completed' and body(r) == text
    assert report['ok'] is True and report['issues'][0]['severity'] == 'warning'
    assert report['facts']['text_hash'] == content_hash(text)
    assert report['facts']['contract']['outline_source']['id'] == oid
    assert report['facts']['editorial_status'] == 'needs_author_review'
    with Session(r.engine) as db:
        saved = db.scalar(sa.select(ChapterVersion))
        assert saved.content_text == text and saved.metadata_json['fact_contract_gate'] == report
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_identity_input_reads_saved_rows_without_flushing_or_discarding_other_pending_edits(core_task):
    r = core_task
    Character.__table__.create(r.engine)
    original_outline = {'entity_registry': {'protagonist': '林照'}}
    with Session(r.engine) as db:
        outline = Outline(project_id=r.pid, level='book', is_confirmed=1, content_json=original_outline)
        character = Character(project_id=r.pid, name='沈听澜', profile_json={'role': '导师'})
        db.add_all([outline, character])
        db.flush()
        oid, cid = outline.id, character.id
        db.commit()
    async with r.sessions() as db:
        outline, character = await db.get(Outline, oid), await db.get(Character, cid)
        outline.content_json = {'raw_text': '主角：林禾。'}
        character.name, character.profile_json = '陈川', {'role': '父亲'}
        contract = await actual_build_fact_contract(db, str(r.pid))
        assert contract.role_names == {'protagonist': '林照'}
        assert contract.role_name_sources == {'protagonist': 'saved_outline'}
        assert contract.registered_character_names == ['沈听澜']
        # A reader must not refresh an ORM identity and silently discard edits.
        assert outline.content_json == {'raw_text': '主角：林禾。'}
        assert character.name == '陈川' and character.profile_json == {'role': '父亲'}
        with Session(r.engine) as independent:
            assert independent.get(Outline, oid).content_json == original_outline
            assert independent.get(Character, cid).name == '沈听澜'
        await db.rollback()


@pytest.mark.asyncio
async def test_core_unexpected_evaluator_error_salvages_with_a_new_session_on_the_same_connection(core_task):
    r = core_task
    identity = chapter_task(r)
    r.evaluate.side_effect = RuntimeError('offline evaluator crash')
    await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'needs_review' and body(r) == DELIVERED
    assert state['saved_manuscript_json']['body_hash'] == content_hash(DELIVERED)
    assert len(r.runtime.sessions) == 3  # claim, core and salvage identity maps
    assert len({id(db.bind) for db in r.runtime.sessions}) == 1


@pytest.mark.asyncio
async def test_core_partial_scene_output_stays_a_candidate_without_claiming_a_manuscript_save(core_task, monkeypatch):
    r = core_task
    identity = submit(r, auto_revise=False)
    async def partial(*a, **k):
        yield '他刚把信封拿出，'
        raise RuntimeError('offline interrupted stream')
    monkeypatch.setattr('app.config.settings.FORCE_DIRECT_CHAPTER', False)
    monkeypatch.setattr('app.services.scene_orchestrator.SceneOrchestrator.orchestrate_chapter_stream', partial)
    await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'needs_review' and state['result_text'] == '他刚把信封拿出，'
    assert body(r) == ORIGINAL and state['saved_manuscript_json'] is None
    r.model.assert_not_awaited()
    r.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('point', ['model', 'evaluation'])
@pytest.mark.parametrize('loss', ['cancel', 'invalidate'])
async def test_core_late_outputs_and_every_salvage_branch_respect_lost_ownership(core_task, point, loss):
    r = core_task
    identity = chapter_task(r)
    entered, release = asyncio.Event(), asyncio.Event()
    target = r.model if point == 'model' else r.evaluate
    reply = target.return_value
    async def held(*a, **k):
        entered.set()
        await release.wait()
        return reply
    target.side_effect = held
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        owner = r.runtime.sessions[0]
        if loss == 'invalidate':
            owner.bind.invalidate()
        with Session(r.engine) as author:
            chapter = author.get(Chapter, r.cid)
            chapter.content_text = '作者在任务中断后明确保存的新稿。'
            task = author.get(GenerationTask, identity)
            task.status = 'cancelled' if loss == 'cancel' else 'needs_review'
            task.result_text = '作者保留的候选。'
            if loss == 'cancel': task.params_json = {**task.params_json, 'cancelled_at': '2026-10-09'}
            author.commit()
        saved = snapshot(r, identity)
        release.set()
        with pytest.raises((GenerationExecutionUnavailable, ownership.TaskExecutionLost)):
            await pending
        assert snapshot(r, identity) == saved
        assert body(r) == '作者在任务中断后明确保存的新稿。'
        assert saved['saved_manuscript_json'] is None
        r.model.assert_awaited_once()
        r.dispatch.assert_not_awaited()
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', ['before_commit', 'after_commit'])
async def test_core_uncertain_manuscript_commit_cannot_be_overwritten_by_error_salvage(core_task, monkeypatch, ack):
    r = core_task
    identity = chapter_task(r)
    original = r.runtime.OwnedSession.commit
    from app.services import generation_task_receipts
    record = generation_task_receipts.record_task_save
    manuscript_transactions = set()
    def remember(connection, chapter, task_id):
        record(connection, chapter, task_id)
        manuscript_transactions.add(connection.get_transaction())
    monkeypatch.setattr(generation_task_receipts, 'record_task_save', remember)
    async def uncertain(db):
        manuscript = db.bind.get_transaction() in manuscript_transactions
        if manuscript and ack == 'before_commit':
            db.guard.revoke()
            raise GenerationExecutionUnavailable()
        await original(db)
        if manuscript:
            db.guard.revoke()
            raise GenerationExecutionUnavailable()
    monkeypatch.setattr(r.runtime.OwnedSession, 'commit', uncertain)
    with pytest.raises(GenerationExecutionUnavailable): await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'evaluating'
    assert body(r) == (DELIVERED if ack == 'after_commit' else ORIGINAL)
    if ack == 'after_commit':
        assert state['saved_manuscript_json']['body_hash'] == content_hash(DELIVERED)
    else:
        assert state['saved_manuscript_json'] is None
    await run(identity)
    assert snapshot(r, identity) == state
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_core_model_failure_with_no_text_keeps_original_manuscript(core_task):
    r = core_task
    identity = chapter_task(r)
    r.model.side_effect = RuntimeError('offline upstream unavailable')
    await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'needs_repair' and state['saved_manuscript_json'] is None
    assert body(r) == ORIGINAL and state['attempt_count'] == 1
    r.dispatch.assert_not_awaited()


@pytest.fixture
def outline_task(core_task, monkeypatch):
    r = core_task
    # Substitute only PG advisory calls; keep the structure checks and SQL.
    monkeypatch.setattr('app.services.chapter_order.try_command_lock', AsyncMock(return_value=True))
    monkeypatch.setattr('app.services.chapter_order.require_materialization_lock', AsyncMock())
    r.outline_text = '沈青回到县城，为弟弟筹钱治伤。旧债的收据牵出当年的隐情，他先核实欠款，再决定是否找债主对质。'
    async def outline(*a, **k):
        async def chunks():
            yield r.outline_text
        return chunks()
    monkeypatch.setattr('app.services.outline_generator.OutlineGenerator.generate_book_outline', outline)
    monkeypatch.setattr('app.services.outline_generator.OutlineGenerator.generate_volume_outline', outline)
    monkeypatch.setattr('app.services.outline_from_reference.build_outline_from_reference',
        AsyncMock(return_value={'status': 'ok', 'outline_text': r.outline_text}))
    with Session(r.engine) as db:
        book = Outline(project_id=r.pid, level='book', content_json={'raw_text': '作者保存的全书大纲：兄弟团聚，先查债务和收据，再追查债主。'})
        db.add(book)
        db.flush()
        r.book_outline = book.id
        db.commit()
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['outline_book', 'outline_from_reference', 'outline_volume', 'outline_chapter'])
async def test_core_outline_saves_and_candidates_use_the_owned_connection(outline_task, monkeypatch, task_type):
    r = outline_task
    params = {'reference_book_id': str(r.book_outline)} if task_type == 'outline_from_reference' else {}
    if task_type == 'outline_chapter':
        monkeypatch.setattr('app.services.outline_readiness.build_outline_readiness_report',
            AsyncMock(return_value=NS(missing_layers=[])))
        async def expansion(*, db, **kwargs):
            # Match the expander's transaction release and final flush; its
            # prompt/source semantics have separate dedicated tests.
            await db.rollback()
            chapter = await db.get(Chapter, r.cid)
            chapter.outline_json = {'summary': r.outline_text}
            await db.flush()
            return chapter.outline_json
        monkeypatch.setattr('app.services.chapter_outline_expander.expand_chapter_outline', expansion)
    identity = submit(r, task_type, **params)
    before = snapshot(r, identity)['params_json']
    await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'completed' and r.outline_text in state['result_text']
    assert state['execution_params_json'] == before and state['saved_manuscript_json'] is None
    assert len({id(db.bind) for db in r.runtime.sessions}) == 1
    with Session(r.engine) as db:
        outlines = db.scalars(sa.select(Outline).where(Outline.project_id == r.pid)).all()
        assert len(outlines) == 1
        if task_type in {'outline_book', 'outline_from_reference'}:
            assert outlines[0].content_json['raw_text'] == r.outline_text
        elif task_type == 'outline_chapter':
            assert db.get(Chapter, r.cid).outline_json['summary'] == r.outline_text
        else:
            assert state['params_json']['volume_candidate']['text_hash'] == content_hash(r.outline_text)
            assert outlines[0].id == r.book_outline
    assert body(r) == ORIGINAL


@pytest.mark.asyncio
async def test_core_outline_save_failure_keeps_original_outline_and_returned_candidate(outline_task, monkeypatch):
    r = outline_task
    identity = submit(r, 'outline_book')
    monkeypatch.setattr('app.services.chapter_order.lock_chapter_structure',
        AsyncMock(side_effect=RuntimeError('offline structure failure')))
    await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'failed' and state['result_text'] == r.outline_text
    assert state['saved_manuscript_json'] is None
    assert len(r.runtime.sessions) == 3 and len({id(db.bind) for db in r.runtime.sessions}) == 1
    with Session(r.engine) as db:
        assert db.get(Outline, r.book_outline).content_json['raw_text'].startswith('作者保存的全书大纲')
