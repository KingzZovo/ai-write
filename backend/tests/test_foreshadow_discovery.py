import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import delete, insert, select, update, text, inspect, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.project import (Chapter, Volume, Project, Foreshadow, ForeshadowReview,
    ForeshadowReviewJob as Job, ForeshadowDiscovery as Candidate, ForeshadowDiscoveryRun as Run, ForeshadowWriteReceipt as Receipt)
from app.services.entity_sources import digest, resolve_entity_source
from app.services.foreshadow_discovery import (DiscoveryBatch, DiscoveryIncomplete, source_snapshot,
    parse_candidates, detect_clues, cached_discovery, persist_discovery, MAX_INPUT_CHARS)
from app.services.foreshadow_discovery_actions import (accept_discovery, dismiss_discovery, list_discoveries,
    DiscoveryNotFound, DiscoverySourceChanged, DiscoveryAlreadyHandled, DiscoveryWriteConflict, DiscoveryProjectUnavailable)
from app.services import foreshadow_review_worker as worker
from tests.test_foreshadow_review_store import database
from tests.test_foreshadow_review_worker import runtime, seed


def candidate_json(evidence='寄信的存根', **changes):
    return dict(description='陈伯为什么保留这张存根？', type='mystery', evidence=evidence,
                reason='动作让存根值得回看，是否需要后续回应仍需作者结合前文判断。', **changes)


@pytest.fixture
def discovery(runtime, monkeypatch):
    r = runtime
    Receipt.__table__.create(r.engine)
    r.mutex = AsyncMock()
    monkeypatch.setattr('app.services.entity_materialization_lock.require_materialization_lock', r.mutex)
    r.pid, r.cid = UUID(r.review.project_id), UUID(r.review.chapter_id)
    snapshot = source_snapshot(r.source)
    r.batch = DiscoveryBatch(snapshot, digest(snapshot), parse_candidates(json.dumps([candidate_json()]), r.source.content))
    r.detector.return_value = r.batch
    return r


async def persist(r, batch=None):
    with r.session.begin():
        current = await resolve_entity_source(r.db, r.pid, 0, r.cid, lock=True, allow_review_drafts=True)
        return await persist_discovery(r.db, batch or r.batch, locked_source=current)


async def stored_id(r):
    await persist(r)
    cid = r.session.execute(select(Candidate.id)).scalar_one()
    r.session.commit()
    return cid


@pytest.mark.asyncio
async def test_full_prose_tail_uses_dedicated_registry_route_and_exact_quote(discovery, monkeypatch):
    r = discovery
    body = '他们沿河走回村里。' * 600 + '\n陈伯把寄信的存根放在桌上。'
    source = NS(**{**vars(r.source), 'content': body})
    model = AsyncMock(return_value=NS(text=json.dumps([candidate_json()]), finish_reason='stop'))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    batch = await detect_clues(source)
    call = model.call_args
    assert call.args == ('chapter_foreshadows', '待核查完整保存稿：\n' + body)
    assert call.kwargs['db'] is None and call.kwargs['project_id'] == r.source.project_id
    assert call.kwargs['chapter_id'] == r.source.chapter_id and call.kwargs['input_char_limit'] == MAX_INPUT_CHARS
    assert batch.candidates[0].evidence_start > 4000
    assert body[batch.candidates[0].evidence_start:batch.candidates[0].evidence_end] == '寄信的存根'


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls'])
async def test_finished_json_with_abnormal_provider_end_is_not_an_empty_success(discovery, monkeypatch, finish):
    model = AsyncMock(return_value=NS(text='[]', finish_reason=finish))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    with pytest.raises(DiscoveryIncomplete): await detect_clues(discovery.source)


@pytest.mark.parametrize('payload', [
    '', 'broken', '{}', 'prefix [] suffix', '[null]', '[1]', '[{"evidence":"寄信的存根"}]',
    json.dumps([{**candidate_json(), 'description': ''}]),
    json.dumps([{**candidate_json(), 'type': 'unknown'}]),
    json.dumps([{**candidate_json(), 'reason': False}]),
    json.dumps([{**candidate_json(), 'evidence': '正文没有这句话'}]),
    json.dumps([{**candidate_json(), 'confidence': 100}]),
    json.dumps([candidate_json(), candidate_json()]),
    '[{"description":"旧","description":"新","type":"plot","evidence":"寄信的存根","reason":"原因"}]',
])
def test_malformed_or_unverifiable_discovery_never_becomes_no_findings(payload):
    with pytest.raises((ValueError, TypeError)): parse_candidates(payload, '陈伯把寄信的存根放在桌上。')


def test_repeated_quote_must_be_disambiguated_and_one_bad_item_rejects_whole_batch():
    body = '他说等信。他又说等信。陈伯收起存根。'
    with pytest.raises(ValueError, match='not_unique'):
        parse_candidates(json.dumps([candidate_json('等信')]), body)
    with pytest.raises(ValueError):
        parse_candidates(json.dumps([candidate_json('他又说等信。'), candidate_json('不在文中')]), body)
    assert parse_candidates('[]', body) == ()


@pytest.mark.asyncio
async def test_oversize_prose_is_rejected_before_any_model_call(discovery, monkeypatch):
    model = AsyncMock()
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    source = NS(**{**vars(discovery.source), 'content': '文' * (MAX_INPUT_CHARS + 1)})
    with pytest.raises(DiscoveryIncomplete, match='budget'): await detect_clues(source)
    model.assert_not_called()


@pytest.mark.asyncio
async def test_input_budget_counts_registered_system_and_extra_instructions(monkeypatch):
    from app.services import prompt_registry as registry
    model = AsyncMock(return_value=NS(text='[]'))
    route = NS(system_prompt='编辑核查' * 300, prompt_id=uuid4())
    monkeypatch.setattr(registry, '_resolve_route_and_tier_cached', AsyncMock(return_value=(route, 'standard')))
    monkeypatch.setattr(registry, 'get_model_router_async', AsyncMock(return_value=NS(generate_with_tier_fallback=model)))
    with pytest.raises(ValueError, match='budget_exceeded'):
        await registry.run_text_prompt('chapter_foreshadows', '完整稿件', None, input_char_limit=1000)
    model.assert_not_called()
    route.system_prompt = '编辑核查'
    await registry.run_text_prompt('chapter_foreshadows', '完整稿件', None, extra_system='逐字核对', input_char_limit=14)
    assert model.call_args.args[1] == [{'role': 'system', 'content': '编辑核查\n\n逐字核对'}, {'role': 'user', 'content': '完整稿件'}]
    assert 'input_char_limit' not in model.call_args.kwargs


@pytest.mark.asyncio
async def test_receipt_and_candidates_are_idempotent_without_writing_story_facts(discovery):
    r = discovery
    assert await persist(r) == 1
    cid = r.session.execute(select(Candidate.id)).scalar_one()
    assert r.session.execute(select(Foreshadow.id)).scalars().all() == [UUID(r.review.suggestions[0].foreshadow_id)]
    r.session.commit()
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid, actor='作者')
    other = replace(r.batch, candidates=(replace(r.batch.candidates[0], description='同次重跑的另一种措辞'),))
    assert await persist(r, other) == 1
    row = r.session.execute(select(Candidate.description, Candidate.status, Candidate.handled_by)).one()
    assert row == (r.batch.candidates[0].description, 'dismissed', '作者')
    assert len(r.session.execute(select(Run.id)).all()) == 1
    r.session.commit()
    cached = await cached_discovery(r.db, r.source)
    assert cached.cached_count == 1 and cached.candidates == ()


@pytest.mark.asyncio
async def test_empty_success_has_a_receipt_and_is_reused_after_title_or_status_edit(discovery):
    r = discovery
    empty = replace(r.batch, candidates=())
    assert await persist(r, empty) == 0
    r.session.execute(update(Chapter).values(title='作者另起标题', status='draft'))
    r.session.commit()
    source = await resolve_entity_source(r.db, r.pid, 0, r.cid, allow_review_drafts=True)
    assert (await cached_discovery(r.db, source)).cached_count == 0
    assert r.session.execute(select(Candidate)).all() == []
    assert r.session.execute(select(Run.candidate_count)).scalar_one() == 0


@pytest.mark.asyncio
async def test_partial_persistence_or_final_task_failure_rolls_back_entire_discovery(discovery):
    r = discovery; job_id = await seed(r)
    @asynccontextmanager
    async def sessions():
        async with r.sessions() as db:
            execute = db.execute
            async def fail_final(stmt):
                if str(stmt).startswith('UPDATE foreshadow_review_jobs') and stmt.compile().params.get('status') == 'completed':
                    raise RuntimeError('commit not confirmed')
                return await execute(stmt)
            yield NS(execute=fail_final, commit=db.commit)
    with pytest.raises(RuntimeError): await worker.run_review_job(job_id, sessions=sessions)
    assert r.session.execute(select(Candidate)).all() == []
    assert r.session.execute(select(Run)).all() == []
    assert r.session.execute(select(ForeshadowReview)).all() == []
    assert r.session.execute(select(Job.status, Job.discovery_count)).one() == ('failed', None)


@pytest.mark.asyncio
async def test_discovery_failure_fails_job_and_does_not_run_or_persist_payoff_review(discovery):
    r = discovery; job_id = await seed(r)
    r.detector.side_effect = DiscoveryIncomplete('provider failed')
    with pytest.raises(DiscoveryIncomplete): await worker.run_review_job(job_id, sessions=r.sessions)
    r.reviewer.assert_not_called()
    assert r.session.execute(select(Job.status, Job.discovery_count, Job.last_error)).one() == ('failed', None, 'DiscoveryIncomplete')
    assert r.session.execute(select(Run)).all() == []


@pytest.mark.asyncio
async def test_new_revision_reuses_discovery_and_first_author_decision(discovery):
    r = discovery; first = await seed(r)
    assert (await worker.run_review_job(first, sessions=r.sessions))['discovered'] == 1
    cid = r.session.execute(select(Candidate.id)).scalar_one(); r.session.commit()
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid, actor='作者')
    second = await seed(r, 101)
    assert (await worker.run_review_job(second, sessions=r.sessions))['discovered'] == 1
    r.detector.assert_awaited_once()
    assert r.session.execute(select(Candidate.status)).scalar_one() == 'dismissed'
    assert r.session.execute(select(Job.discovery_count).order_by(Job.source_revision)).scalars().all() == [1, 1]


@pytest.mark.asyncio
async def test_real_sql_source_worker_and_both_review_steps_commit_together(discovery, monkeypatch):
    from app.services.foreshadow_review import review_scoped_chapter
    from app.services.foreshadow_manager import ForeshadowManager
    r = discovery
    r.source = await resolve_entity_source(r.db, r.pid, 0, r.cid, allow_review_drafts=True)
    r.session.commit()
    monkeypatch.setattr(worker, 'resolve_entity_source', resolve_entity_source)
    monkeypatch.setattr(worker, 'review_scoped_chapter', review_scoped_chapter)
    monkeypatch.setattr(worker, 'detect_clues', detect_clues)
    monkeypatch.setattr(ForeshadowManager, 'check_resolution', AsyncMock(return_value=list(r.review.suggestions)))
    model = AsyncMock(return_value=NS(text=json.dumps([candidate_json()]), finish_reason='stop'))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    job_id = await seed(r)
    assert await worker.run_review_job(job_id, sessions=r.sessions) == {'status': 'completed', 'saved': 1, 'discovered': 1}
    assert r.session.execute(select(Candidate.status)).scalar_one() == 'pending'
    assert r.session.execute(select(ForeshadowReview.status)).scalar_one() == 'pending'
    assert len(r.session.execute(select(Foreshadow.id)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'position', 'chapter_deleted', 'new_token'])
async def test_source_or_attempt_change_during_discovery_cannot_persist(discovery, monkeypatch, change):
    r = discovery
    r.source = await resolve_entity_source(r.db, r.pid, 0, r.cid, allow_review_drafts=True)
    r.session.commit()
    monkeypatch.setattr(worker, 'resolve_entity_source', resolve_entity_source)
    job_id = await seed(r)
    async def detect(_):
        with Session(r.engine) as db:
            if change == 'body': db.execute(update(Chapter).values(content_text='作者刚改过正文。'))
            if change == 'position': db.execute(update(Chapter).values(global_idx=914))
            if change == 'chapter_deleted': db.execute(delete(Chapter))
            if change == 'new_token': db.execute(update(Job).values(attempt_token=uuid4()))
            db.commit()
        return r.batch
    r.detector.side_effect = detect
    result = await worker.run_review_job(job_id, sessions=r.sessions)
    assert result['status'] == ('superseded' if change == 'new_token' else 'obsolete')
    assert r.session.execute(select(Run)).all() == []
    assert r.session.execute(select(Candidate)).all() == []


@pytest.mark.asyncio
async def test_author_acceptance_creates_once_and_does_not_replay_after_edit_or_delete(discovery):
    r = discovery; cid = await stored_id(r)
    with r.session.begin(): assert await accept_discovery(r.db, r.pid, cid, actor='首位作者') == cid
    row = r.session.execute(select(*Foreshadow.__table__.columns).where(Foreshadow.id == cid)).mappings().one()
    assert (row['status'], row['planted_chapter'], row['source'], row['resolve_conditions_json']) == ('planted', 913, 'lifecycle', [])
    first = r.session.execute(select(Candidate.handled_at, Candidate.handled_by)).one()
    r.session.execute(update(Foreshadow).where(Foreshadow.id == cid).values(description='作者修改过的悬念', status='resolved', resolved_chapter=920))
    r.session.commit()
    with r.session.begin(): await accept_discovery(r.db, r.pid, cid, actor='重试者')
    assert r.session.execute(select(Foreshadow.description).where(Foreshadow.id == cid)).scalar_one() == '作者修改过的悬念'
    assert r.session.execute(select(Candidate.handled_at, Candidate.handled_by)).one() == first
    r.session.execute(delete(Foreshadow).where(Foreshadow.id == cid)); r.session.commit()
    with r.session.begin(): assert await accept_discovery(r.db, r.pid, cid) == cid
    assert r.session.execute(select(Foreshadow.id).where(Foreshadow.id == cid)).all() == []
    assert (await list_discoveries(r.db, r.pid, 'accepted'))['discoveries'][0]['record_present'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'position', 'generating', 'duplicate_index', 'quote', 'snapshot', 'foreign'])
async def test_author_cannot_accept_stale_unknown_ambiguous_or_foreign_evidence(discovery, change):
    r = discovery; cid = await stored_id(r)
    if change == 'body': r.session.execute(update(Chapter).values(content_text='作者改稿。'))
    if change == 'position': r.session.execute(update(Chapter).values(global_idx=1000))
    if change == 'generating': r.session.execute(update(Chapter).values(status='generating'))
    if change == 'duplicate_index': r.session.execute(insert(Chapter).values(id=uuid4(), volume_id=UUID(r.review.volume_id),
        title='重复编号', chapter_idx=4, global_idx=913, content_text='另一章', status='completed'))
    if change == 'quote': r.session.execute(update(Candidate).values(evidence_end=50000))
    if change == 'snapshot': r.session.execute(update(Run).values(source_sha256='0' * 64))
    r.session.commit()
    with pytest.raises(DiscoveryNotFound if change == 'foreign' else DiscoverySourceChanged):
        with r.session.begin(): await accept_discovery(r.db, uuid4() if change == 'foreign' else r.pid, cid)
    assert r.session.execute(select(Candidate.status)).scalar_one() == 'pending'
    assert r.session.execute(select(Foreshadow.id).where(Foreshadow.id == cid)).all() == []
    r.session.commit()
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid, actor='作者')
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid, actor='重试者')
    assert r.session.execute(select(Candidate.handled_by)).scalar_one() == '作者'


@pytest.mark.asyncio
async def test_conflicting_decisions_and_existing_id_never_report_success(discovery):
    r = discovery; cid = await stored_id(r)
    r.session.execute(insert(Foreshadow).values(id=cid, project_id=r.pid, description='已有记录', type='plot', planted_chapter=1))
    r.session.commit()
    with pytest.raises(DiscoveryWriteConflict):
        with r.session.begin(): await accept_discovery(r.db, r.pid, cid)
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid)
    with pytest.raises(DiscoveryAlreadyHandled):
        with r.session.begin(): await accept_discovery(r.db, r.pid, cid)
    assert r.session.execute(select(Foreshadow.description).where(Foreshadow.id == cid)).scalar_one() == '已有记录'


@pytest.mark.asyncio
async def test_pending_graph_create_identity_cannot_be_claimed_by_candidate_acceptance(discovery):
    r = discovery; cid = await stored_id(r)
    r.session.execute(insert(Receipt).values(id=cid, project_id=r.pid, foreshadow_id=cid,
        command_sha256='a' * 64, operation='create', source='neo4j', outcome='pending'))
    r.session.commit()
    with pytest.raises(DiscoveryWriteConflict):
        with r.session.begin(): await accept_discovery(r.db, r.pid, cid)
    assert r.session.execute(select(Foreshadow.id).where(Foreshadow.id == cid)).all() == []
    assert r.session.execute(select(Candidate.status)).scalar_one() == 'pending'


@pytest.mark.asyncio
async def test_new_registration_requires_an_active_project_under_share_lock(discovery):
    from app.models.project import _utcnow
    r = discovery; cid = await stored_id(r)
    r.session.execute(update(Project).values(deleted_at=_utcnow())); r.session.commit()
    statements = []; execute = r.db.execute
    async def record(stmt):
        statements.append(stmt)
        return await execute(stmt)
    r.db.execute = record
    with pytest.raises(DiscoveryProjectUnavailable):
        with r.session.begin(): await accept_discovery(r.db, r.pid, cid)
    assert r.session.execute(select(Candidate.status)).scalar_one() == 'pending'
    assert r.session.execute(select(Foreshadow.id).where(Foreshadow.id == cid)).all() == []
    project_sql = str(statements[1].compile(dialect=postgresql.dialect()))
    assert 'FOR SHARE' in project_sql and 'deleted_at IS NULL' in project_sql


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_failure', [False, True])
async def test_batch_draft_handoff_commits_one_shared_job_or_rolls_it_back(discovery, commit_failure):
    from app.services.foreshadow_review_jobs import enqueue_saved_draft_review
    r = discovery; revisions = []
    r.session.execute(update(Chapter).values(status='draft')); r.session.commit()
    @asynccontextmanager
    async def sessions():
        async with r.sessions() as db:
            async def execute(stmt):
                sql = str(stmt.compile(dialect=postgresql.dialect()))
                if "nextval('entity_source_outbox_revisions')" in sql:
                    revisions.append(sql)
                    return NS(scalar_one=lambda: 1000 + len(revisions))
                return await db.execute(stmt)
            async def commit():
                if commit_failure: raise RuntimeError('commit failed')
                await db.commit()
            yield NS(execute=execute, commit=commit)
    async def enqueue():
        return await enqueue_saved_draft_review(r.pid, UUID(r.review.volume_id), 3, r.source.content, sessions=sessions)
    if commit_failure:
        with pytest.raises(RuntimeError): await enqueue()
        assert r.session.execute(select(Job.id)).all() == []
    else:
        assert await enqueue() == await enqueue()
        assert len(revisions) == 1
        assert r.session.execute(select(Job.status, Job.discovery_count)).one() == ('pending', None)


@pytest.mark.asyncio
@pytest.mark.parametrize('source_model', [Chapter, Volume])
async def test_deleted_sources_keep_history_and_project_purge_removes_it(discovery, source_model):
    r = discovery
    r.session.execute(text('PRAGMA foreign_keys=ON')); r.session.commit()
    cid = await stored_id(r)
    r.session.execute(delete(source_model)); r.session.commit()
    result = await list_discoveries(r.db, r.pid)
    assert result['discoveries'][0]['source_state'] == 'unavailable'
    assert result['discoveries'][0]['evidence'] == '寄信的存根'
    r.session.commit()
    with pytest.raises(DiscoverySourceChanged):
        with r.session.begin(): await accept_discovery(r.db, r.pid, cid)
    r.session.execute(delete(Project)); r.session.commit()
    assert r.session.execute(select(Run)).all() == [] and r.session.execute(select(Candidate)).all() == []


@pytest.mark.asyncio
async def test_list_paginates_with_project_scope_and_source_state(discovery):
    r = discovery; cid = await stored_id(r)
    assert (await list_discoveries(r.db, r.pid, limit=1))['discoveries'][0]['source_state'] == 'current'
    assert (await list_discoveries(r.db, r.pid, limit=1, offset=1))['discoveries'] == []
    assert (await list_discoveries(r.db, uuid4())) == {'discoveries': [], 'has_more': False}
    r.session.execute(update(Chapter).values(content_text='另一稿')); r.session.commit()
    assert (await list_discoveries(r.db, r.pid))['discoveries'][0]['source_state'] == 'changed'


@pytest.mark.asyncio
async def test_api_uses_verified_actor_and_acknowledges_only_after_commit(discovery):
    from app.api import foreshadows as api
    r = discovery; cid = await stored_id(r)
    fail = [True]
    async def commit():
        if fail[0]:
            r.session.rollback()
            raise RuntimeError('private database detail')
        r.session.commit()
    adapter = NS(execute=r.db.execute, commit=AsyncMock(side_effect=commit))
    async def provide(): return adapter
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.auth_username = '已验证作者'
        return await call_next(request)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        base = f'/api/projects/{r.pid}/foreshadows/discoveries'
        listed = await client.get(base)
        assert listed.status_code == 200 and listed.json()['discoveries'][0]['source_state'] == 'current'
        assert (await client.get(base + '?offset=-1')).status_code == 422
        failed = await client.post(base + f'/{cid}/accept', json={'actor': '伪造账号'})
        assert failed.status_code == 503 and 'private' not in failed.text
        assert r.session.execute(select(Foreshadow.id).where(Foreshadow.id == cid)).all() == []
        fail[0] = False
        success = await client.post(base + f'/{cid}/accept')
        assert success.json() == {'id': str(cid), 'status': 'accepted', 'foreshadow_id': str(cid)}
        assert (await client.post(base + f'/{cid}/accept')).status_code == 200
        assert (await client.post(base + f'/{cid}/dismiss')).status_code == 409
    assert r.session.execute(select(Candidate.handled_by)).scalar_one() == '已验证作者'


def test_discovery_migration_matches_schema_and_refuses_loss_of_receipts():
    from tests.test_foreshadow_review_retention import migration
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    base = migration('a1001937_foreshadow_review_jobs')
    step = migration('a1001942_foreshadow_discoveries')
    assert step.down_revision == 'a1001941'
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            base.upgrade(); step.upgrade()
            for model in (Run, Candidate, Job):
                later = {'scan_revision', 'model_name'} if model is Run else {'rescan_discovery', 'discovery_run_id'} if model is Job else set()
                assert {c['name'] for c in inspect(connection).get_columns(model.__tablename__)} == set(model.__table__.columns.keys()) - later
            assert inspect(connection).get_unique_constraints(Run.__tablename__)[0]['column_names'] == ['chapter_id', 'source_sha256']
            step.downgrade(); step.upgrade()
            connection.execute(text('''INSERT INTO foreshadow_discovery_runs
                (id, project_id, chapter_id, source_sha256, source_snapshot, candidate_count, created_at)
                VALUES (:id, :project, :chapter, :sha, '{}', 0, '2026-10-08T00:00:00')'''),
                dict(id=uuid4().hex, project=uuid4().hex, chapter=uuid4().hex, sha='0' * 64))
            with pytest.raises(RuntimeError, match='Preserve'): step.downgrade()
            assert Candidate.__tablename__ in inspect(connection).get_table_names()
    engine.dispose()
