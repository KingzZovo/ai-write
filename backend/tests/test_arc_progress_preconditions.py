"""A text summary alone is not permission to advance a saved manuscript."""
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from app.api import arc as api
from tests.test_arc_persistence import create_arc
from tests.test_chapter_order_maintenance import book


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["chapter", "direction", "finish"])
async def test_legacy_mutation_requires_an_explicit_source_and_fixed_request(book, monkeypatch, action):
    _, _, outline, _ = await create_arc(book)
    original = outline.content_json.copy()
    model = AsyncMock(return_value=[])
    monkeypatch.setattr(api, "build_arc_completion_suggestions", model)
    with pytest.raises(HTTPException) as exc:
        if action == "chapter":
            await api.chapter_written(str(book.pid), api.ChapterWrittenBody(chapter_summary="写完了"), book.db)
        elif action == "direction":
            await api.next_direction(str(book.pid), api.NextDirectionBody(direction="继续"), book.db)
        else:
            await api.complete_arc(str(book.pid), db=book.db)
    assert exc.value.status_code == 428
    book.session.expire_all()
    assert outline.content_json == original
    model.assert_not_awaited()
