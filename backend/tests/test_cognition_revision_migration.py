"""Migration preservation contracts on SQLite; real PostgreSQL remains external."""
import importlib.util
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.services.prompt_registry import BUILTIN_PROMPTS


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/a1001948_cognition_author_revisions.py'
    spec = importlib.util.spec_from_file_location('cognition_revision_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated(monkeypatch):
    m = migration()
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys = ON')
        metadata = sa.MetaData()
        projects = sa.Table('projects', metadata, sa.Column('id', sa.UUID(), primary_key=True))
        chapters = sa.Table('chapters', metadata, sa.Column('id', sa.UUID(), primary_key=True),
            sa.Column('project_id', sa.UUID(), sa.ForeignKey('projects.id', ondelete='CASCADE')))
        records = sa.Table('chapter_cognition_extractions', metadata, sa.Column('id', sa.UUID(), primary_key=True),
            sa.Column('chapter_id', sa.UUID(), sa.ForeignKey('chapters.id', ondelete='CASCADE')))
        prompts = sa.Table('prompt_assets', metadata, sa.Column('id', sa.UUID(), primary_key=True),
            sa.Column('task_type', sa.String()), sa.Column('system_prompt', sa.Text()), sa.Column('output_schema', sa.JSON()),
            sa.Column('endpoint_id', sa.String()), sa.Column('model_hint', sa.String()), sa.Column('temperature', sa.Float()))
        metadata.create_all(db)
        pid, cid, rid = uuid4(), uuid4(), uuid4()
        db.execute(projects.insert().values(id=pid))
        db.execute(chapters.insert().values(id=cid, project_id=pid))
        db.execute(records.insert().values(id=rid, chapter_id=cid))
        custom_schema = deepcopy(m.OLD_SCHEMA)
        custom_schema['description'] = '作者自定义'
        ids = [uuid4() for _ in range(5)]
        originals = [
            ('chapter_cognition', m.OLD_PROMPT, m.OLD_SCHEMA),
            ('chapter_cognition', m.OLD_PROMPT+'\n作者要求', m.OLD_SCHEMA),
            ('chapter_cognition', m.OLD_PROMPT, custom_schema),
            ('other_task', m.OLD_PROMPT, m.OLD_SCHEMA),
            ('chapter_cognition', m.OLD_PROMPT, None),
        ]
        for ident, (task, prompt, schema) in zip(ids, originals):
            db.execute(prompts.insert().values(id=ident, task_type=task, system_prompt=prompt,
                output_schema=schema, endpoint_id='existing-cpa', model_hint='configured-model', temperature=0.4))
        monkeypatch.setattr(m, 'op', Operations(MigrationContext.configure(db)))
        m.upgrade()
        yield m, db, prompts, ids, originals, pid, cid, rid
    engine.dispose()


def test_migration_upgrades_only_untouched_prompt_schema_pair(migrated):
    m, db, prompts, ids, originals, *_ = migrated
    # Reapplying just the policy update remains harmless.
    m._replace_default(db, m.OLD_PROMPT, m.OLD_SCHEMA, m.NEW_PROMPT, m.NEW_SCHEMA)
    rows = {row.id: row for row in db.execute(sa.select(prompts)).all()}
    assert rows[ids[0]].system_prompt == m.NEW_PROMPT and rows[ids[0]].output_schema == m.NEW_SCHEMA
    for ident, original in zip(ids[1:], originals[1:]):
        assert (rows[ident].task_type, rows[ident].system_prompt, rows[ident].output_schema) == original
    assert all((row.endpoint_id, row.model_hint, row.temperature) == ('existing-cpa', 'configured-model', 0.4) for row in rows.values())
    builtin = next(p for p in BUILTIN_PROMPTS if p['task_type'] == 'chapter_cognition')
    assert builtin['system_prompt'] == m.NEW_PROMPT and builtin['output_schema'] == m.NEW_SCHEMA
    assert db.execute(sa.text('SELECT source_mode, author_revision_id FROM chapter_cognition_extractions')).one() == ('auto', None)


def test_history_survives_chapter_cascade_but_project_delete_cleans_it(migrated):
    m, db, _, _, _, pid, cid, rid = migrated
    revisions = sa.Table('cognition_revisions', sa.MetaData(), autoload_with=db)
    # SQLite reflects the literal UUID type as NUMERIC; bind UUIDs with the
    # same explicit SQLAlchemy type used by the migration/production model.
    for name in ('id', 'project_id', 'chapter_id', 'record_id', 'previous_id'):
        revisions.c[name].type = sa.Uuid()
    db.execute(revisions.insert().values(id=uuid4(), project_id=pid, chapter_id=cid, record_id=rid,
        source_hash='a'*64, source_json={}, base_version='b'*64, mode='author', changes_json=[],
        model_snapshot_json={}, actor='作者', payload_hash='c'*64))
    with pytest.raises(RuntimeError, match='校订历史'):
        m.downgrade()
    db.execute(sa.text('DELETE FROM chapters'))
    assert db.scalar(sa.text('SELECT count(*) FROM chapter_cognition_extractions')) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(revisions)) == 1
    db.execute(sa.text('DELETE FROM projects'))
    assert db.scalar(sa.select(sa.func.count()).select_from(revisions)) == 0


def test_empty_history_downgrade_preserves_custom_prompts(migrated):
    m, db, prompts, ids, originals, *_ = migrated
    m.downgrade()
    assert 'cognition_revisions' not in sa.inspect(db).get_table_names()
    assert 'source_mode' not in {column['name'] for column in sa.inspect(db).get_columns('chapter_cognition_extractions')}
    for ident, original in zip(ids, originals):
        row = db.execute(sa.select(prompts).where(prompts.c.id == ident)).one()
        assert (row.task_type, row.system_prompt, row.output_schema) == original
