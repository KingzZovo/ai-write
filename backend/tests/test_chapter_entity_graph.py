"""Real Neo4j integration, isolated by a random project in an explicit test server."""
import os
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
pytestmark = pytest.mark.external_services
import pytest_asyncio
from neo4j import AsyncGraphDatabase
from app.services.entity_timeline import EntityTimelineService
from app.services.chapter_entity_extraction import FIELDS, EntityExtractionError

@pytest_asyncio.fixture
async def graph():
    uri = os.environ.get('ENTITY_TEST_NEO4J_URI')
    if not uri:
        pytest.skip('requires explicit disposable ENTITY_TEST_NEO4J_URI')
    if uri != 'bolt://127.0.0.1:57687':
        pytest.fail('refusing non-test Neo4j')
    driver = AsyncGraphDatabase.driver(uri, auth=('neo4j', 'aiwrite_test_only'))
    project = 'entity-test-' + uuid4().hex
    try:
        await driver.verify_connectivity()
        yield driver, project
    finally:
        async with driver.session() as session:
            await session.run('MATCH (c:Character {project_id:$pid}) OPTIONAL MATCH (c)-[:HAS_STATE]->(s:CharacterState) DETACH DELETE s,c', pid=project)
            await session.run('MATCH (n {project_id:$pid}) DETACH DELETE n', pid=project)
        await driver.close()

@pytest.mark.asyncio
async def test_complete_payload_writes_at_actual_chapter_without_prestory_leak(graph, monkeypatch):
    driver, project = graph
    service = EntityTimelineService(driver, strict_writes=True)
    await service.initialize_graph(project)
    source = '江枫走进酒馆。他与店主结为同盟。'
    data = {key: [] for key in FIELDS}
    data['new_characters'] = [{'name':'江枫', 'state':{'状态':'在酒馆'}, 'evidence':'江枫走进酒馆。'}]
    data['new_relationships'] = [{'char1':'江枫', 'char2':'店主', 'type':'同盟', 'evidence':'他与店主结为同盟。'}]
    data['location_changes'] = [{'character':'江枫', 'location':'酒馆', 'evidence':'江枫走进酒馆。'}]
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', AsyncMock(return_value=SimpleNamespace(text=json.dumps(data,ensure_ascii=False))))
    await service.extract_and_update(project, 8, source)
    assert await service.get_character_state_at(project, '江枫', 7) is None
    assert (await service.get_character_state_at(project, '江枫', 8)).status == {'状态':'在酒馆'}
    assert await service.get_relationships_at(project, 7) == []
    assert len(await service.get_relationships_at(project, 8)) == 1
    async with driver.session() as session:
        row = await (await session.run('MATCH (:Character {project_id:$pid})-[r:AT_LOCATION]->(l) RETURN r.chapter_start AS start,l.name AS name',pid=project)).single()
        assert row['start'] == 8 and row['name'] == '酒馆'

@pytest.mark.asyncio
async def test_late_invalid_record_leaves_real_graph_untouched(graph, monkeypatch):
    driver, project = graph
    service = EntityTimelineService(driver, strict_writes=True)
    data = {key: [] for key in FIELDS}
    data['new_characters'] = [{'name':'江枫','state':{},'evidence':'江枫走进酒馆。'}]
    data['location_changes'] = [{'character':'江枫','location':'不存在','evidence':'编造证据'}]
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',AsyncMock(return_value=SimpleNamespace(text=json.dumps(data,ensure_ascii=False))))
    with pytest.raises(EntityExtractionError):
        await service.extract_and_update(project, 8, '江枫走进酒馆。')
    async with driver.session() as session:
        row = await (await session.run('MATCH (n {project_id:$pid}) RETURN count(n) AS count',pid=project)).single()
        assert row['count'] == 0
