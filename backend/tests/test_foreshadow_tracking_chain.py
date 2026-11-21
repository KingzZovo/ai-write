"""Graph projection must preserve locally owned and unknown-source records.

Plan rendering and prose-source chronology are covered by
test_foreshadow_chronology.py. Automatic outline-to-fact writes were removed.
"""
from __future__ import annotations

import uuid

import pytest
from unittest.mock import AsyncMock

from sqlalchemy.dialects import postgresql


PID = "f14712d6-6dc6-4cfb-b05f-e107fa02b63d"


class _FakeResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self


# ---------------------------------------------------------------------------
# Defect 2: materialize deletion sync must not wipe PG-only foreshadows
# ---------------------------------------------------------------------------

FID_NEO = str(uuid.uuid4())        # in Neo4j and PG (source='neo4j')
FID_NEO_STALE = str(uuid.uuid4())  # deleted from Neo4j, PG source='neo4j'
FID_LIFECYCLE = str(uuid.uuid4())  # PG-only (source='lifecycle'), never in Neo4j
FID_LEGACY = str(uuid.uuid4())     # PG-only legacy row (source NULL)


class _FakeNeoResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        return self._gen()

    async def data(self):
        return self._rows

    async def _gen(self):
        for r in self._rows:
            yield r


class _FakeNeoSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **kwargs):
        if "Foreshadow" in query:
            return _FakeNeoResult([
                {
                    "id": FID_NEO,
                    "type": "伏笔",
                    "description": "青铜齿轮印记",
                    "planted": 3,
                    "conds": "[]",
                    "blueprint": "{}",
                    "prox": 0.0,
                    "status": "planted",
                    "resolved": None,
                }
            ])
        return _FakeNeoResult([])


class _FakeNeoDriver:
    def session(self, *args, **kwargs):
        return _FakeNeoSession()


class _MaterializeFakeDB:
    """In-memory foreshadows table that honors the source filter of SELECTs.

    With the unfixed code (no source filter in the deletion-sync SELECT) it
    returns ALL rows, reproducing the wipe; with the fix it returns only
    source='neo4j' rows.
    """

    def __init__(self, fs_table):
        self.fs_table = list(fs_table)  # dicts: {"id", "source"}
        self.deleted_fs_ids: list[str] = []
        self.fs_upsert_params: dict = {}
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def scalar(self, query, params):
        assert 'pg_try_advisory_xact_lock' in str(query)
        return True

    async def execute(self, stmt, params=None):
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        if "INSERT INTO foreshadows" in sql:
            self.fs_upsert_params = dict(compiled.params)
            return _FakeResult(rowcount=1)
        if "DELETE FROM foreshadows" in sql:
            ids: list[str] = []
            for v in compiled.params.values():
                if isinstance(v, (list, tuple)):
                    ids.extend(str(x) for x in v)
            self.deleted_fs_ids.extend(ids)
            self.fs_table = [r for r in self.fs_table if r["id"] not in ids]
            return _FakeResult(rowcount=len(ids))
        if sql.lstrip().upper().startswith("SELECT") and "FROM foreshadows" in sql:
            if set(stmt.selected_columns.keys()) == {'id', 'project_id', 'source'}:
                # The bulk projector now verifies ownership after upsert.
                # Return the actual owners of only the requested IDs.
                from types import SimpleNamespace
                ids = {str(fid) for value in compiled.params.values() if isinstance(value, list) for fid in value}
                return _FakeResult([SimpleNamespace(id=uuid.UUID(r['id']), project_id=uuid.UUID(PID), source=r['source'])
                                    for r in self.fs_table if r['id'] in ids])
            if "foreshadows.source" in sql:
                rows = [r for r in self.fs_table if r["source"] == "neo4j"]
            else:
                rows = list(self.fs_table)
            return _FakeResult([(r["id"],) for r in rows])
        # Everything else (characters, relationships, ...) is empty.
        return _FakeResult()


@pytest.fixture
def _fake_env(monkeypatch):
    fs_table = [
        {"id": FID_NEO, "source": "neo4j"},
        {"id": FID_NEO_STALE, "source": "neo4j"},
        {"id": FID_LIFECYCLE, "source": "lifecycle"},
        {"id": FID_LEGACY, "source": None},
    ]
    db = _MaterializeFakeDB(fs_table)

    from app.db import neo4j as neo4j_mod
    from app.db import session as session_mod

    monkeypatch.setattr(neo4j_mod, "init_neo4j", AsyncMock())
    monkeypatch.setattr(neo4j_mod, "_driver", _FakeNeoDriver())
    monkeypatch.setattr(session_mod, "async_session_factory", lambda: db)
    return db


@pytest.mark.asyncio
async def test_materialize_deletion_sync_only_touches_neo4j_rows(_fake_env):
    from app.tasks.entity_tasks import _materialize_entities_to_postgres

    db = _fake_env
    result = await _materialize_entities_to_postgres(
        project_id=PID, chapter_idx=5, caller="test"
    )

    # Sanity: success path reached (Neo4j foreshadow was seen + upserted).
    assert result["foreshadows_seen"] == 1
    assert db.committed is True

    # The genuinely stale Neo4j-origin row IS reconciled away...
    assert FID_NEO_STALE in db.deleted_fs_ids
    # ...but PG-only rows (lifecycle + legacy NULL) must survive extraction.
    assert FID_LIFECYCLE not in db.deleted_fs_ids
    assert FID_LEGACY not in db.deleted_fs_ids
    # And the row still present in Neo4j is kept.
    assert FID_NEO not in db.deleted_fs_ids

    surviving = {r["id"] for r in db.fs_table}
    assert surviving == {FID_NEO, FID_LIFECYCLE, FID_LEGACY}


@pytest.mark.asyncio
async def test_materialize_upsert_stamps_source_neo4j(_fake_env):
    from app.tasks.entity_tasks import _materialize_entities_to_postgres

    db = _fake_env
    await _materialize_entities_to_postgres(
        project_id=PID, chapter_idx=5, caller="test"
    )
    # The upsert must write source='neo4j' so future deletion syncs can
    # identify rows that originated from Neo4j.
    assert "neo4j" in db.fs_upsert_params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
async def test_materialize_commit_failure_never_reports_created_rows(_fake_env, strict):
    from app.tasks.entity_tasks import _materialize_entities_to_postgres
    db = _fake_env
    db.commit = AsyncMock(side_effect=RuntimeError('commit failure'))
    db.rollback = AsyncMock()
    if strict:
        with pytest.raises(RuntimeError, match='commit failure'):
            await _materialize_entities_to_postgres(project_id=PID, chapter_idx=5, caller='test', strict=True)
    else:
        result = await _materialize_entities_to_postgres(project_id=PID, chapter_idx=5, caller='test')
        assert result['foreshadows_upserted'] == result['foreshadows_seen'] == 0
    db.rollback.assert_awaited_once()
