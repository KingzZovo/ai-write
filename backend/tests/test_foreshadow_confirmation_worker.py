from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
import json

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.models.project import Foreshadow, ForeshadowReview as Review, ForeshadowConfirmationJob as Job, Chapter, _utcnow
from app.services.foreshadow_provenance import REVIEW_SOURCE_FIELDS, foreshadow_source_hash
from app.services.foreshadow_review_store import persist_resolution_review
from app.services.foreshadow_review_actions import accept_review, dismiss_review, ReviewAlreadyHandled
from app.services.foreshadow_review_reader import list_resolution_reviews
from app.services import foreshadow_confirmation_worker as worker
from tests.test_foreshadow_review_store import database
from tests.test_foreshadow_graph_confirmation import Graph


@pytest_asyncio.fixture
async def runtime(database, monkeypatch, request):
    session, db, review, _ = database
    initial_status = getattr(request, 'param', 'ready')
    session.execute(update(Foreshadow).values(source='neo4j', status=initial_status, resolution_blueprint_json={}))
    fs = dict(session.execute(select(*(getattr(Foreshadow, k) for k in REVIEW_SOURCE_FIELDS))).mappings().one()); session.commit()
    review = replace(review, suggestions=(replace(review.suggestions[0], foreshadow_sha256=foreshadow_source_hash(fs)),))
    with session.begin(): await persist_resolution_review(db, review)
    rid = session.execute(select(Review.id)).scalar_one(); session.commit()
    r = NS(session=session, db=db, review=review, id=rid, pid=UUID(review.project_id), fail_commit=False, initial_status=initial_status, engine=session.get_bind())
    @asynccontextmanager
    async def sessions():
        with Session(r.engine) as store:
            async def execute(stmt): return store.execute(stmt)
            async def commit():
                if r.fail_commit and store.execute(select(Review.status)).scalar_one_or_none() == 'accepted':
                    r.fail_commit = False
                    raise RuntimeError('database commit failed')
                store.commit()
            yield NS(execute=execute, commit=commit)
    r.sessions = sessions
    r.graph = Graph({**fs, 'resolve_conditions_json': json.dumps(fs['resolve_conditions_json']),
                     'resolution_blueprint_json': json.dumps(fs['resolution_blueprint_json'])})
    r.materialization_lock = AsyncMock()
    monkeypatch.setattr(worker, 'require_materialization_lock', r.materialization_lock)
    return r


async def stage(r):
    with r.session.begin(): assert await accept_review(r.db, r.pid, r.id, actor='writer') == 'confirming'


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ready', 'planted'], indirect=True)
async def test_graph_intent_commits_without_pretending_accepted_and_completes(runtime):
    r = runtime; await stage(r)
    with r.session.begin(): assert await accept_review(r.db, r.pid, r.id, actor='retry') == 'confirming'
    assert r.session.execute(select(Job.requested_by)).scalar_one() == 'writer'
    assert r.session.execute(select(Review.status)).scalar_one() == 'confirming'
    assert r.session.execute(select(Foreshadow.status)).scalar_one() == r.initial_status
    r.session.commit()
    assert (await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions))['status'] == 'completed'
    assert r.graph.writes == 1
    assert r.session.execute(select(Review.status, Review.handled_by)).one() == ('accepted', 'writer')
    assert r.session.execute(select(Foreshadow.status, Foreshadow.resolved_chapter)).one() == ('resolved', 913)
    assert (await list_resolution_reviews(r.db, r.pid, 'accepted', 20, 0))['reviews'][0]['source_state'] == 'projection_current'
    r.materialization_lock.assert_awaited_once()
    r.session.commit()
    assert (await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions))['status'] == 'not_claimed'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['sql_commit', 'graph_ack'])
async def test_uncertain_graph_success_is_recovered_without_overwriting_newer_author_change(runtime, failure):
    r = runtime; await stage(r)
    r.fail_commit = failure == 'sql_commit'; r.graph.lose_commit_ack = failure == 'graph_ack'
    with pytest.raises((RuntimeError, ConnectionError)):
        await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions)
    assert r.graph.writes == 1
    assert r.session.execute(select(Review.status)).scalar_one() == 'confirming'
    assert r.session.execute(select(Job.status)).scalar_one() == 'failed'
    r.graph.node.update(description='作者的新悬念', status='planted', resolved_chapter=None)
    r.session.execute(update(Chapter).values(content_text='作者的新正文')); r.session.commit()
    assert (await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions))['status'] == 'completed'
    assert r.graph.writes == 1
    assert r.session.execute(select(Foreshadow.description, Foreshadow.status)).one() == ('作者的新悬念', 'planted')
    assert r.session.execute(select(Review.status, Review.handled_by)).one() == ('accepted', 'writer')
    assert (await list_resolution_reviews(r.db, r.pid, 'accepted', 20, 0))['reviews'][0]['source_state'] == 'changed'


@pytest.mark.asyncio
async def test_changed_chapter_rejects_without_graph_edit_and_can_be_dismissed(runtime):
    r = runtime; await stage(r)
    with pytest.raises(ReviewAlreadyHandled):
        with r.session.begin(): await dismiss_review(r.db, r.pid, r.id)
    r.session.execute(update(Chapter).values(content_text='改稿')); r.session.commit()
    assert (await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions))['status'] == 'rejected'
    assert r.graph.writes == 0
    assert (await list_resolution_reviews(r.db, r.pid, 'confirming', 20, 0))['reviews'][0]['confirmation_status'] == 'rejected'
    r.session.commit()
    with r.session.begin(): await dismiss_review(r.db, r.pid, r.id, actor='writer')
    assert r.session.execute(select(Review.status)).scalar_one() == 'dismissed'


@pytest.mark.asyncio
async def test_graph_unavailable_keeps_recoverable_intent_and_safe_error(runtime):
    r = runtime; await stage(r)
    r.graph.fail_receipt = True
    with pytest.raises(RuntimeError): await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions)
    assert r.graph.writes == 0 and r.graph.receipt is None
    assert r.session.execute(select(Job.status, Job.last_error)).one() == ('failed', 'RuntimeError')
    r.session.commit()
    with r.session.begin(): assert await accept_review(r.db, r.pid, r.id, actor='retry') == 'confirming'
    assert r.session.execute(select(Job.attempts, Job.requested_by)).one() == (0, 'writer')


@pytest.mark.asyncio
async def test_recovery_requeues_abandoned_work_and_exhausts_old_running_attempts(runtime):
    r = runtime; await stage(r)
    r.session.execute(update(Job).values(status='running', attempts=1, updated_at=_utcnow() - timedelta(minutes=11))); r.session.commit()
    dispatched = []
    assert await worker.replay_confirmations(r.sessions, dispatch=dispatched.append) == {'examined': 1, 'sent': 1}
    assert dispatched == [str(r.id)]
    r.session.execute(update(Job).values(status='running', attempts=3, updated_at=_utcnow() - timedelta(minutes=11))); r.session.commit()
    assert await worker.replay_confirmations(r.sessions, dispatch=dispatched.append) == {'examined': 0, 'sent': 0}
    assert r.session.execute(select(Job.status)).scalar_one() == 'failed'


@pytest.mark.asyncio
async def test_route_acknowledges_only_durable_intent_and_exposes_safe_progress(runtime):
    import httpx
    from fastapi import FastAPI
    from app.api import foreshadows as api
    r = runtime
    async def commit(): r.session.commit()
    async def provide_db(): return NS(execute=r.db.execute, commit=commit)
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        base = f'/api/projects/{r.pid}/foreshadows/reviews'
        response = await client.post(base + f'/{r.id}/accept')
        assert response.status_code == 202 and response.json() == {'id': str(r.id), 'status': 'confirming'}
        assert r.session.execute(select(Job.status)).scalar_one() == 'pending'
        r.session.commit()
        response = await client.get(base + '?status=confirming')
        assert response.status_code == 200 and response.json()['reviews'][0]['confirmation_status'] == 'pending'
        assert 'result_json' not in response.text and 'requested_by' not in response.text
        r.session.commit()
        assert (await client.post(f'/api/projects/{uuid4()}/foreshadows/reviews/{r.id}/accept')).status_code == 404
    assert r.graph.writes == 0


@pytest.mark.asyncio
async def test_intent_and_review_status_roll_back_together(runtime):
    r = runtime
    with pytest.raises(RuntimeError):
        with r.session.begin():
            await accept_review(r.db, r.pid, r.id, actor='writer')
            raise RuntimeError('failed acknowledgement')
    assert r.session.execute(select(Review.status)).scalar_one() == 'pending'
    assert r.session.execute(select(Job)).all() == []


@pytest.mark.asyncio
async def test_confirmation_migration_matches_model_and_refuses_unsettled_downgrade(runtime):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    r = runtime
    path = Path(__file__).parents[1] / 'alembic/versions/a1001940_foreshadow_confirmation_jobs.py'
    spec = importlib.util.spec_from_file_location('confirm_migration', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.down_revision == 'a1001939'
    Job.__table__.drop(r.engine)
    with r.engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert {c['name'] for c in inspect(connection).get_columns(Job.__tablename__)} == set(Job.__table__.columns.keys())
            assert {fk['referred_table'] for fk in inspect(connection).get_foreign_keys(Job.__tablename__)} == {'projects', 'foreshadow_reviews'}
    await stage(r)
    for status in ('pending', 'rejected'):
        r.session.execute(update(Job).values(status=status)); r.session.commit()
        with r.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match='outstanding graph confirmations'): module.downgrade()
    with r.session.begin(): await dismiss_review(r.db, r.pid, r.id)
    with r.engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            assert Job.__tablename__ not in inspect(connection).get_table_names()


@pytest.mark.asyncio
async def test_recovery_keeps_applied_history_when_story_sources_were_deleted(runtime):
    from sqlalchemy import delete
    r = runtime; await stage(r); r.fail_commit = True
    with pytest.raises(RuntimeError): await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions)
    r.graph.node = None
    r.session.execute(delete(Chapter)); r.session.commit()
    assert (await worker.run_confirmation(r.id, driver=r.graph, sessions=r.sessions))['status'] == 'completed'
    assert r.graph.writes == 1 and r.graph.node is None
    assert r.session.execute(select(Foreshadow)).all() == []
    assert r.session.execute(select(Review.status)).scalar_one() == 'accepted'
    assert (await list_resolution_reviews(r.db, r.pid, 'accepted', 20, 0))['reviews'][0]['source_state'] == 'unavailable'
