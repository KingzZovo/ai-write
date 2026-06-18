"""Regression: outline calls must use a ROUTABLE task_type.

prompt_assets defines routes for ``outline_book`` / ``outline_volume`` /
``outline_chapter`` — there is NO ``outline`` task_type. The non-staged volume
path and the chapter-outline path called ``router.generate(task_type="outline")``,
which has no DB route, so model_router fell back to the empty env provider and
the relay answered 502 "unknown provider for model" (observed on the 神裔
full-flow run, Stage 3). The call sites must pass the specific routable
task_type that matches their _log_meta.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.outline_generator import OutlineGenerator


class _Result:
    text = '{"chapter_idx": 1, "title": "t", "summary": "s"}'


def _gen_with_spy():
    gen = OutlineGenerator(project_id="p1")
    spy = AsyncMock(return_value=_Result())
    gen.router = MagicMock()
    gen.router.generate = spy
    return gen, spy


@pytest.mark.asyncio
async def test_chapter_outline_uses_routable_task_type():
    gen, spy = _gen_with_spy()
    await gen.generate_chapter_outline(
        {"title": "b"}, {"volume_idx": 1}, chapter_idx=1, stream=False,
    )
    assert spy.await_count == 1
    task_type = spy.await_args.kwargs.get("task_type")
    assert task_type == "outline_chapter", f"got {task_type!r}; 'outline' has no DB route"


@pytest.mark.asyncio
async def test_volume_outline_nonstaged_uses_routable_task_type():
    gen, spy = _gen_with_spy()
    await gen.generate_volume_outline(
        {"title": "b"}, volume_idx=1, stream=False, staged=False,
    )
    assert spy.await_count == 1
    task_type = spy.await_args.kwargs.get("task_type")
    assert task_type == "outline_volume", f"got {task_type!r}; 'outline' has no DB route"
