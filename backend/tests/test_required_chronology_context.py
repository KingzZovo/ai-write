from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from app.services.context_pack import ContextPackBuilder
from app.services.context_errors import RequiredContextUnavailable


@pytest.mark.asyncio
async def test_chronology_volume_lookup_is_scoped_to_project():
    from uuid import uuid4
    project_id, volume_id = uuid4(), uuid4()
    db = NS(execute=AsyncMock(return_value=NS(scalar_one_or_none=lambda: None)))
    with pytest.raises(RequiredContextUnavailable, match='persisted_chapter_chronology_unavailable'):
        await ContextPackBuilder(db=db)._resolve_global_chapter_idx(project_id, volume_id, 3)
    query = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert 'volumes.project_id =' in str(query)
    assert project_id in query.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, 0, -1, True, '13'])
async def test_invalid_global_result_is_not_replaced_with_local_chapter(monkeypatch, value):
    monkeypatch.setattr('app.services.memory_chronology.resolve_history_boundary', AsyncMock(return_value=value))
    with pytest.raises(RequiredContextUnavailable, match='persisted_chapter_chronology_unavailable'):
        await ContextPackBuilder(db=NS())._resolve_global_chapter_idx('p', 'v', 3)


@pytest.mark.asyncio
async def test_build_stops_before_any_history_or_fact_read_when_resolver_fails(monkeypatch):
    from app.services import ctxpack_cache, memory_chronology
    monkeypatch.setattr(ctxpack_cache, 'is_invalid', AsyncMock(return_value=False))
    history = AsyncMock()
    monkeypatch.setattr(memory_chronology, 'resolve_history_boundary', history)
    builder = ContextPackBuilder(db=NS())
    failure = RuntimeError('private database details')
    monkeypatch.setattr(builder, '_resolve_global_chapter_idx', AsyncMock(side_effect=failure))
    proximity, facts = AsyncMock(), AsyncMock()
    monkeypatch.setattr(builder, '_build_proximity', proximity)
    monkeypatch.setattr(builder, '_build_facts', facts)
    with pytest.raises(RequiredContextUnavailable) as caught:
        await builder.build('p', 'v', 3)
    assert caught.value.__cause__ is failure
    history.assert_not_awaited()
    proximity.assert_not_awaited()
    facts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('persisted', [None, 0, -1, True, '31', 31])
async def test_build_uses_one_persisted_index_for_all_fact_readers(monkeypatch, persisted):
    from app.services import context_pack as module, ctxpack_cache, memory_chronology
    monkeypatch.setattr(ctxpack_cache, 'is_invalid', AsyncMock(return_value=False))
    monkeypatch.setattr(memory_chronology, 'resolve_history_boundary', AsyncMock(return_value=persisted))
    monkeypatch.setattr(module, 'build_fact_contract', AsyncMock(return_value=NS(to_prompt=lambda: '姓名记录')))
    builder = ContextPackBuilder(db=NS())
    proximity, facts, rag = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(builder, '_build_proximity', proximity)
    monkeypatch.setattr(builder, '_build_facts', facts)
    monkeypatch.setattr(builder, '_build_rag', rag)
    if persisted != 31 or type(persisted) is not int:
        with pytest.raises(RequiredContextUnavailable, match='persisted_chapter_chronology_unavailable'):
            await builder.build('p', 'v', 3)
        proximity.assert_not_awaited()
        facts.assert_not_awaited()
        return
    pack = await builder.build('p', 'v', 3)
    assert pack.global_chapter_idx == pack.history_before_global_idx == 31
    assert facts.call_args.kwargs['global_chapter_idx'] == 31
    assert 'foreshadow_chapter_idx' not in facts.call_args.kwargs
