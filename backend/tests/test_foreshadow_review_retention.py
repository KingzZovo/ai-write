import importlib.util
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, inspect, select, text

from app.models.project import Project, Volume, Chapter, Foreshadow, ForeshadowReview
from app.services.foreshadow_review_actions import accept_review, dismiss_review, ReviewSourceChanged
from app.services.foreshadow_review_reader import list_resolution_reviews
from app.services.foreshadow_review_store import persist_resolution_review
from tests.test_foreshadow_review_store import database


def enforce_foreign_keys(session):
    session.execute(text('PRAGMA foreign_keys=ON'))
    assert session.execute(text('PRAGMA foreign_keys')).scalar_one() == 1
    session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('source', [Chapter, Volume, Foreshadow])
@pytest.mark.parametrize('decision', ['pending', 'accepted', 'dismissed'])
async def test_source_deletion_keeps_evidence_decision_and_original_ids(database, source, decision):
    session, db, review, _ = database
    enforce_foreign_keys(session)
    with session.begin(): await persist_resolution_review(db, review)
    record_id = session.execute(select(ForeshadowReview.id)).scalar_one(); session.commit()
    if decision == 'accepted':
        with session.begin(): await accept_review(db, UUID(review.project_id), record_id, actor='writer')
    if decision == 'dismissed':
        with session.begin(): await dismiss_review(db, UUID(review.project_id), record_id, actor='writer')
    before = dict(session.execute(select(*ForeshadowReview.__table__.columns)).mappings().one())
    session.execute(delete(source)); session.commit()
    after = dict(session.execute(select(*ForeshadowReview.__table__.columns)).mappings().one())
    assert after == before
    result = await list_resolution_reviews(db, UUID(review.project_id), decision, 20, 0)
    assert result['reviews'][0]['source_state'] == 'unavailable'
    assert result['reviews'][0]['source_snapshot'] == before['source_snapshot']
    assert result['reviews'][0]['evidence'] == review.suggestions[0].evidence
    assert (await list_resolution_reviews(db, uuid4(), decision, 20, 0))['reviews'] == []
    session.commit()
    if decision == 'pending':
        with pytest.raises(ReviewSourceChanged):
            with session.begin(): await accept_review(db, UUID(review.project_id), record_id)
        with session.begin(): await dismiss_review(db, UUID(review.project_id), record_id)
    elif decision == 'accepted':
        # Idempotent acknowledgement must not recreate deleted story sources.
        with session.begin(): await accept_review(db, UUID(review.project_id), record_id, actor='retry')
        assert session.execute(select(source.id)).all() == []


@pytest.mark.asyncio
async def test_project_purge_still_removes_its_history_after_source_deletion(database):
    session, db, review, _ = database
    enforce_foreign_keys(session)
    with session.begin(): await persist_resolution_review(db, review)
    session.execute(delete(Chapter)); session.commit()
    assert session.execute(select(ForeshadowReview.id)).scalar_one()
    session.execute(delete(Project)); session.commit()
    assert session.execute(select(ForeshadowReview)).all() == []


def migration(name):
    path = Path(__file__).parents[1] / 'alembic/versions' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize('remove_source', [False, True])
async def test_migration_preserves_evidence_and_refuses_lossy_downgrade(database, remove_source):
    session, db, review, _ = database
    enforce_foreign_keys(session)
    engine = session.get_bind()
    ForeshadowReview.__table__.drop(engine)
    baseline = migration('a1001936_foreshadow_reviews')
    decisions = migration('a1001938_foreshadow_review_decisions')
    retention = migration('a1001939_retain_foreshadow_review_history')
    assert retention.down_revision == decisions.revision
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            baseline.upgrade(); decisions.upgrade()
    with session.begin(): await persist_resolution_review(db, review)
    before = dict(session.execute(select(*ForeshadowReview.__table__.columns)).mappings().one()); session.commit()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            retention.upgrade()
            assert {fk['referred_table'] for fk in inspect(connection).get_foreign_keys('foreshadow_reviews')} == {'projects'}
            assert {col['name'] for col in inspect(connection).get_columns('foreshadow_reviews')} == set(ForeshadowReview.__table__.columns.keys())
            assert inspect(connection).get_unique_constraints('foreshadow_reviews')[0]['name'] == 'uq_foreshadow_review_snapshot'
            assert inspect(connection).get_indexes('foreshadow_reviews')[0]['name'] == 'ix_foreshadow_reviews_project_status'
    assert dict(session.execute(select(*ForeshadowReview.__table__.columns)).mappings().one()) == before
    if remove_source: session.execute(delete(Chapter))
    session.commit()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            if remove_source:
                with pytest.raises(RuntimeError, match='retained review history'):
                    retention.downgrade()
                assert {fk['referred_table'] for fk in inspect(connection).get_foreign_keys('foreshadow_reviews')} == {'projects'}
            else:
                retention.downgrade()
                assert {fk['referred_table'] for fk in inspect(connection).get_foreign_keys('foreshadow_reviews')} == {'projects', 'chapters', 'foreshadows'}
    assert dict(session.execute(select(*ForeshadowReview.__table__.columns)).mappings().one()) == before
