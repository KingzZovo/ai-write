import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.services.outline_settings_graph import settings_payload, write_settings_graph
from tests.test_outline_settings_source import source_db

PAYLOAD = {'characters': [{'name': '林禾', 'role': '学徒'}, {'name': '周砚', 'role': '模型旧设定'}],
    'world_rules': [{'category': '日常', 'rule_text': '修补不可造出缺页文字'}],
    'relationships': [{'source_name': '林禾', 'target_name': '周砚', 'rel_type': 'mentor'}]}


def test_every_character_has_a_profile_and_existing_author_cards_are_preserved():
    author = SimpleNamespace(id=uuid4(), profile_json={'role': '作者确认的师父'})
    chars, rules, rels = settings_payload(PAYLOAD, {'周砚': author})
    by_name = {item['name']: item for item in chars}
    assert json.loads(by_name['林禾']['profile_json'])['role'] == '学徒'
    assert json.loads(by_name['周砚']['profile_json']) == author.profile_json
    assert by_name['周砚']['profile_origin'] == 'author_card'
    assert by_name['周砚']['id'] == str(author.id)
    assert len(rules) == len(rels) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_at', [None, 1, 2])
async def test_graph_writes_share_one_managed_transaction(fail_at):
    calls = []
    committed = []
    transactions = 0
    async def run(query, **params):
        if len(calls) == fail_at: raise RuntimeError('graph failed')
        calls.append((query, params))
        return SimpleNamespace(consume=AsyncMock())
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def run(self, query):
            assert 'CREATE CONSTRAINT' in query
            return SimpleNamespace(consume=AsyncMock())
        async def execute_write(self, work):
            nonlocal transactions
            transactions += 1
            await work(SimpleNamespace(run=run))
            committed.extend(calls)
    source = SimpleNamespace(project_id=uuid4(), outline_id=uuid4(), digest='source')
    operation = write_settings_graph(SimpleNamespace(session=lambda: Session()), source, PAYLOAD, {})
    if fail_at is None:
        await operation
        assert len(committed) == 4
        assert 'RelationshipProjectGuard' in calls[0][0]
        assert all('ON CREATE SET' in query for query, _ in calls[1:])
        assert all(params['digest'] == 'source' for _, params in calls[1:])
    else:
        with pytest.raises(RuntimeError): await operation
        assert not committed
    assert transactions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['none', 'graph', 'projection'])
async def test_endpoint_reports_projection_counts_and_never_hides_write_failures(monkeypatch, failure):
    from app.api import outlines as api
    project, outline, db = source_db()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
    monkeypatch.setattr(api, 'extract_settings_from_outline', AsyncMock(return_value=PAYLOAD))
    monkeypatch.setattr(api, 'init_neo4j', AsyncMock())
    driver = object()
    monkeypatch.setattr(api._neo4j_mod, '_driver', driver)
    graph = AsyncMock(side_effect=RuntimeError('graph fail') if failure == 'graph' else None)
    project_graph = AsyncMock(return_value={'chars_created': 2, 'rels_created': 1, 'rules_created': 1},
        side_effect=RuntimeError('projection fail') if failure == 'projection' else None)
    monkeypatch.setattr('app.services.outline_settings_graph.write_settings_graph', graph)
    monkeypatch.setattr(api, '_materialize_entities_to_postgres', project_graph)
    if failure == 'none':
        result = await api.extract_settings(str(project.id), str(outline.id), db)
        assert result.characters_created == 2 and result.relationships_created == result.world_rules_created == 1
    else:
        with pytest.raises(HTTPException) as exc: await api.extract_settings(str(project.id), str(outline.id), db)
        assert exc.value.status_code == 503
    if failure == 'graph': project_graph.assert_not_awaited()
    else:
        assert project_graph.call_args.kwargs['strict'] is True
        assert project_graph.call_args.kwargs['preserve_existing_profiles'] is True
        assert project_graph.call_args.kwargs['graph_driver'] is driver
