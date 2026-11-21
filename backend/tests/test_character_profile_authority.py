from unittest.mock import AsyncMock
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest

from app.services.character_profile_authority import projected_profile


@pytest.mark.parametrize('origin', [None, 'outline_extraction', 'author_card', 'unknown'])
def test_automatic_or_historical_graph_does_not_replace_author_card(origin):
    assert projected_profile({'identity': '作者确认的师父'}, {'identity': '旧图中的学徒'}, origin) is None


@pytest.mark.parametrize('incoming', [{}, {'identity': '师父', 'aliases': ['老周']}])
def test_explicit_author_edits_include_clearing_all_fields(incoming):
    result = projected_profile({'identity': '旧身份'}, incoming, 'author_edit')
    assert result == incoming and result is not incoming


@pytest.mark.parametrize('existing', [None, {}])
def test_automatic_profile_preserves_existing_empty_card(existing):
    assert projected_profile(existing, {'identity': '学徒'}, 'outline_extraction') is None


@pytest.mark.parametrize('incoming', [None, 'invalid', [], 1])
def test_malformed_profiles_never_erase_cards(incoming):
    assert projected_profile({'identity': '师父'}, incoming, 'author_edit') is None


def test_outline_import_can_preserve_even_an_author_graph_profile():
    assert projected_profile({'identity': '作者卡片'}, {'identity': '图档案'}, 'author_edit',
        preserve_existing=True) is None


@pytest.mark.asyncio
async def test_actual_author_save_marks_graph_profile_as_author_edit(monkeypatch):
    from app.api import neo4j_settings as api
    from tests.test_character_profile_write import ProfileDriver
    from tests.support.character_project import CharacterProjectDB
    pid = str(uuid4())
    driver = ProfileDriver()
    materialize = AsyncMock(return_value={})
    monkeypatch.setattr(api, '_materialize_entities_to_postgres', materialize)
    await api.upsert_character(pid, api.Neo4jCharacterUpsertRequest(name='周砚', profile_json={}, expected_profile_json=None),
                               driver, CharacterProjectDB(pid))
    query, params = next((q, p) for q, p in driver.calls if 'SET c.profile_json=' in q)
    assert "c.profile_origin='author_edit'" in query
    assert params['profile'] == '{}'
    materialize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('origin,incoming,initial,expected', [
    ('outline_extraction', {'identity': '旧身份'}, {'identity': '作者身份'}, {'identity': '作者身份'}),
    (None, {'identity': '旧身份'}, {}, {}),
    ('author_edit', {}, {'identity': '旧身份'}, {}),
    ('author_edit', {'identity': '作者身份'}, {'identity': '旧身份'}, {'identity': '作者身份'}),
    ('outline_extraction', {'identity': '新人物'}, None, {'identity': '新人物'}),
])
async def test_real_materializer_preserves_automatic_edits_and_projects_author_clear(
    monkeypatch, origin, incoming, initial, expected,
):
    from app.models.project import Character
    from app.tasks.entity_tasks import _materialize_entities_to_postgres
    pid = uuid4()
    chars = [] if initial is None else [Character(id=uuid4(), project_id=pid, name='周砚', profile_json=initial)]
    class GraphResult:
        def __init__(self, rows): self.rows = rows
        async def data(self): return self.rows
        def __aiter__(self):
            async def rows():
                for row in self.rows: yield row
            return rows()
    async def run(query, **params):
        rows = [{'name': '周砚', 'profile_json': json.dumps(incoming), 'profile_origin': origin}] if 'c.profile_json AS' in query else []
        return GraphResult(rows)
    @asynccontextmanager
    async def graph_session(): yield SimpleNamespace(run=run)
    def result(rows):
        return SimpleNamespace(all=lambda: rows, scalars=lambda: SimpleNamespace(all=lambda: rows))
    async def execute(query):
        columns = query.column_descriptions
        if len(columns) == 1 and columns[0]['expr'] is Character: return result(chars)
        if len(columns) == 2 and columns[0]['entity'] is Character:
            return result([(c.id, c.name) for c in chars])
        return result([])
    def add(row):
        row.id = uuid4()
        chars.append(row)
    db = SimpleNamespace(execute=execute, scalar=AsyncMock(return_value=True), add=add,
        flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    @asynccontextmanager
    async def sessions(): yield db
    monkeypatch.setattr('app.db.session.async_session_factory', sessions)
    counts = await _materialize_entities_to_postgres(project_id=str(pid), chapter_idx=0,
        caller='test.profile-authority', strict=True, graph_driver=SimpleNamespace(session=graph_session))
    assert len(chars) == 1 and chars[0].profile_json == expected
    assert counts['chars_created'] == (1 if initial is None else 0)
    db.commit.assert_awaited_once()
