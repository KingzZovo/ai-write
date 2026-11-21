"""Real PostgreSQL coverage; run when the integration database is accessible."""
from copy import deepcopy

import pytest
from fastapi import HTTPException
from app.api.outlines import OutlineExpectation, OutlineUpdate, confirm_outline, update_outline
from app.models.project import Outline
from tests.test_pipeline_control import runtime_db
from tests.test_volume_creation import creation


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['confirm', 'edit'])
async def test_stale_session_cannot_confirm_or_overwrite_changed_book(creation, operation):
    r = creation
    async with r.sessions() as stale:
        cached = await stale.get(Outline, r.bid)
        expected = deepcopy(cached.content_json)
        async with r.sessions() as writer:
            await update_outline(str(r.pid), str(r.bid), OutlineUpdate(
                content_json={'raw_text': '另一窗口的新稿'}, is_confirmed=False), db=writer)
            await writer.commit()
        with pytest.raises(HTTPException) as exc:
            if operation == 'confirm':
                await confirm_outline(str(r.pid), str(r.bid),
                    body=OutlineExpectation(expected_content_json=expected), db=stale)
            else:
                await update_outline(str(r.pid), str(r.bid), OutlineUpdate(
                    expected_content_json=expected, content_json={'raw_text': '过期编辑'}), db=stale)
        assert exc.value.status_code == 409
        await stale.rollback()
    async with r.sessions() as verify:
        row = await verify.get(Outline, r.bid)
        assert row.content_json == {'raw_text': '另一窗口的新稿'}
        assert row.is_confirmed == 0


@pytest.mark.asyncio
async def test_saved_edit_can_be_confirmed_and_keeps_plan(creation):
    r = creation
    async with r.sessions() as db:
        book = await db.get(Outline, r.bid)
        original = deepcopy(book.content_json)
        saved = await update_outline(str(r.pid), str(r.bid), OutlineUpdate(
            expected_content_json=original, content_json={**original, 'raw_text': '作者修订'},
            is_confirmed=False), db=db)
        await db.commit()
    async with r.sessions() as db:
        confirmed = await confirm_outline(str(r.pid), str(r.bid),
            body=OutlineExpectation(expected_content_json=saved.content_json), db=db)
        await db.commit()
        assert confirmed.is_confirmed == 1
        assert confirmed.content_json['volume_plan'] == original['volume_plan']
