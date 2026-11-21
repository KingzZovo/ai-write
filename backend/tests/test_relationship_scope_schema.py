"""Exercise new migration semantics and prompt seeding on disposable SQLite."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset
from app.services import prompt_registry, prompt_cache
from app.services.relationship_scope import SYSTEM_PROMPT, OUTPUT_SCHEMA
from tests.test_pipeline_control import runtime_db


def load_migration():
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/a1001945_relationship_scope_stages.py'
    spec = importlib.util.spec_from_file_location('relationship_scope_stage_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_migration_preserves_legacy_payload_and_refuses_to_drop_unfinished_reviews():
    engine = create_engine('sqlite://')
    legacy = json.dumps({'changed_relationships': [{'type': '朋友', 'evidence': '完整原句。'}]}, ensure_ascii=False)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE chapter_entity_extractions (id TEXT PRIMARY KEY, payload_json JSON, payload_hash TEXT)'))
        conn.execute(text('INSERT INTO chapter_entity_extractions VALUES (:id,:data,:hash)'), dict(id='old', data=legacy, hash='legacy-digest'))
        migration = load_migration()
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        row = conn.execute(text('SELECT * FROM chapter_entity_extractions')).mappings().one()
        assert row['payload_stage'] == 'complete' and row['scope_json'] is None
        assert row['payload_json'] == legacy and row['payload_hash'] == 'legacy-digest'
        conn.execute(text("UPDATE chapter_entity_extractions SET payload_stage='scope_pending',scope_json='{}'"))
        with pytest.raises(RuntimeError, match='Preserve unfinished'): migration.downgrade()
        assert 'scope_json' in {column['name'] for column in inspect(conn).get_columns('chapter_entity_extractions')}
        conn.execute(text("UPDATE chapter_entity_extractions SET payload_stage='complete',scope_json=NULL"))
        migration.downgrade()
        assert {column['name'] for column in inspect(conn).get_columns('chapter_entity_extractions')} == {'id', 'payload_json', 'payload_hash'}
        assert conn.execute(text('SELECT payload_json FROM chapter_entity_extractions')).scalar_one() == legacy
        migration.upgrade()
        conn.execute(text("INSERT INTO chapter_entity_extractions (id) VALUES ('new')"))
        assert conn.execute(text("SELECT payload_stage FROM chapter_entity_extractions WHERE id='new'")).scalar_one() == 'complete'
    engine.dispose()


@pytest.mark.asyncio
async def test_real_pg_stage_migration_preserves_source_and_payload_defaults(runtime_db):
    from app.models.entity_extraction import ChapterEntityExtraction as Record
    from app.services import entity_projection as ep
    from app.services.entity_sources import digest
    from tests.test_entity_projection import prepare, source, payload
    r = runtime_db
    await prepare(r)
    src = await source(r)
    rid = await ep.ensure_record(r.sessions, src)
    legacy = payload()
    async with r.sessions() as db:
        row = await db.get(Record, rid)
        row.payload_json, row.payload_hash = legacy, digest([src.source_hash, legacy])
        await db.commit()
    migration = load_migration()
    async with r.engine.begin() as conn:
        def roundtrip(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            migration.downgrade(); migration.upgrade()
        await conn.run_sync(roundtrip)
    async with r.sessions() as db:
        row = await db.get(Record, rid)
        assert row.payload_stage == 'complete' and row.scope_json is None
        assert row.payload_json == legacy and row.payload_hash == digest([src.source_hash, legacy])
        assert row.source_hash == src.source_hash and row.source_json == src.receipt


@pytest.mark.asyncio
async def test_scope_seed_inherits_the_configured_route_and_preserves_author_customizations():
    engine = create_engine('sqlite://')
    LLMEndpoint.__table__.create(engine)
    PromptAsset.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        endpoint = LLMEndpoint(name='本地测试端点', provider_type='openai_compatible', default_model='configured-model')
        session.add(endpoint); session.flush()
        parent = PromptAsset(task_type='chapter_entities', name='本章提取', system_prompt='已有五数组任务',
            endpoint_id=endpoint.id, model_name='configured-model', temperature=0.2, max_tokens=12345, model_tier='flagship')
        session.add(parent); session.commit()
        async def scalar(statement): return session.scalar(statement)
        async def execute(statement): return session.execute(statement)
        async def flush(): session.flush()
        registry = prompt_registry.PromptRegistry(NS(scalar=scalar, execute=execute, flush=flush, add=session.add))
        await registry.seed_builtins(); session.commit()
        child = session.scalar(select(PromptAsset).where(PromptAsset.task_type == 'relationship_scope'))
        assert child.system_prompt == SYSTEM_PROMPT and child.output_schema == OUTPUT_SCHEMA
        for field in ('endpoint_id', 'model_name', 'temperature', 'max_tokens', 'model_tier'):
            assert getattr(child, field) == getattr(parent, field)
        assert child.system_prompt != parent.system_prompt
        child.system_prompt, child.model_name, child.temperature = '作者定制的关系核查要求', 'author-model', 0.3
        session.commit()
        assert await registry.seed_builtins() == 0
        assert (child.system_prompt, child.model_name, child.temperature) == ('作者定制的关系核查要求', 'author-model', 0.3)
    engine.dispose()


@pytest.mark.asyncio
async def test_missing_scope_asset_never_falls_back_to_the_five_array_prompt(monkeypatch):
    snapshot = AsyncMock(return_value=None)
    monkeypatch.setattr(prompt_cache, 'get_snapshot', snapshot)
    with pytest.raises(ValueError, match='relationship_scope'):
        await prompt_registry._resolve_route_and_tier_cached('relationship_scope', object())
    snapshot.assert_awaited_once()
    assert snapshot.await_args.args[0] == 'relationship_scope'


@pytest.mark.asyncio
async def test_actual_registered_scope_prompt_is_budgeted_before_provider_io(monkeypatch):
    db = object()
    snapshot = NS(id=uuid4(), task_type='relationship_scope', name='核查', endpoint_id=uuid4(), model_name='test',
                  system_prompt='完整的注册提示词。' * 100, mode='structured', temperature=0.2, max_tokens=4096, model_tier=None)
    model = AsyncMock(side_effect=AssertionError('oversized input must not call provider'))
    monkeypatch.setattr(prompt_cache, 'get_snapshot', AsyncMock(return_value=snapshot))
    monkeypatch.setattr(prompt_registry, 'get_model_router_async', AsyncMock(return_value=NS(generate_with_tier_fallback=model)))
    with pytest.raises(ValueError, match='prompt_input_budget_exceeded'):
        await prompt_registry.run_text_prompt('relationship_scope', '完整章节正文。', db, input_char_limit=100)
    model.assert_not_awaited()
