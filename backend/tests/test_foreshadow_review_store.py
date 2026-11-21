import hashlib
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects import postgresql

from app.models.project import Project, Volume, Chapter, Foreshadow, ForeshadowReview, ForeshadowConfirmationJob
from app.services.foreshadow_manager import ResolutionSuggestion
from app.services.foreshadow_provenance import REVIEW_SOURCE_FIELDS, foreshadow_source_hash
from app.services.foreshadow_review import ScopedResolutionReview
from app.services.foreshadow_review_store import persist_resolution_review


@pytest.fixture
def database():
    engine = create_engine('sqlite://')
    for model in (Project, Volume, Chapter, Foreshadow, ForeshadowReview, ForeshadowConfirmationJob):
        model.__table__.create(engine)
    with Session(engine) as session:
        pid, vid, cid, fid = uuid4(), uuid4(), uuid4(), uuid4()
        session.execute(insert(Project).values(id=pid, title='测试作品'))
        session.execute(insert(Volume).values(id=vid, project_id=pid, title='卷二', volume_idx=2))
        text = '陈伯把寄信的存根放在桌上。'
        session.execute(insert(Chapter).values(id=cid, volume_id=vid, title='信', chapter_idx=3,
            global_idx=913, content_text=text, status='completed'))
        session.execute(insert(Foreshadow).values(id=fid, project_id=pid, type='mystery', description='寄信人',
            planted_chapter=910, status='ready', source='lifecycle', resolve_conditions_json=['身份明确']))
        session.commit()
        chapter = session.execute(select(Chapter.updated_at)).scalar_one()
        fs = session.execute(select(*(getattr(Foreshadow, key) for key in REVIEW_SOURCE_FIELDS))).mappings().one()
        sha = hashlib.sha256(text.encode()).hexdigest()
        suggestion = ResolutionSuggestion(str(fid), 913, '寄信的存根', 4, 9, '出示了寄信凭据，需作者核对',
                                          sha, foreshadow_source_hash(fs), '寄信人')
        # Offsets are measured against the actual supplied source.
        start = text.index(suggestion.evidence)
        suggestion = replace(suggestion, evidence_start=start, evidence_end=start + len(suggestion.evidence))
        review = ScopedResolutionReview(str(pid), str(vid), str(cid), 3, 913, sha, chapter, 'completed', (suggestion,))
        session.commit()
        queries = []
        async def execute(statement):
            queries.append(statement)
            return session.execute(statement)
        yield session, NS(execute=execute), review, queries
    engine.dispose()


@pytest.mark.asyncio
async def test_persists_evidence_deduplicates_and_does_not_reset_review_or_foreshadow(database):
    session, db, review, queries = database
    with session.begin():
        assert await persist_resolution_review(db, review) == 1
    saved = session.execute(select(ForeshadowReview)).scalar_one()
    assert saved.status == 'pending' and saved.evidence == review.suggestions[0].evidence
    assert saved.source_snapshot['global_idx'] == 913
    assert saved.source_snapshot['foreshadow']['description'] == '寄信人'
    session.execute(update(ForeshadowReview).values(status='dismissed'))
    session.commit()
    with session.begin():
        assert await persist_resolution_review(db, review) == 0
    session.expire_all()
    assert session.execute(select(ForeshadowReview.status)).scalar_one() == 'dismissed'
    assert session.execute(select(Foreshadow.status)).scalar_one() == 'ready'
    sql = str(queries[0].compile(dialect=postgresql.dialect()))
    assert 'FOR UPDATE OF chapters, volumes' in sql
    assert queries[0].get_execution_options()['autoflush'] is False
    assert 'ORDER BY foreshadows.id FOR UPDATE' in str(queries[1].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['text', 'chapter_position', 'chapter_status', 'foreshadow', 'foreign_project'])
async def test_stale_or_foreign_source_is_not_persisted(database, change):
    session, db, review, _ = database
    if change == 'text': session.execute(update(Chapter).values(content_text='作者改稿'))
    if change == 'chapter_position': session.execute(update(Chapter).values(global_idx=914))
    if change == 'chapter_status': session.execute(update(Chapter).values(status='draft'))
    if change == 'foreshadow': session.execute(update(Foreshadow).values(description='另一个悬念'))
    if change == 'foreign_project': review = replace(review, project_id=str(uuid4()))
    session.commit()
    with session.begin():
        assert await persist_resolution_review(db, review) == 0
    assert session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
async def test_invalid_evidence_rolls_back_prior_insert_in_same_transaction(database):
    session, db, review, _ = database
    invalid = replace(review.suggestions[0], evidence='伪造原句')
    with pytest.raises(ValueError, match='invalid_foreshadow_review_evidence'):
        with session.begin():
            await persist_resolution_review(db, replace(review, suggestions=(*review.suggestions, invalid)))
    assert session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
async def test_hook_hands_off_without_committing_borrowed_generation_session(monkeypatch):
    from app.services import foreshadow_review_jobs
    from app.services.hook_manager import HookManager
    enqueue = AsyncMock()
    monkeypatch.setattr(foreshadow_review_jobs, 'enqueue_saved_draft_review', enqueue)
    db = NS(execute=AsyncMock(), commit=AsyncMock())
    await HookManager(db=db)._enqueue_foreshadow_review('p', 'v', 3, '原稿')
    enqueue.assert_awaited_once_with('p', 'v', 3, '原稿')
    db.commit.assert_not_called()


def test_migration_creates_matching_columns_indexes_and_can_downgrade():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    path = Path(__file__).parents[1] / 'alembic/versions/a1001936_foreshadow_reviews.py'
    spec = importlib.util.spec_from_file_location('review_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == 'a1001935'
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            inspector = inspect(connection)
            assert {c['name'] for c in inspector.get_columns('foreshadow_reviews')} == set(ForeshadowReview.__table__.columns.keys()) - {'handled_at', 'handled_by'}
            unique = inspector.get_unique_constraints('foreshadow_reviews')
            assert unique[0]['column_names'] == ['chapter_id', 'foreshadow_id', 'snapshot_sha256']
            assert {fk['referred_table'] for fk in inspector.get_foreign_keys('foreshadow_reviews')} == {'projects', 'chapters', 'foreshadows'}
            assert inspector.get_indexes('foreshadow_reviews')[0]['name'] == 'ix_foreshadow_reviews_project_status'
            migration.downgrade()
            assert 'foreshadow_reviews' not in inspect(connection).get_table_names()
    engine.dispose()


@pytest.mark.asyncio
async def test_evidence_offsets_cannot_run_past_source_end(database):
    session, db, review, _ = database
    text = session.execute(select(Chapter.content_text)).scalar_one()
    session.commit()
    invalid = replace(review.suggestions[0], evidence=text, evidence_start=0, evidence_end=len(text) + 1)
    with pytest.raises(ValueError, match='invalid_foreshadow_review_evidence'):
        with session.begin():
            await persist_resolution_review(db, replace(review, suggestions=(invalid,)))
    assert session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change, expected', [
    ('none', 'current'), ('body', 'changed'), ('foreshadow', 'changed'),
    ('snapshot', 'unknown'), ('offset', 'unknown'), ('malformed', 'unknown'),
])
async def test_review_reader_reports_current_changed_and_unknown_sources(database, change, expected):
    from app.services.foreshadow_review_reader import list_resolution_reviews
    session, db, review, queries = database
    with session.begin():
        await persist_resolution_review(db, review)
    if change == 'body': session.execute(update(Chapter).values(content_text='作者新稿'))
    if change == 'foreshadow': session.execute(update(Foreshadow).values(description='新问题'))
    if change == 'snapshot': session.execute(update(ForeshadowReview).values(snapshot_sha256='0' * 64))
    if change == 'offset': session.execute(update(ForeshadowReview).values(evidence_end=99999))
    if change == 'malformed': session.execute(update(ForeshadowReview).values(source_snapshot=[]))
    session.commit()
    queries.clear()
    result = await list_resolution_reviews(db, UUID(review.project_id), 'pending', 20, 0)
    assert len(result['reviews']) == 1 and not result['has_more']
    assert result['reviews'][0]['source_state'] == expected
    if expected == 'unknown':
        assert result['reviews'][0]['source_snapshot'] == {}
    assert len(queries) == 3
    for query in queries:
        assert query.get_execution_options()['autoflush'] is False
        assert UUID(review.project_id) in query.compile(dialect=postgresql.dialect()).params.values()
    assert await list_resolution_reviews(db, uuid4(), 'pending', 20, 0) == {'reviews': [], 'has_more': False}


@pytest.mark.asyncio
async def test_graph_projection_match_is_not_reported_as_authoritative_current(database):
    from app.services.foreshadow_review_reader import list_resolution_reviews
    session, db, review, _ = database
    session.execute(update(Foreshadow).values(source='neo4j'))
    source = session.execute(select(*(getattr(Foreshadow, key) for key in REVIEW_SOURCE_FIELDS))).mappings().one()
    review = replace(review, suggestions=(replace(review.suggestions[0], foreshadow_sha256=foreshadow_source_hash(source)),))
    session.commit()
    with session.begin():
        assert await persist_resolution_review(db, review) == 1
    result = await list_resolution_reviews(db, UUID(review.project_id), 'pending', 20, 0)
    assert result['reviews'][0]['source_state'] == 'projection_current'


@pytest.mark.asyncio
async def test_review_list_route_returns_evidence_and_validates_pagination(database):
    import httpx
    from fastapi import FastAPI
    from app.api import foreshadows as api
    session, db, review, _ = database
    with session.begin():
        await persist_resolution_review(db, review)
    app = FastAPI()
    app.include_router(api.router)
    async def provide_db(): return db
    app.dependency_overrides[api.get_db] = provide_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        base = f'/api/projects/{review.project_id}/foreshadows/reviews'
        response = await client.get(base)
        assert response.status_code == 200
        item = response.json()['reviews'][0]
        assert item['evidence'] == review.suggestions[0].evidence
        assert item['source_state'] == 'current'
        for suffix in ('?limit=0', '?offset=-1', '?status=unknown'):
            assert (await client.get(base + suffix)).status_code == 422
        assert (await client.get(base + '?offset=1')).json() == {'reviews': [], 'has_more': False}
        assert (await client.get(base + '?status=dismissed')).json()['reviews'] == []


@pytest.mark.asyncio
async def test_dismissal_is_scoped_idempotent_and_never_changes_story_facts(database):
    from app.services.foreshadow_review_actions import dismiss_review, ReviewNotFound, ReviewAlreadyHandled
    session, db, review, queries = database
    with session.begin():
        await persist_resolution_review(db, review)
    review_id = session.execute(select(ForeshadowReview.id)).scalar_one()
    session.commit()
    with pytest.raises(ReviewNotFound):
        with session.begin():
            await dismiss_review(db, uuid4(), review_id)
    for _ in range(2):
        with session.begin():
            await dismiss_review(db, UUID(review.project_id), review_id)
    assert session.execute(select(ForeshadowReview.status)).scalar_one() == 'dismissed'
    assert session.execute(select(Foreshadow.status)).scalar_one() == 'ready'
    session.execute(update(ForeshadowReview).values(status='accepted'))
    session.commit()
    with pytest.raises(ReviewAlreadyHandled):
        with session.begin():
            await dismiss_review(db, UUID(review.project_id), review_id)
    assert session.execute(select(ForeshadowReview.status)).scalar_one() == 'accepted'


@pytest.mark.asyncio
@pytest.mark.parametrize('action, outcome', [('dismiss', 'dismissed'), ('accept', 'accepted')])
async def test_review_action_route_acknowledges_only_after_commit_and_maps_conflicts(database, action, outcome):
    import httpx
    from fastapi import FastAPI
    from app.api import foreshadows as api
    session, db, review, _ = database
    with session.begin():
        await persist_resolution_review(db, review)
    review_id = session.execute(select(ForeshadowReview.id)).scalar_one()
    session.commit()
    async def commit(): session.commit()
    adapter = NS(execute=db.execute, commit=AsyncMock(side_effect=commit))
    async def provide_db(): return adapter
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = provide_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        route = f'/api/projects/{review.project_id}/foreshadows/reviews/{review_id}/{action}'
        response = await client.post(route)
        assert response.status_code == 200 and response.json()['status'] == outcome
        adapter.commit.assert_awaited_once()
        assert (await client.post(route)).status_code == 200
        assert (await client.post(route.replace(review.project_id, str(uuid4())))).status_code == 404
        session.execute(update(ForeshadowReview).values(status='accepted' if action == 'dismiss' else 'dismissed'))
        session.commit()
        assert (await client.post(route)).status_code == 409


@pytest.mark.asyncio
async def test_failed_dismiss_commit_does_not_return_success():
    from app.api.foreshadows import dismiss_foreshadow_review
    db = NS(execute=AsyncMock(return_value=NS(scalar_one_or_none=lambda: uuid4())),
            commit=AsyncMock(side_effect=RuntimeError('commit unavailable')))
    with pytest.raises(RuntimeError, match='commit unavailable'):
        await dismiss_foreshadow_review(uuid4(), uuid4(), NS(state=NS()), db)


@pytest.mark.asyncio
async def test_accept_review_updates_both_records_and_retry_never_replays(database):
    from app.services.foreshadow_review_actions import accept_review, dismiss_review, ReviewAlreadyHandled
    session, db, review, queries = database
    with session.begin(): await persist_resolution_review(db, review)
    review_id = session.execute(select(ForeshadowReview.id)).scalar_one()
    session.commit()
    queries.clear()
    with session.begin(): await accept_review(db, UUID(review.project_id), review_id)
    assert session.execute(select(Foreshadow.status, Foreshadow.resolved_chapter)).one() == ('resolved', 913)
    assert session.execute(select(ForeshadowReview.status)).scalar_one() == 'accepted'
    session.execute(update(Foreshadow).values(status='planted', resolved_chapter=None))
    session.commit()
    with session.begin(): await accept_review(db, UUID(review.project_id), review_id)
    assert session.execute(select(Foreshadow.status)).scalar_one() == 'planted'
    session.commit()
    with pytest.raises(ReviewAlreadyHandled):
        with session.begin(): await dismiss_review(db, UUID(review.project_id), review_id)
    assert 'FOR UPDATE OF chapters, volumes' in str(queries[1].compile(dialect=postgresql.dialect()))
    assert 'FOR UPDATE' in str(queries[2].compile(dialect=postgresql.dialect()))
    assert 'FOR UPDATE' in str(queries[3].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'foreshadow', 'dismissed', 'foreign_project', 'legacy_source'])
async def test_acceptance_rejects_stale_processed_foreign_or_graph_sources(database, change):
    from app.services.foreshadow_review_actions import accept_review, ReviewSourceChanged, ReviewSourceRequiresSync, ReviewAlreadyHandled, ReviewNotFound
    session, db, review, _ = database
    if change == 'legacy_source':
        session.execute(update(Foreshadow).values(source=None))
        source = session.execute(select(*(getattr(Foreshadow, key) for key in REVIEW_SOURCE_FIELDS))).mappings().one()
        review = replace(review, suggestions=(replace(review.suggestions[0], foreshadow_sha256=foreshadow_source_hash(source)),))
        session.commit()
    with session.begin(): await persist_resolution_review(db, review)
    review_id = session.execute(select(ForeshadowReview.id)).scalar_one()
    if change == 'body': session.execute(update(Chapter).values(content_text='改稿'))
    if change == 'foreshadow': session.execute(update(Foreshadow).values(description='另一个悬念'))
    if change == 'dismissed': session.execute(update(ForeshadowReview).values(status='dismissed'))
    session.commit()
    error = {'legacy_source': ReviewSourceRequiresSync, 'dismissed': ReviewAlreadyHandled, 'foreign_project': ReviewNotFound}.get(change, ReviewSourceChanged)
    with pytest.raises(error):
        with session.begin():
            await accept_review(db, uuid4() if change == 'foreign_project' else UUID(review.project_id), review_id)
    assert session.execute(select(Foreshadow.status)).scalar_one() == 'ready'
    assert session.execute(select(ForeshadowReview.status)).scalar_one() == ('dismissed' if change == 'dismissed' else 'pending')


@pytest.mark.asyncio
async def test_second_acceptance_write_failure_rolls_back_foreshadow_change(database):
    from app.services.foreshadow_review_actions import accept_review
    session, db, review, _ = database
    with session.begin(): await persist_resolution_review(db, review)
    review_id = session.execute(select(ForeshadowReview.id)).scalar_one()
    session.commit()
    async def execute(statement):
        if str(statement).startswith('UPDATE foreshadow_reviews'):
            raise RuntimeError('review write failed')
        return await db.execute(statement)
    with pytest.raises(RuntimeError, match='review write failed'):
        with session.begin(): await accept_review(NS(execute=execute), UUID(review.project_id), review_id)
    assert session.execute(select(Foreshadow.status)).scalar_one() == 'ready'
    assert session.execute(select(ForeshadowReview.status)).scalar_one() == 'pending'


@pytest.mark.asyncio
async def test_accept_commit_failure_never_acknowledges_success(monkeypatch):
    from app.api.foreshadows import accept_foreshadow_review
    from app.services import foreshadow_review_actions
    monkeypatch.setattr(foreshadow_review_actions, 'accept_review', AsyncMock())
    db = NS(commit=AsyncMock(side_effect=RuntimeError('commit unavailable')))
    with pytest.raises(RuntimeError, match='commit unavailable'):
        await accept_foreshadow_review(uuid4(), uuid4(), NS(state=NS()), db)


@pytest.mark.asyncio
@pytest.mark.parametrize('change, expected', [
    ('none', 'accepted_current'), ('body', 'changed'), ('description', 'changed'),
    ('reopened', 'changed'), ('chapter', 'changed'), ('hash', 'unknown'), ('evidence', 'unknown'),
])
async def test_accepted_history_checks_post_decision_state_and_preserves_original_snapshot(database, change, expected):
    from app.services.foreshadow_review_actions import accept_review
    from app.services.foreshadow_review_reader import list_resolution_reviews
    session, db, review, _ = database
    with session.begin(): await persist_resolution_review(db, review)
    before = session.execute(select(ForeshadowReview)).scalar_one()
    review_id, snapshot, digest = before.id, before.source_snapshot, before.snapshot_sha256
    session.commit()
    with session.begin(): await accept_review(db, UUID(review.project_id), review_id)
    if change == 'body': session.execute(update(Chapter).values(content_text='新正文'))
    if change == 'description': session.execute(update(Foreshadow).values(description='另一条悬念'))
    if change == 'reopened': session.execute(update(Foreshadow).values(status='ready', resolved_chapter=None))
    if change == 'chapter': session.execute(update(Foreshadow).values(resolved_chapter=914))
    if change == 'hash': session.execute(update(ForeshadowReview).values(snapshot_sha256='0' * 64))
    if change == 'evidence': session.execute(update(ForeshadowReview).values(evidence='不在正文中的话'))
    session.commit()
    result = await list_resolution_reviews(db, UUID(review.project_id), 'accepted', 20, 0)
    assert len(result['reviews']) == 1
    record = result['reviews'][0]
    assert record['status'] == 'accepted' and record['source_state'] == expected
    stored = session.execute(select(ForeshadowReview.source_snapshot, ForeshadowReview.snapshot_sha256)).one()
    assert stored.source_snapshot == snapshot and snapshot['foreshadow']['status'] == 'ready'
    if change != 'hash': assert stored.snapshot_sha256 == digest
    assert (await list_resolution_reviews(db, UUID(review.project_id), 'pending', 20, 0))['reviews'] == []
    assert (await list_resolution_reviews(db, uuid4(), 'accepted', 20, 0))['reviews'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('action_name', ['accept_review', 'dismiss_review'])
async def test_decision_actor_and_time_are_atomic_and_first_decision_survives_retries(database, monkeypatch, action_name):
    from datetime import datetime, timezone
    from app.services import foreshadow_review_actions as actions
    session, db, review, _ = database
    with session.begin(): await persist_resolution_review(db, review)
    review_id = session.execute(select(ForeshadowReview.id)).scalar_one(); session.commit()
    action = getattr(actions, action_name)
    first = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(actions, '_utcnow', lambda: first)
    with pytest.raises(RuntimeError):
        with session.begin():
            await action(db, UUID(review.project_id), review_id, actor='writer')
            raise RuntimeError('transaction failed')
    assert session.execute(select(ForeshadowReview.handled_at, ForeshadowReview.handled_by)).one() == (None, None)
    session.commit()
    with session.begin(): await action(db, UUID(review.project_id), review_id, actor='writer')
    monkeypatch.setattr(actions, '_utcnow', lambda: datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    with session.begin(): await action(db, UUID(review.project_id), review_id, actor='another-user')
    row = session.execute(select(ForeshadowReview.handled_at, ForeshadowReview.handled_by)).one()
    assert row.handled_at.replace(tzinfo=timezone.utc) == first and row.handled_by == 'writer'


def test_decision_migration_preserves_unknown_legacy_values_and_downgrades():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    engine = create_engine('sqlite://')
    modules = []
    for name in ('a1001936_foreshadow_reviews', 'a1001938_foreshadow_review_decisions'):
        path = Path(__file__).parents[1] / 'alembic/versions' / (name + '.py')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); modules.append(module)
    assert modules[1].down_revision == 'a1001937'
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            modules[0].upgrade()
            connection.execute(text("INSERT INTO foreshadow_reviews (id,project_id,chapter_id,foreshadow_id,snapshot_sha256,source_snapshot,evidence,evidence_start,evidence_end,reason,status,created_at) VALUES ('r','p','c','f','hash','{}','正文',0,2,'解释','accepted','2026-10-07')"))
            modules[1].upgrade()
            assert connection.execute(text('SELECT status, handled_at, handled_by FROM foreshadow_reviews')).one() == ('accepted', None, None)
            assert {c['name'] for c in inspect(connection).get_columns('foreshadow_reviews')} == set(ForeshadowReview.__table__.columns.keys())
            modules[1].downgrade()
            assert connection.execute(text('SELECT status FROM foreshadow_reviews')).scalar_one() == 'accepted'
            assert 'handled_at' not in {c['name'] for c in inspect(connection).get_columns('foreshadow_reviews')}
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['accept', 'dismiss'])
async def test_decision_route_uses_verified_request_identity_not_client_payload(database, action):
    import httpx
    from fastapi import FastAPI, Request
    from app.api import foreshadows as api
    session, db, review, _ = database
    with session.begin(): await persist_resolution_review(db, review)
    review_id = session.execute(select(ForeshadowReview.id)).scalar_one(); session.commit()
    async def commit(): session.commit()
    async def provide_db(): return NS(execute=db.execute, commit=commit)
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide_db
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.auth_username = 'verified-writer'
        return await call_next(request)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/api/projects/{review.project_id}/foreshadows/reviews/{review_id}/{action}', json={'handled_by': 'forged'})
        assert response.status_code == 200
    assert session.execute(select(ForeshadowReview.handled_by)).scalar_one() == 'verified-writer'
