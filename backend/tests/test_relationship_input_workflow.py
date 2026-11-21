"""Large books must not send unrelated relationship history to each extraction."""
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.chapter_entity_extraction import FIELDS
from tests.test_character_state_context import state_db
from tests.test_relationship_scope_workflow import workflow, run
from tests.test_relationship_context import reference


@pytest.mark.asyncio
@pytest.mark.parametrize('unrelated_count,shared_protagonist', [(0, False), (300, False), (5000, True)])
async def test_model_input_keeps_only_relationships_of_the_actual_change(workflow, monkeypatch, unrelated_count, shared_protagonist):
    r = workflow
    for i in range(unrelated_count):
        r.graph.edges.append(dict(edge_id=f'unrelated-{i}', project_id=str(r.pid),
            source='陈川' if shared_protagonist else f'旁支人物{i}', target=f'支线人物{i}', properties=dict(type='同乡', chapter_start=900,
                source_chapter_id=r.first.chapter_id, source_hash=r.first.source_hash, event_kind='new')))
    calls = []

    async def model(**kw):
        calls.append(kw)
        assert '支线人物' not in kw['user_content']
        assert r.source.content in kw['user_content']
        if kw['task_type'] == 'chapter_entities':
            data = {key: [] for key in FIELDS}
            data['changed_relationships'] = [dict(char1='陈川', char2='沈青', type='绝交', evidence=r.source.content)]
        else:
            assert kw['task_type'] == 'relationship_scope'
            data = {'reviews': [
                dict(record_id=reference(r.first, '朋友'), ended=True, evidence=r.source.content),
                dict(record_id=reference(r.first, '同事'), ended=False, evidence=''),
            ]}
        return NS(text=json.dumps(data, ensure_ascii=False), finish_reason='stop')

    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', AsyncMock(side_effect=model))
    assert (await run(r))['status'] == 'ok'
    assert [kw['task_type'] for kw in calls] == ['chapter_entities', 'relationship_scope']
    print(f'REL_INPUT_UNRELATED={unrelated_count} USER_CHARS={[len(call["user_content"]) for call in calls]}')
