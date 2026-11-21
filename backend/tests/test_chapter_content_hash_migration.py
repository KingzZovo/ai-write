"""Actual PostgreSQL trigger/backfill tests; skipped before IO in offline mode."""
import hashlib
import importlib.util
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, select, text

from app.models.project import Chapter
from app.services.character_cognition import load_ledger
from app.services.cognition_validation import EvidenceProofCache
from tests.test_cognition_sources import cog_db, extract, runtime_db

pytestmark = [pytest.mark.asyncio, pytest.mark.external_services]


def migration(filename):
    path = Path(__file__).parents[1] / 'alembic' / 'versions' / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def hash_db(cog_db, monkeypatch):
    r = cog_db
    outbox = migration('a1001925_entity_source_outbox.py')
    r.migration = migration('a1001949_chapter_content_hash.py')
    async with r.engine.begin() as connection:
        r.before = dict((await connection.execute(text('SELECT * FROM chapters'))).mappings().one())
        def install(sync):
            ops = Operations(MigrationContext.configure(sync))
            outbox.op = r.migration.op = ops
            outbox.upgrade()
            # create_all already added the nullable model field; exercise the
            # actual migration from the previous schema, in this test only.
            ops.drop_column('chapters', 'content_hash')
            r.migration.upgrade()
        await connection.run_sync(install)
    monkeypatch.setattr('app.services.cognition_validation.evidence_proofs', EvidenceProofCache())
    return r


async def row(r):
    async with r.sessions() as db:
        return dict((await db.execute(text('SELECT * FROM chapters'))).mappings().one())


async def test_backfill_preserves_saved_material_and_does_not_enqueue_paid_work(hash_db):
    r = hash_db
    after = await row(r)
    assert after['content_hash'] == hashlib.sha256(r.before['content_text'].encode()).hexdigest()
    assert {k: v for k, v in after.items() if k != 'content_hash'} == {k: v for k, v in r.before.items() if k != 'content_hash'}
    async with r.sessions() as db:
        assert await db.scalar(text('SELECT count(*) FROM entity_source_outbox')) == 0
        await db.execute(text("UPDATE chapters SET content_hash = :hash"), {'hash': '0' * 64})
        await db.commit()
        assert await db.scalar(text('SELECT count(*) FROM entity_source_outbox')) == 0
    assert (await row(r))['content_hash'] == after['content_hash']


@pytest.mark.parametrize('body', ['新稿：雨停了。\n𠮷野回到门口。', '', None])
async def test_direct_sql_body_and_supplied_hash_change_atomically(hash_db, body):
    r = hash_db
    before = await row(r)
    async with r.sessions() as db:
        await db.execute(text('UPDATE chapters SET content_text = :body, content_hash = :old'),
            {'body': body, 'old': before['content_hash']})
        assert await db.scalar(select(Chapter.content_hash)) == hashlib.sha256((body or '').encode()).hexdigest()
        await db.rollback()
    assert await row(r) == before
    async with r.sessions() as db:
        await db.execute(text('UPDATE chapters SET content_text = :body, content_hash = :old'),
            {'body': body, 'old': before['content_hash']})
        await db.commit()
    assert (await row(r))['content_hash'] == hashlib.sha256((body or '').encode()).hexdigest()


async def test_metadata_only_writes_do_not_even_invoke_hash_function(hash_db):
    r = hash_db
    before = await row(r)
    async with r.engine.begin() as connection:
        await connection.execute(text('''CREATE OR REPLACE FUNCTION maintain_chapter_content_hash()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'metadata update invoked body hash trigger';
            END $$'''))
        await connection.execute(text("UPDATE chapters SET title = '改章名', summary = '新摘要', global_idx = 2"))
    assert (await row(r))['content_hash'] == before['content_hash']


async def test_actual_trigger_enables_warm_cognition_reads_and_invalidates_on_edit(hash_db, monkeypatch):
    from app.services.context_errors import RequiredContextUnavailable
    r = hash_db
    await extract(r, monkeypatch, [dict(character='沈青', still_unknown='父亲已收信', evidence='沈青不知道父亲已经收到信。')])
    async with r.sessions() as db:
        cold = await load_ledger(db, r.project, before_global_idx=2)
    reads = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith('SELECT') and 'chapters.content_text' in statement:
            reads.append(statement)
    event.listen(r.engine.sync_engine, 'before_cursor_execute', observe)
    try:
        async with r.sessions() as db:
            assert await load_ledger(db, r.project, before_global_idx=2) == cold
        assert reads == []
        async with r.sessions() as db:
            await db.execute(text("UPDATE chapters SET content_text = '沈青已经读完父亲的回信。'"))
            await db.commit()
        async with r.sessions() as db:
            with pytest.raises(RequiredContextUnavailable, match='not_ready'):
                await load_ledger(db, r.project, before_global_idx=2)
    finally:
        event.remove(r.engine.sync_engine, 'before_cursor_execute', observe)


async def test_actual_trigger_enables_entity_proofs_without_replaying_old_facts(hash_db, monkeypatch):
    from app.services.entity_sources import filter_current_entity_rows, resolve_entity_source
    r = hash_db
    monkeypatch.setattr('app.services.entity_source_validation.entity_evidence_proofs', EvidenceProofCache())
    async with r.sessions() as db:
        source = await resolve_entity_source(db, r.project, 0, r.before['id'])
    old = dict(source_chapter_id=source.chapter_id, source_hash=source.source_hash,
        global_idx=source.global_idx, excerpt=source.content)
    reads = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith('SELECT') and 'chapters.content_text' in statement:
            reads.append(statement)
    event.listen(r.engine.sync_engine, 'before_cursor_execute', observe)
    try:
        async with r.sessions() as db:
            assert await filter_current_entity_rows(db, r.project, [old], before_global_idx=2) == [old]
        assert len(reads) == 1
        reads.clear()
        async with r.sessions() as db:
            assert await filter_current_entity_rows(db, r.project, [old], before_global_idx=2) == [old]
        assert not reads
        new_body = '沈青拆开回信，得知父亲已经收信。'
        async with r.sessions() as db:
            await db.execute(text('UPDATE chapters SET content_text = :body'), {'body': new_body})
            await db.commit()
        async with r.sessions() as db:
            assert await filter_current_entity_rows(db, r.project, [old], before_global_idx=2) == []
            new = await resolve_entity_source(db, r.project, 0, r.before['id'])
        current = dict(old, source_hash=new.source_hash, excerpt=new_body)
        reads.clear()
        async with r.sessions() as db:
            assert await filter_current_entity_rows(db, r.project, [current], before_global_idx=2) == [current]
        assert len(reads) == 1
    finally:
        event.remove(r.engine.sync_engine, 'before_cursor_execute', observe)


async def test_downgrade_and_reupgrade_preserve_prose_and_other_columns(hash_db):
    r = hash_db
    before = await row(r)
    async with r.engine.begin() as connection:
        def cycle(sync):
            r.migration.op = Operations(MigrationContext.configure(sync))
            r.migration.downgrade()
            r.migration.upgrade()
        await connection.run_sync(cycle)
    assert await row(r) == before
