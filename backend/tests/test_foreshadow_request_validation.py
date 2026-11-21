from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from app.api import foreshadows as api


@pytest.mark.parametrize('patch', [
    {'status': 'unknown'}, {'status': None}, {'description': None},
    {'description': '  \n'}, {'type': ''}, {'type': None}, {'planted_chapter': None},
    {'planted_chapter': True}, {'planted_chapter': '2'}, {'resolved_chapter': -1},
    {'status': 'resolved'}, {'status': 'resolved', 'resolved_chapter': None},
    {'resolved_chapter': 20}, {'resolved_chapter': None},
    {'status': 'planted', 'resolved_chapter': 20},
])
@pytest.mark.asyncio
async def test_invalid_patch_returns_422_before_any_store_operation(patch):
    app = FastAPI()
    app.include_router(api.router)
    db = NS(execute=AsyncMock(), flush=AsyncMock())
    graph = MagicMock()
    async def provide_db(): return db
    async def provide_graph(): return graph
    app.dependency_overrides[api.get_db] = provide_db
    app.dependency_overrides[api.get_foreshadow_driver] = provide_graph
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.put(f'/api/projects/{uuid4()}/foreshadows/{uuid4()}', json=patch,
            headers={'Idempotency-Key': str(uuid4()), 'If-Match': 'a' * 64})
    assert response.status_code == 422
    db.execute.assert_not_called()
    db.flush.assert_not_called()
    graph.session.assert_not_called()


def test_omission_and_optional_clearing_preserve_patch_semantics():
    assert api.ForeshadowUpdateRequest().model_dump(exclude_unset=True) == {}
    patch = api.ForeshadowUpdateRequest(resolution_blueprint=None, resolve_conditions=[])
    assert patch.model_dump(exclude_unset=True) == {
        'resolution_blueprint': None, 'resolve_conditions': [],
    }
    for status in ('planned', 'pending', 'planted', 'ripening', 'ready'):
        assert api.ForeshadowUpdateRequest(status=status).model_dump(exclude_unset=True) == {
            'status': status, 'resolved_chapter': None,
        }
    assert api.ForeshadowUpdateRequest(status='resolved', resolved_chapter=0).resolved_chapter == 0
    assert api.ForeshadowUpdateRequest(description='  作者保留空格。 ').description == '  作者保留空格。 '


@pytest.mark.parametrize('data', [{'description': ' '}, {'type': '\n'}, {'planted_chapter': True}])
def test_create_rejects_empty_required_text_and_boolean_chapter(data):
    with pytest.raises(ValidationError):
        api.ForeshadowCreateRequest(**({'type': '伏笔', 'description': '蓝色印记', 'planted_chapter': 1} | data))


@pytest.mark.parametrize('chapter', [True, -1, 1.5, '2'])
def test_resolution_requires_nonnegative_integer_chapter(chapter):
    with pytest.raises(ValidationError):
        api.ForeshadowResolveRequest(resolved_chapter=chapter)


@pytest.mark.parametrize('chapter', [2**31, 2**63, 2**100])
def test_chapter_index_cannot_commit_to_graph_when_it_exceeds_the_sql_integer_column(chapter):
    with pytest.raises(ValidationError):
        api.ForeshadowCreateRequest(type='mystery', description='线索', planted_chapter=chapter)
    with pytest.raises(ValidationError):
        api.ForeshadowUpdateRequest(planted_chapter=chapter)
    with pytest.raises(ValidationError):
        api.ForeshadowResolveRequest(resolved_chapter=chapter)
