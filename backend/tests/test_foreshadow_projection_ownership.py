from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles, deregister

from app.models.project import Foreshadow, Project
from app.services.foreshadow_projection import project_foreshadow_rows, ForeshadowProjectionConflict
from tests.test_foreshadow_review_store import database


@pytest.fixture
def projection(database):
    # SQLite executes the ownership/rollback branches. Render only its JSONB
    # casts as TEXT; actual PG JSON equality/locking still needs PG integration.
    @compiles(JSONB, 'sqlite')
    def sqlite_jsonb(element, compiler, **kwargs):
        return 'TEXT'
    session, db, review, queries = database
    row = dict(session.execute(select(*Foreshadow.__table__.columns)).mappings().one())
    session.commit()
    try:
        yield NS(session=session, db=db, pid=UUID(review.project_id), row=row, queries=queries)
    finally:
        deregister(JSONB)


def incoming(r, **changes):
    return dict(r.row, source='neo4j', description='图谱里的记录', **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['lifecycle', None, 'foreign_project'])
async def test_bulk_graph_sync_cannot_overwrite_local_unknown_or_foreign_owner(projection, owner):
    r = projection
    if owner == 'foreign_project':
        other = uuid4()
        r.session.execute(insert(Project).values(id=other, title='另一作品'))
        r.session.execute(update(Foreshadow).values(project_id=other, source='neo4j'))
    else:
        r.session.execute(update(Foreshadow).values(source=owner))
    r.session.commit()
    before = dict(r.session.execute(select(*Foreshadow.__table__.columns)).mappings().one()); r.session.commit()
    with pytest.raises(ForeshadowProjectionConflict, match='owner_changed'):
        with r.session.begin(): await project_foreshadow_rows(r.db, r.pid, [incoming(r)])
    assert dict(r.session.execute(select(*Foreshadow.__table__.columns)).mappings().one()) == before


@pytest.mark.asyncio
async def test_owned_graph_rows_update_once_without_changing_project_or_source(projection):
    r = projection
    r.session.execute(update(Foreshadow).values(source='neo4j')); r.session.commit()
    with r.session.begin(): assert await project_foreshadow_rows(r.db, r.pid, [incoming(r)]) == 1
    with r.session.begin(): assert await project_foreshadow_rows(r.db, r.pid, [incoming(r)]) == 0
    assert r.session.execute(select(Foreshadow.description, Foreshadow.source, Foreshadow.project_id)).one() == ('图谱里的记录', 'neo4j', r.pid)
    statement = next(q for q in r.queries if str(q).startswith('INSERT INTO foreshadows'))
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert 'WHERE foreshadows.project_id =' in sql and 'foreshadows.source =' in sql
    assignment = sql.split('DO UPDATE SET ', 1)[1].split(' WHERE ', 1)[0]
    assert 'project_id =' not in assignment and 'source =' not in assignment
    assert ' AS JSONB)' in sql


@pytest.mark.asyncio
async def test_late_batch_collision_rolls_back_earlier_new_graph_rows(projection):
    r = projection
    batch = [incoming(r, id=uuid4()) for _ in range(500)] + [incoming(r)]
    with pytest.raises(ForeshadowProjectionConflict):
        with r.session.begin(): await project_foreshadow_rows(r.db, r.pid, batch)
    assert r.session.execute(select(Foreshadow.id)).scalars().all() == [r.row['id']]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['duplicate_id', 'wrong_project', 'wrong_source'])
async def test_projection_rejects_invalid_input_before_first_batch(projection, case):
    r = projection
    row = incoming(r)
    rows = [row, row] if case == 'duplicate_id' else [{**row,
        ('project_id' if case == 'wrong_project' else 'source'): str(uuid4()) if case == 'wrong_project' else 'lifecycle'}]
    r.queries.clear()
    with pytest.raises(ForeshadowProjectionConflict):
        with r.session.begin(): await project_foreshadow_rows(r.db, r.pid, rows)
    assert r.queries == []
