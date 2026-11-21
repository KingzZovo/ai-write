"""Selection uses exact directed pairs and complete verified graph history."""
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy import update

from app.models.project import Chapter
from app.services.entity_context import verified_relationship_candidates
from tests.test_character_state_context import state_db, add_state
from tests.test_relationship_context import Graph, edge, reference


@pytest.mark.asyncio
async def test_pair_selection_keeps_full_history_until_source_validation_and_folding(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    closing = await add_state(r, 901, {})
    r.session.execute(update(Chapter).where(Chapter.id == UUID(closing.chapter_id)).values(status='draft'))
    r.session.commit()
    author = edge(first, '作者设定', actor='陈川', target='沈青')
    author['origin'] = 'author'
    graph = Graph([edge(first, end=900), edge(first, '同事'),
        edge(closing, '绝交', event='change', replaces=[reference(first)]),
        edge(first, actor='沈青', target='陈川'), edge(first, target='沈夫人'), author])
    result = await verified_relationship_candidates(r.db, graph, r.pid, 902, pairs=[('陈川', '沈青')])
    assert {row['type'] for row in result} == {'同事', '朋友'}
    assert all((row['char1'], row['char2']) == ('陈川', '沈青') for row in result)
    query, params = graph.queries[0]
    assert 'UNWIND $pairs AS pair' in query and 'name:pair[0]' in query and 'name:pair[1]' in query
    assert params['pairs'] == [('陈川', '沈青')]
    assert 'e.chapter_end IS NULL' not in query and 'LIMIT' not in query


@pytest.mark.asyncio
async def test_many_selected_pairs_use_bounded_queries_with_no_global_record_limit(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    pairs = [(f'人物{i}', '沈青') for i in range(501)]
    graph = Graph([edge(first, actor=actor, target=target) for actor, target in pairs])
    result = await verified_relationship_candidates(r.db, graph, r.pid, 901, pairs=pairs + pairs[:5])
    assert len(result) == 501
    assert len(graph.queries) == 3 and [len(params['pairs']) for _, params in graph.queries] == [200, 200, 101]
    assert {(row['char1'], row['char2']) for row in result} == set(pairs)


@pytest.mark.asyncio
async def test_empty_pair_selection_does_not_read_the_whole_project():
    db, driver = MagicMock(), MagicMock()
    assert await verified_relationship_candidates(db, driver, 'unused', 901, pairs=[]) == []
    db.execute.assert_not_called(); driver.session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('pairs', [[('陈川',)], [('陈川', '')], [('陈川', 9)], ['陈川沈青']])
async def test_invalid_pairs_fail_before_graph_io(pairs):
    driver = MagicMock()
    with pytest.raises(ValueError):
        await verified_relationship_candidates(MagicMock(), driver, 'unused', 901, pairs=pairs)
    driver.session.assert_not_called()
