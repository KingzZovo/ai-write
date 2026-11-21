"""Offline API mutation checks; PostgreSQL locking still needs integration validation."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.api.outlines import OutlineExpectation, OutlineUpdate, confirm_outline, update_outline


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['confirm', 'edit'])
@pytest.mark.parametrize('change', ['unchanged', 'changed', 'missing', 'wrong_project', 'legacy'])
async def test_outline_snapshot_guard(operation, change):
    project_id, outline_id = uuid4(), uuid4()
    expected = {'raw_text': '作者读到的书纲', 'volume_plan': [{'idx': 1}]}
    row = SimpleNamespace(id=outline_id, project_id=project_id, level='book', parent_id=None,
        content_json=deepcopy(expected), version=2, is_confirmed=0, created_at=None)
    if change == 'changed': row.content_json['raw_text'] = '另一窗口已修改'
    if change == 'wrong_project': row.project_id = uuid4()
    before = deepcopy(row.__dict__)
    db = SimpleNamespace(scalar=AsyncMock(side_effect=['book'] + ([SimpleNamespace(deleted_at=None)] if operation == 'edit' else []) + [None if change == 'missing' else row]),
        flush=AsyncMock(), refresh=AsyncMock())
    body = {} if change == 'legacy' else {'expected_content_json': expected}
    if operation == 'confirm':
        call = confirm_outline(str(project_id), str(outline_id), body=OutlineExpectation(**body), db=db)
    else:
        call = update_outline(str(project_id), str(outline_id), body=OutlineUpdate(**body,
            content_json={**expected, 'raw_text': '当前作者新稿'}), db=db)
    if change in ('changed', 'missing', 'wrong_project'):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == (409 if change == 'changed' else 404)
        assert row.__dict__ == before
        db.flush.assert_not_awaited()
    else:
        result = await call
        db.flush.assert_awaited_once()
        assert result.is_confirmed == (1 if operation == 'confirm' else 0)
        assert result.version == (3 if operation == 'edit' else 2)
        assert result.content_json['volume_plan'] == expected['volume_plan']
    statement = db.scalar.call_args.args[0]
    assert statement._for_update_arg is not None
    assert statement.get_execution_options()['populate_existing'] is True


@pytest.mark.asyncio
async def test_legacy_null_outline_can_be_edited_with_empty_snapshot():
    pid, oid = uuid4(), uuid4()
    row = SimpleNamespace(id=oid, project_id=pid, level='book', parent_id=None,
        content_json=None, version=1, is_confirmed=0, created_at=None)
    db = SimpleNamespace(scalar=AsyncMock(side_effect=['book', SimpleNamespace(deleted_at=None), row]),
        flush=AsyncMock(), refresh=AsyncMock())
    result = await update_outline(str(pid), str(oid), OutlineUpdate(expected_content_json={},
        content_json={'raw_text': '作者第一稿'}), db=db)
    assert result.content_json == {'raw_text': '作者第一稿'}
    assert result.version == 2
