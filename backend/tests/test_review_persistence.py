"""Real SQLite checkpoint/variant/report writes, with provider IO replaced.

The adapter executes SQL synchronously to avoid private runtime services. It
tests the already-owned state machine core. Execution ownership and its
HTTP entrypoints are tested separately in test_generation_execution.py.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.models.generation_run import GenerationRun, CriticReport
from app.models.project import Project, Volume, Chapter, ChapterVariant
from app.services import generation_runner as runner
from app.services.context_errors import RequiredContextUnavailable
from app.services.review_contract import make_critic_report, incomplete_critic_report, critic_review_status
from tests.support.generation_run_fixture import verified_history


FIRST = '沈青把两千元放进信封，推到弟弟面前。'
SECOND = '沈青数出两千元，等弟弟伸手来接。'


def clean(text):
    return make_critic_report(text, issues=[], stages=[{'name': 'critic', 'status': 'complete'}])


class SQLiteSession:
    def __init__(self, session): self.session = session
    def add(self, row):
        for column in row.__table__.columns:
            value = getattr(row, column.name)
            if isinstance(column.type, sa.Uuid) and isinstance(value, str):
                setattr(row, column.name, UUID(value))
        self.session.add(row)
    async def get(self, model, key):
        return self.session.get(model, UUID(key) if isinstance(key, str) else key)
    async def execute(self, statement):
        compiled = statement.compile()
        parameters = {}
        for key, value in compiled.params.items():
            if isinstance(compiled.binds[key].type, sa.Uuid) and isinstance(value, str):
                parameters[key] = UUID(value)
        return self.session.execute(statement, parameters)
    async def commit(self): self.session.commit()
    async def flush(self): self.session.flush()
    async def rollback(self): self.session.rollback()
    async def refresh(self, row): self.session.refresh(row)


@pytest.fixture
def saved(monkeypatch):
    engine = sa.create_engine('sqlite://')
    for model in (Project, Volume, Chapter, GenerationRun, CriticReport, ChapterVariant):
        model.__table__.create(engine)
    with engine.begin() as conn:
        # This production constraint lives in a1000700, not ORM metadata.
        conn.exec_driver_sql('CREATE UNIQUE INDEX variants_run_index ON chapter_variants(chapter_id, run_id, variant_idx)')
    with Session(engine, expire_on_commit=False) as session:
        project = Project(id=uuid4(), title='审读候选')
        volume = Volume(id=uuid4(), project_id=project.id, volume_idx=1, title='归来')
        chapter = Chapter(id=uuid4(), volume_id=volume.id, chapter_idx=1, global_idx=1, title='交付', content_text=FIRST)
        session.add_all([project, volume, chapter])
        session.commit()
        db = SQLiteSession(session)
        planning = AsyncMock(return_value={'pack': {'system_prompt': '当前确认的前文与作者设定'}})
        drafting = AsyncMock(side_effect=[{'text': FIRST}, {'text': SECOND}])
        async def review(**kwargs): return clean(kwargs['draft'])
        monkeypatch.setattr(runner, '_phase_planning', planning)
        monkeypatch.setattr(runner, '_phase_drafting', drafting)
        monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=review))
        monkeypatch.setattr(runner.run_bus, 'publish', AsyncMock())
        monkeypatch.setenv('BVSR_ENABLED', 'false')
        monkeypatch.setenv('BVSR_N', '2')
        yield NS(db=db, session=session, pid=project.id, cid=chapter.id, planning=planning, drafting=drafting)
    engine.dispose()


async def start(r, *, budget=0):
    return await runner._create_run_locked(project_id=str(r.pid), chapter_id=str(r.cid), db=r.db, max_rewrite_count=budget)


def reports(r):
    r.session.expire_all()
    return list(r.session.scalars(sa.select(CriticReport).order_by(CriticReport.created_at)))


@pytest.mark.asyncio
async def test_review_receipt_survives_reload_and_api_projection(saved):
    from app.api.generation_runs import CriticReportResponse, RunResponse
    run = await start(saved)
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db)
    assert result['status'] == 'done' and result['editorial_status'] == 'needs_author_review'
    row = reports(saved)[0]
    payload = CriticReportResponse.model_validate(row).model_dump(mode='json')
    assert payload['review_status'] == 'complete'
    assert critic_review_status({**payload['review_json'], 'issues': payload['issues_json']}, FIRST) == 'complete'
    assert result['final_text'] == FIRST
    api_result = RunResponse.model_validate(saved.session.get(GenerationRun, run.id)).model_dump()
    assert api_result['review_status'] == 'complete' and api_result['editorial_status'] == 'needs_author_review'
    assert saved.session.get(Chapter, saved.cid).content_text == FIRST  # Run output is not author acceptance.


def test_legacy_report_api_does_not_call_empty_issues_complete():
    from app.api.generation_runs import CriticReportResponse
    old = CriticReportResponse(id=uuid4(), run_id=uuid4(), round=1, issues_json=[])
    assert old.model_dump()['review_status'] == 'unknown'


@pytest.mark.asyncio
async def test_unreviewed_first_draft_is_retained_but_cannot_win(saved, monkeypatch):
    from app.api.variants import _to_out
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    async def critic(**kwargs):
        return incomplete_critic_report(FIRST) if kwargs['draft'] == FIRST else clean(SECOND)
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=critic))
    run = await start(saved)
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db)
    assert result['final_text'] == SECOND and result['status'] == 'done'
    saved.session.expire_all()
    variants = list(saved.session.scalars(sa.select(ChapterVariant).order_by(ChapterVariant.variant_idx)))
    assert [v.content_text for v in variants] == [FIRST, SECOND]
    assert variants[0].score is None and not variants[0].is_winner
    assert variants[1].is_winner
    assert _to_out(variants[0]).review_status == 'incomplete'
    assert _to_out(variants[1]).review_status == 'complete'
    cp = saved.session.get(GenerationRun, run.id).checkpoint_data
    assert cp['drafting']['variants'][0]['critic_report']['review_status'] == 'incomplete'
    assert cp['drafting']['variants'][1]['critic_report']['review_status'] == 'complete'


@pytest.mark.asyncio
async def test_all_failed_reviews_persist_candidates_with_no_winner(saved, monkeypatch):
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    async def critic(**kwargs): return incomplete_critic_report(kwargs['draft'])
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=critic))
    run = await start(saved)
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db)
    assert result['status'] == 'needs_review' and result['review_status'] == 'incomplete'
    rows = list(saved.session.scalars(sa.select(ChapterVariant)))
    assert len(rows) == 2 and all(v.score is None and not v.is_winner for v in rows)
    assert result['final_text'] == FIRST
    assert len(reports(saved)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('still_failing', [False, True])
@pytest.mark.parametrize('enabled_after_restart', ['true', 'false'])
async def test_resume_rechecks_all_saved_candidates_without_regenerating_or_duplicating_them(
    saved, monkeypatch, still_failing, enabled_after_restart,
):
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    async def unavailable(**kwargs): return incomplete_critic_report(kwargs['draft'])
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=unavailable))
    run = await start(saved)
    run_id = run.id
    assert (await runner._execute_run_locked(run_id=str(run_id), db=saved.db))['stop_reason'] == 'variant_reviews_incomplete'
    original_ids = {v.variant_idx: v.id for v in saved.session.scalars(sa.select(ChapterVariant))}
    monkeypatch.setenv('BVSR_ENABLED', enabled_after_restart)

    async def fresh(**kwargs):
        text = kwargs['draft']
        return incomplete_critic_report(text) if still_failing or text == FIRST else clean(text)

    reviewer = AsyncMock(side_effect=fresh)
    monkeypatch.setattr(runner, 'run_critic', reviewer)
    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db, resume=True)

    assert result['status'] == ('needs_review' if still_failing else 'done')
    assert result['final_text'] == (FIRST if still_failing else SECOND)
    assert [call.kwargs['draft'] for call in reviewer.await_args_list] == ([FIRST, SECOND] if still_failing else [FIRST, SECOND, SECOND])
    saved.session.expire_all()
    rows = list(saved.session.scalars(sa.select(ChapterVariant).order_by(ChapterVariant.variant_idx)))
    assert {v.variant_idx: v.id for v in rows} == original_ids
    assert [v.content_text for v in rows] == [FIRST, SECOND]
    assert [v.is_winner for v in rows] == [False, not still_failing]
    assert critic_review_status(rows[1].critic_report_json, SECOND) == ('incomplete' if still_failing else 'complete')
    assert saved.drafting.await_count == 2
    assert len(reports(saved)) == (4 if still_failing else 5)


@pytest.mark.asyncio
async def test_candidate_review_retry_preserves_the_authors_explicit_choice(saved, monkeypatch):
    from app.api.variants import SelectVariantRequest, select_variant_endpoint

    monkeypatch.setenv('BVSR_ENABLED', 'true')
    async def unavailable(**kwargs): return incomplete_critic_report(kwargs['draft'])
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=unavailable))
    run = await start(saved)
    run_id = run.id
    await runner._execute_run_locked(run_id=str(run_id), db=saved.db)
    choice = saved.session.scalars(sa.select(ChapterVariant).where(ChapterVariant.variant_idx == 0)).one()
    await select_variant_endpoint(choice.id, SelectVariantRequest(apply_to_chapter=False), saved.db)

    async def fresh(**kwargs):
        return incomplete_critic_report(FIRST) if kwargs['draft'] == FIRST else clean(SECOND)

    reviewer = AsyncMock(side_effect=fresh)
    monkeypatch.setattr(runner, 'run_critic', reviewer)
    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db, resume=True)

    assert result['status'] == 'needs_review' and result['final_text'] == FIRST
    assert result['stop_reason'] == 'review_incomplete'
    saved.session.expire_all()
    rows = list(saved.session.scalars(sa.select(ChapterVariant).order_by(ChapterVariant.variant_idx)))
    assert [v.selected_by_user for v in rows] == [True, False]
    assert [v.is_winner for v in rows] == [True, False]
    assert critic_review_status(rows[1].critic_report_json, SECOND) == 'complete'
    assert saved.session.get(GenerationRun, run_id).checkpoint_data['drafting']['bvsr']['winner_idx'] == 0
    assert [call.kwargs['draft'] for call in reviewer.await_args_list] == [FIRST, SECOND, FIRST]


@pytest.mark.asyncio
async def test_candidate_review_retry_does_not_overwrite_a_different_saved_text(saved, monkeypatch):
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    async def unavailable(**kwargs): return incomplete_critic_report(kwargs['draft'])
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=unavailable))
    run = await start(saved)
    run_id = run.id
    await runner._execute_run_locked(run_id=str(run_id), db=saved.db)
    first = saved.session.scalars(sa.select(ChapterVariant).where(ChapterVariant.variant_idx == 0)).one()
    first.content_text = '另一份已经保存的稿件。'
    saved.session.commit()

    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db, resume=True)

    assert result['status'] == 'failed'
    saved.session.expire_all()
    assert list(saved.session.scalars(sa.select(ChapterVariant.content_text).order_by(ChapterVariant.variant_idx))) == ['另一份已经保存的稿件。', SECOND]
    assert [v['text'] for v in saved.session.get(GenerationRun, run_id).checkpoint_data['drafting']['variants']] == [FIRST, SECOND]
    assert saved.drafting.await_count == 2


@pytest.mark.asyncio
async def test_author_selected_fragment_stays_incomplete_after_candidate_review_retry(saved, monkeypatch):
    from app.api.variants import SelectVariantRequest, select_variant_endpoint

    fragment = '她数到最后一张，'
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    saved.drafting.side_effect = [{'text': FIRST, 'delivery_status': 'complete'},
                                 {'text': fragment, 'delivery_status': 'incomplete'}]
    async def unavailable(**kwargs): return incomplete_critic_report(kwargs['draft'])
    reviewer = AsyncMock(side_effect=unavailable)
    monkeypatch.setattr(runner, 'run_critic', reviewer)
    run = await start(saved)
    run_id = run.id
    await runner._execute_run_locked(run_id=str(run_id), db=saved.db)
    choice = saved.session.scalars(sa.select(ChapterVariant).where(ChapterVariant.variant_idx == 1)).one()
    await select_variant_endpoint(choice.id, SelectVariantRequest(apply_to_chapter=False), saved.db)
    reviewer.reset_mock()

    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db, resume=True)

    assert result['status'] == 'needs_review' and result['stop_reason'] == 'draft_incomplete'
    assert result['final_text'] == fragment
    assert [call.kwargs['draft'] for call in reviewer.await_args_list] == [FIRST]
    saved.session.expire_all()
    current = saved.session.get(GenerationRun, run_id).checkpoint_data['drafting']
    assert current['delivery_status'] == 'incomplete' and current['text'] == fragment
    assert saved.drafting.await_count == 2


@pytest.mark.asyncio
async def test_author_can_explicitly_select_an_unreviewed_draft(saved, monkeypatch):
    from app.api.variants import select_variant_endpoint, SelectVariantRequest
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    async def critic(**kwargs): return incomplete_critic_report(kwargs['draft'])
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=critic))
    run = await start(saved)
    await runner._execute_run_locked(run_id=str(run.id), db=saved.db)
    variant = saved.session.scalars(sa.select(ChapterVariant).order_by(ChapterVariant.variant_idx.desc())).first()
    result = await select_variant_endpoint(variant.id, SelectVariantRequest(apply_to_chapter=False), saved.db)
    assert result.selected_by_user and result.is_winner and result.review_status == 'incomplete'
    assert result.score is None
    assert saved.session.get(Chapter, saved.cid).content_text == FIRST


@pytest.mark.asyncio
async def test_mid_variant_source_failure_keeps_every_delivered_text(saved, monkeypatch):
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    calls = 0
    async def critic(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: raise RequiredContextUnavailable('character_cognition_decision_changed')
        return clean(kwargs['draft'])
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=critic))
    run = await start(saved)
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db)
    assert result['status'] == 'failed'
    saved.session.expire_all()
    restored = saved.session.get(GenerationRun, run.id)
    assert restored.status == 'failed'
    assert [v['text'] for v in restored.checkpoint_data['drafting']['variants']] == [FIRST, SECOND]
    assert restored.checkpoint_data['drafting']['text'] == FIRST


@pytest.mark.asyncio
async def test_persistence_failure_stops_selection_and_resume_rechecks_saved_candidates(saved, monkeypatch):
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    original_flush = saved.db.flush
    saved.db.flush = AsyncMock(side_effect=RuntimeError('private database detail'))
    run = await start(saved)
    assert (await runner._execute_run_locked(run_id=str(run.id), db=saved.db))['status'] == 'failed'
    saved.session.expire_all()
    restored = saved.session.get(GenerationRun, run.id)
    assert len(restored.checkpoint_data['drafting']['variants']) == 2
    assert not list(saved.session.scalars(sa.select(ChapterVariant)))
    assert 'private' not in restored.last_error
    before = runner.run_critic.await_count
    saved.db.flush = original_flush
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db, resume=True)
    assert result['status'] == 'done'
    assert saved.drafting.await_count == 2  # No duplicate generation after delivery.
    assert runner.run_critic.await_count == before + 3  # Both saved variants + selected manuscript.
    assert len(list(saved.session.scalars(sa.select(ChapterVariant)))) == 2


@pytest.mark.asyncio
async def test_real_failed_flush_rolls_back_before_reading_the_expired_run(saved, monkeypatch):
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    run = await start(saved)
    run_id = run.id
    existing = ChapterVariant(chapter_id=saved.cid, run_id=run_id, variant_idx=0, content_text='已有的候选记录。')
    saved.session.add(existing)
    saved.session.commit()
    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db)
    assert result['status'] == 'failed'
    saved.session.expire_all()
    restored = saved.session.get(GenerationRun, run_id)
    assert restored.status == 'failed' and restored.checkpoint_data['failure']['phase'] == 'drafting'
    assert [v['text'] for v in restored.checkpoint_data['drafting']['variants']] == [FIRST, SECOND]
    assert list(saved.session.scalars(sa.select(ChapterVariant.content_text))) == ['已有的候选记录。']


@pytest.mark.asyncio
async def test_review_retry_reuses_the_manuscript_without_resetting_budget(saved, monkeypatch):
    run = await start(saved, budget=1)
    run.rewrite_count = 1
    run.phase = 'critic'
    run.checkpoint_data = {'drafting': {'text': FIRST, 'delivery_status': 'complete'}}
    saved.session.add_all(verified_history(run))
    saved.session.commit()
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(return_value=incomplete_critic_report(FIRST)))
    assert (await runner._execute_run_locked(run_id=str(run.id), db=saved.db))['status'] == 'needs_review'
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(return_value=clean(FIRST)))
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db, resume=True)
    assert result['status'] == 'done' and result['rewrite_count'] == 1 and result['stop_reason'] is None
    saved.drafting.assert_not_awaited()
    assert len(reports(saved)) == 2


@pytest.mark.asyncio
async def test_truncated_revision_retains_both_texts_and_cannot_be_resumed_as_complete(saved, monkeypatch):
    issue = {'severity': 'hard', 'category': 'resource', 'desc': '交付后余额待核对。', 'location': '两千元'}
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(return_value=make_critic_report(FIRST, issues=[issue], stages=[{'name': 'critic', 'status': 'complete'}])))
    rewrite = AsyncMock(return_value={'text': '她数到最后一张，', 'delivery_status': 'incomplete'})
    monkeypatch.setattr(runner, '_phase_rewrite', rewrite)
    run = await start(saved, budget=1)
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db)
    assert result['status'] == 'needs_review' and result['final_text'] == '她数到最后一张，'
    saved.session.expire_all()
    restored = saved.session.get(GenerationRun, run.id)
    assert restored.checkpoint_data['draft_history'][0]['text'] == FIRST
    assert restored.rewrite_count == 1
    calls = runner.run_critic.await_count
    assert (await runner._execute_run_locked(run_id=str(run.id), db=saved.db, resume=True))['status'] == 'needs_review'
    assert runner.run_critic.await_count == calls


@pytest.mark.asyncio
async def test_legacy_finalize_checkpoint_cannot_invent_missing_call_history(saved, monkeypatch):
    run = await start(saved)
    run.phase = 'finalize'
    run.checkpoint_data = {'drafting': {'text': FIRST}, 'critic': {'report': {'issues': [], 'hard_count': 0}}}
    saved.session.commit()
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(return_value=incomplete_critic_report(FIRST)))
    result = await runner._execute_run_locked(run_id=str(run.id), db=saved.db, resume=True)
    assert result['status'] == 'needs_review'
    assert result['stop_reason'] == 'model_call_history_unverified'
    runner.run_critic.assert_not_awaited()
    assert 'finalize' not in saved.session.get(GenerationRun, run.id).checkpoint_data


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['rewrite', 'finalize'])
@pytest.mark.parametrize('failed', [False, True])
async def test_resume_rechecks_current_sources_even_with_a_complete_old_receipt(saved, monkeypatch, phase, failed):
    run = await start(saved, budget=2)
    run_id = run.id
    issues = [{'severity': 'hard', 'category': 'resource', 'desc': '交付后余额待核对。', 'location': '两千元'}] if phase == 'rewrite' else []
    old_report = make_critic_report(FIRST, issues=issues, stages=[{'name': 'critic', 'status': 'complete'}])
    run.phase = 'failed' if failed else phase
    run.status = 'failed' if failed else 'running'
    run.rewrite_count = 1
    run.checkpoint_data = {'drafting': {'text': FIRST, 'delivery_status': 'complete'},
                           'critic': {'report': old_report, 'round': 1}, 'failure': {'phase': phase}}
    saved.session.add_all(verified_history(run))
    saved.session.commit()
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(return_value=incomplete_critic_report(FIRST)))
    rewrite = AsyncMock(return_value={'text': SECOND, 'delivery_status': 'complete'})
    monkeypatch.setattr(runner, '_phase_rewrite', rewrite)

    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db, resume=True)

    assert result['status'] == 'needs_review' and result['stop_reason'] == 'review_incomplete'
    assert result['final_text'] == FIRST and result['rewrite_count'] == 1
    runner.run_critic.assert_awaited_once()
    saved.planning.assert_awaited_once()
    assert saved.planning.await_args.kwargs['candidate_text'] == FIRST
    rewrite.assert_not_awaited()
    saved.drafting.assert_not_awaited()
    assert len(reports(saved)) == 2  # Keep the old review and the fresh incomplete receipt.
    assert 'finalize' not in saved.session.get(GenerationRun, run_id).checkpoint_data


@pytest.mark.asyncio
async def test_notification_failure_cannot_resume_a_truncated_revision_as_complete(saved, monkeypatch):
    issue = {'severity': 'hard', 'category': 'resource', 'desc': '交付后余额待核对。', 'location': '两千元'}
    old_report = make_critic_report(FIRST, issues=[issue], stages=[{'name': 'critic', 'status': 'complete'}])
    fragment = '她数到最后一张，'
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(return_value=old_report))
    rewrite = AsyncMock(return_value={'text': fragment, 'delivery_status': 'incomplete'})
    monkeypatch.setattr(runner, '_phase_rewrite', rewrite)

    async def publish(*_args, **kwargs):
        if kwargs['event'] == 'phase.end' and kwargs['payload'].get('phase') == 'rewrite':
            raise RuntimeError('notification_unavailable')

    monkeypatch.setattr(runner.run_bus, 'publish', AsyncMock(side_effect=publish))
    run = await start(saved, budget=1)
    run_id = run.id
    assert (await runner._execute_run_locked(run_id=str(run_id), db=saved.db))['status'] == 'failed'
    saved.session.expire_all()
    restored = saved.session.get(GenerationRun, run_id)
    assert restored.checkpoint_data['drafting']['text'] == fragment
    assert restored.checkpoint_data['drafting']['delivery_status'] == 'incomplete'

    monkeypatch.setattr(runner.run_bus, 'publish', AsyncMock())
    fresh_review = AsyncMock(return_value=clean(fragment))
    monkeypatch.setattr(runner, 'run_critic', fresh_review)
    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db, resume=True)

    assert result['status'] == 'needs_review' and result['stop_reason'] == 'draft_incomplete'
    assert result['final_text'] == fragment and result['rewrite_count'] == 1
    fresh_review.assert_not_awaited()
    rewrite.assert_awaited_once()
    saved.session.expire_all()
    cp = saved.session.get(GenerationRun, run_id).checkpoint_data
    assert cp['draft_history'][0]['text'] == FIRST and 'finalize' not in cp


@pytest.mark.asyncio
@pytest.mark.parametrize('status,phase', [('needs_review', 'awaiting_review'), ('failed', 'failed'), ('done', 'done')])
async def test_run_result_returns_current_draft_until_completion(saved, status, phase):
    run = await start(saved)
    run_id = run.id
    run.status, run.phase = status, phase
    run.checkpoint_data = {'drafting': {'text': SECOND}, 'critic': {'report': clean(SECOND)},
                           'finalize': {'final_text': FIRST}, 'awaiting_review': {'reason': 'issues_need_review'}}
    saved.session.commit()
    saved.session.expire_all()
    result = await runner._execute_run_locked(run_id=str(run_id), db=saved.db)
    assert result['final_text'] == (FIRST if status == 'done' else SECOND)
    assert saved.session.get(GenerationRun, run_id).checkpoint_data['finalize']['final_text'] == FIRST


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/a1001950_critic_review_receipts.py'
    spec = importlib.util.spec_from_file_location('critic_review_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('has_new_receipt', [False, True])
def test_migration_keeps_old_reports_unknown_and_guards_new_receipts(monkeypatch, has_new_receipt):
    engine = sa.create_engine('sqlite://')
    m = migration()
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE critic_reports (id INTEGER PRIMARY KEY, issues_json JSON NOT NULL)')
        conn.exec_driver_sql("INSERT INTO critic_reports VALUES (1, '[]')")
        monkeypatch.setattr(m, 'op', Operations(MigrationContext.configure(conn)))
        m.upgrade()
        assert conn.execute(sa.text('SELECT issues_json, review_json FROM critic_reports')).one() == ('[]', '{}')
        assert critic_review_status({'issues': []}) == 'unknown'
        if has_new_receipt:
            conn.execute(sa.text('UPDATE critic_reports SET review_json=:value'), {'value': json.dumps(clean(FIRST))})
            with pytest.raises(RuntimeError, match='评审回执'): m.downgrade()
            assert conn.scalar(sa.text('SELECT review_json FROM critic_reports')) == json.dumps(clean(FIRST))
        else:
            m.downgrade()
            assert conn.scalar(sa.text('SELECT issues_json FROM critic_reports')) == '[]'
    engine.dispose()
