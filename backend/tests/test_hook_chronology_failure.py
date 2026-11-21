from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.services.hook_manager import HookManager
from app.services.context_errors import RequiredContextUnavailable


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, 0, -1, True, '31', 1.5])
async def test_hook_resolver_rejects_unknown_or_malformed_persisted_position(value):
    manager = HookManager(db=NS(execute=AsyncMock(return_value=NS(scalar_one_or_none=lambda: value))))
    with pytest.raises(RequiredContextUnavailable):
        await manager._resolve_global_idx(str(uuid4()), str(uuid4()), 3)
    manager._db.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RequiredContextUnavailable('unknown'), TimeoutError()])
async def test_pre_hooks_stop_before_using_unlocated_history(monkeypatch, failure):
    manager = HookManager(db=NS())
    monkeypatch.setattr(manager, '_resolve_global_idx', AsyncMock(side_effect=failure))
    character, foreshadow, outline = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, '_check_character_consistency', character)
    monkeypatch.setattr(manager, '_check_foreshadows', foreshadow)
    monkeypatch.setattr(manager, '_check_outline_alignment', outline)
    result = await manager.run_pre_hooks('p', 'v', 3, {})
    assert not result.can_proceed and result.errors
    character.assert_not_awaited()
    foreshadow.assert_not_awaited()
    outline.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RequiredContextUnavailable('unknown'), TimeoutError()])
async def test_post_hooks_keep_scoped_work_but_skip_unlocated_foreshadows(monkeypatch, failure):
    manager = HookManager(db=NS())
    monkeypatch.setattr(manager, '_resolve_global_idx', AsyncMock(side_effect=failure))
    steps = {}
    for name in ('_update_entities', '_generate_summary', '_enqueue_foreshadow_review',):
        steps[name] = AsyncMock()
        monkeypatch.setattr(manager, name, steps[name])
    await manager.run_post_hooks('p', 'v', 3, '正文')
    for name in ('_update_entities', '_generate_summary'):
        steps[name].assert_awaited_once_with('p', 'v', 3, '正文')
    for name in ('_enqueue_foreshadow_review',):
        steps[name].assert_not_awaited()


@pytest.mark.asyncio
async def test_post_hooks_queue_one_scoped_review_after_persisted_position_check(monkeypatch):
    manager = HookManager(db=NS())
    monkeypatch.setattr(manager, '_resolve_global_idx', AsyncMock(return_value=913))
    steps = {}
    for name in ('_update_entities', '_generate_summary', '_enqueue_foreshadow_review',):
        steps[name] = AsyncMock()
        monkeypatch.setattr(manager, name, steps[name])
    await manager.run_post_hooks('p', 'v', 3, '正文')
    steps['_enqueue_foreshadow_review'].assert_awaited_once_with('p', 'v', 3, '正文')


@pytest.mark.asyncio
async def test_pre_hooks_pass_persisted_position_to_character_and_foreshadow_checks(monkeypatch):
    manager = HookManager(db=NS())
    monkeypatch.setattr(manager, '_resolve_global_idx', AsyncMock(return_value=913))
    character, foreshadow = AsyncMock(return_value=[]), AsyncMock(return_value='')
    monkeypatch.setattr(manager, '_check_character_consistency', character)
    monkeypatch.setattr(manager, '_check_foreshadows', foreshadow)
    monkeypatch.setattr(manager, '_check_outline_alignment', AsyncMock(return_value=[]))
    assert (await manager.run_pre_hooks('p', 'v', 3, {})).can_proceed
    character.assert_awaited_once_with('p', 913, {})
    foreshadow.assert_awaited_once_with('p', 913)
