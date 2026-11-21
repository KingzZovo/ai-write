from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.models.project import Project, Outline
from app.models.entity_extraction import OutlineSettingsExtraction as Receipt
from app.services.outline_settings_source import read_settings_source, require_current_settings_source, SettingsSourceError


def source_db():
    project = Project(id=uuid4(), title='修补以后', settings_json={})
    outline = Outline(id=uuid4(), project_id=project.id, level='book', version=1, is_confirmed=1,
        content_json={'raw_text': '母亲还活着，林禾给母亲打电话。'})
    receipts = []
    async def scalar(query):
        model = query.column_descriptions[0]['entity']
        if model is Project: return project
        if model is Outline: return outline
        if model is Receipt: return receipts[0] if receipts else None
    def add(row):
        row.id = uuid4()
        receipts.append(row)
    return project, outline, SimpleNamespace(scalar=AsyncMock(side_effect=scalar), rollback=AsyncMock(),
        add=Mock(side_effect=add), flush=AsyncMock(), commit=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'text', 'version', 'confirmation', 'project', 'deleted'])
async def test_source_is_rechecked_under_locks_after_extraction(change):
    project, outline, db = source_db()
    source = await read_settings_source(db, project.id, outline.id)
    if change == 'text': outline.content_json['raw_text'] = '作者又改了书纲'
    if change == 'version': outline.version += 1
    if change == 'confirmation': outline.is_confirmed = 0
    if change == 'project': project.settings_json['new_setting'] = '作者设定'
    if change == 'deleted': project.deleted_at = 'deleted'
    if change == 'none': await require_current_settings_source(db, source)
    else:
        with pytest.raises(SettingsSourceError, match='未写入'): await require_current_settings_source(db, source)
    assert source.text == '母亲还活着，林禾给母亲打电话。'
    for call in db.scalar.call_args_list[2:]:
        assert call.args[0]._for_update_arg is not None
        assert call.args[0].get_execution_options()['populate_existing'] is True
        if call.args[0].column_descriptions[0]['entity'] is Project:
            from sqlalchemy.dialects import postgresql
            assert 'FOR NO KEY UPDATE' in str(call.args[0].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['text', 'project'])
async def test_real_api_rejects_stale_result_before_any_graph_or_materialization(monkeypatch, change):
    from app.api import outlines as api
    project, outline, db = source_db()
    async def extract(text, **kwargs):
        db.rollback.assert_awaited_once()
        assert text == outline.content_json['raw_text']
        if change == 'text': outline.content_json['raw_text'] = '新稿明确人物仍在世'
        else: project.title = '新标题'
        return {'characters': [{'name': '过期人物'}], 'world_rules': [], 'relationships': []}
    model = AsyncMock(side_effect=extract)
    graph = AsyncMock()
    projection = AsyncMock()
    monkeypatch.setattr(api, 'extract_settings_from_outline', model)
    monkeypatch.setattr(api, 'init_neo4j', graph)
    monkeypatch.setattr(api, '_materialize_entities_to_postgres', projection)
    with pytest.raises(HTTPException) as exc:
        await api.extract_settings(str(project.id), str(outline.id), db)
    assert exc.value.status_code == 409
    graph.assert_not_awaited(); projection.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['volume', 'blank', 'missing'])
async def test_invalid_source_rejected_before_model_work(monkeypatch, kind):
    from app.api import outlines as api
    project, outline, db = source_db()
    if kind == 'volume': outline.level = 'volume'
    if kind == 'blank': outline.content_json = {'raw_text': ' '}
    if kind == 'missing': db.scalar.side_effect = None; db.scalar.return_value = None
    model = AsyncMock()
    monkeypatch.setattr(api, 'extract_settings_from_outline', model)
    with pytest.raises(HTTPException): await api.extract_settings(str(project.id), str(outline.id), db)
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_source_can_finish_extraction(monkeypatch):
    from app.api import outlines as api
    project, outline, db = source_db()
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: [])
    db.execute = AsyncMock(return_value=empty)
    db.flush = AsyncMock()
    monkeypatch.setattr(api, 'extract_settings_from_outline', AsyncMock(return_value={
        'characters': [], 'world_rules': [], 'relationships': []}))
    async def init_graph():
        # No graph access before source recheck and source row locks.
        db.commit.assert_awaited_once()  # Payload persisted before graph work.
        assert db.scalar.call_args_list[-1].args[0]._for_update_arg is not None
    monkeypatch.setattr(api, 'init_neo4j', init_graph)
    monkeypatch.setattr(api._neo4j_mod, '_driver', object())
    monkeypatch.setattr('app.services.outline_settings_graph.write_settings_graph', AsyncMock())
    materialize = AsyncMock(return_value={})
    monkeypatch.setattr(api, '_materialize_entities_to_postgres', materialize)
    result = await api.extract_settings(str(project.id), str(outline.id), db)
    assert result.characters_created == result.world_rules_created == result.relationships_created == 0
    materialize.assert_awaited_once()
