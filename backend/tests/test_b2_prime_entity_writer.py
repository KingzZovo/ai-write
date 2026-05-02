"""v1.5.0 B2' (entity writer): unify Character/HAS_STATE writes on
EntityTimelineService + celery dispatch + 49 GqlStatus warning fix.

Guards against regression of:
  1. The wrong-schema MERGE (c.status / c.location flat fields) in
     hook_manager._update_entities -- those properties are never read
     and produced UnknownPropertyKey GqlStatus warnings.
  2. ChapterEvaluator-style P0: sync get_model_router() inside an async
     coroutine -> unloaded router. extract_and_update must use
     await get_model_router_async() + generate_with_tier_fallback.
  3. Idempotency: re-running entity extraction for the same
     (project_id, chapter_idx) must be a no-op.
  4. The dispatch helper must never raise (broker-down resilience).
  5. _check_character_consistency must read via EntityTimelineService
     (correct CharacterState schema), not flat c.status fields.
  6. dispatch_for_chapter resolves project_id via Volume lookup or hint.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# 1. dispatch_entity_extraction never raises when broker is unreachable
# ---------------------------------------------------------------------------


def test_dispatch_entity_extraction_swallows_broker_failure():
    from app.services import entity_dispatch

    mock_app = MagicMock()
    mock_app.send_task.side_effect = ConnectionError("broker down")

    with patch.dict(
        "sys.modules",
        {"app.tasks": MagicMock(celery_app=mock_app)},
    ):
        result = entity_dispatch.dispatch_entity_extraction(
            project_id="proj-1",
            chapter_idx=5,
            caller="test",
        )
    assert result is False  # failure surfaced as False, not exception
    mock_app.send_task.assert_called_once()
    name, kwargs_call = mock_app.send_task.call_args.args, mock_app.send_task.call_args.kwargs
    assert name[0] == "entities.extract_chapter"
    assert kwargs_call["kwargs"]["project_id"] == "proj-1"
    assert kwargs_call["kwargs"]["chapter_idx"] == 5


def test_dispatch_entity_extraction_skips_missing_ids():
    from app.services import entity_dispatch
    assert entity_dispatch.dispatch_entity_extraction(
        project_id=None, chapter_idx=1, caller="test",
    ) is False
    assert entity_dispatch.dispatch_entity_extraction(
        project_id="x", chapter_idx=None, caller="test",
    ) is False


def test_dispatch_entity_extraction_success_kwargs():
    """Successful dispatch passes structured kwargs payload."""
    from app.services import entity_dispatch

    mock_app = MagicMock()
    mock_app.send_task.return_value = MagicMock(id="task-id-1")

    with patch.dict(
        "sys.modules",
        {"app.tasks": MagicMock(celery_app=mock_app)},
    ):
        result = entity_dispatch.dispatch_entity_extraction(
            project_id="proj-2",
            chapter_idx=3,
            chapter_id="ch-abc",
            caller="unit-test",
            countdown=10,
        )
    assert result is True
    call_kwargs = mock_app.send_task.call_args.kwargs
    assert call_kwargs["countdown"] == 10
    assert call_kwargs["kwargs"]["chapter_id"] == "ch-abc"
    assert call_kwargs["kwargs"]["caller"] == "unit-test"


# ---------------------------------------------------------------------------
# 2. dispatch_for_chapter resolves project_id via Volume or hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_for_chapter_uses_hint_no_db_lookup():
    from app.services import entity_dispatch

    chapter = MagicMock()
    chapter.chapter_idx = 7
    chapter.id = "ch-id"
    chapter.volume_id = "vol-1"

    db = MagicMock()
    db.get = AsyncMock()  # should never be called when hint is provided

    with patch.object(
        entity_dispatch, "dispatch_entity_extraction", return_value=True
    ) as mock_dispatch:
        ok = await entity_dispatch.dispatch_for_chapter(
            chapter, db,
            caller="unit-test",
            project_id_hint="proj-hint",
        )
    assert ok is True
    db.get.assert_not_called()
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["project_id"] == "proj-hint"
    assert mock_dispatch.call_args.kwargs["chapter_idx"] == 7


@pytest.mark.asyncio
async def test_dispatch_for_chapter_resolves_via_volume():
    from app.services import entity_dispatch

    chapter = MagicMock()
    chapter.chapter_idx = 4
    chapter.id = "ch-id"
    chapter.volume_id = "vol-2"

    volume = MagicMock()
    volume.project_id = "proj-from-volume"

    db = MagicMock()
    db.get = AsyncMock(return_value=volume)

    with patch.object(
        entity_dispatch, "dispatch_entity_extraction", return_value=True
    ) as mock_dispatch:
        ok = await entity_dispatch.dispatch_for_chapter(
            chapter, db,
            caller="unit-test",
        )
    assert ok is True
    db.get.assert_awaited_once()
    assert mock_dispatch.call_args.kwargs["project_id"] == "proj-from-volume"


# ---------------------------------------------------------------------------
# 3. EntityTimelineService.extract_and_update uses async router + tier fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_and_update_uses_async_tier_fallback_router():
    """Regression: B1' P0 (sync router in async path) must not return.

    Verifies that extract_and_update awaits get_model_router_async() and
    invokes generate_with_tier_fallback (NOT the legacy router.generate).
    """
    from app.services import entity_timeline

    fake_router = MagicMock()
    fake_router.generate_with_tier_fallback = AsyncMock(
        return_value=MagicMock(text='{"characters": [], "locations": [], "relationships": []}')
    )
    fake_router.generate = MagicMock(
        side_effect=AssertionError("legacy sync .generate() should not be called")
    )

    fake_session = MagicMock()
    fake_session.run = AsyncMock(return_value=AsyncMock(single=AsyncMock(return_value=None)))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    fake_driver = MagicMock()
    fake_driver.session = MagicMock(return_value=fake_session)

    service = entity_timeline.EntityTimelineService(fake_driver)

    with patch.object(
        entity_timeline, "get_model_router_async",
        new=AsyncMock(return_value=fake_router),
    ):
        await service.extract_and_update(
            project_id="proj-1",
            chapter_idx=2,
            chapter_text="主角在酒馆与江枫相遇。",
        )

    fake_router.generate_with_tier_fallback.assert_awaited_once()
    call_kwargs = fake_router.generate_with_tier_fallback.call_args.kwargs
    assert call_kwargs["task_type"] == "extraction"
    assert call_kwargs["_log_meta"]["caller"] == "EntityTimelineService.extract_and_update"
    assert call_kwargs["_log_meta"]["project_id"] == "proj-1"
    assert call_kwargs["_log_meta"]["chapter_idx"] == 2


# ---------------------------------------------------------------------------
# 4. HookManager._update_entities now dispatches a celery task (no inline cypher)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_manager_update_entities_dispatches_only():
    """_update_entities must NOT touch Neo4j directly anymore.

    Regression guard: any cypher MERGE for Character with flat
    c.status/c.location was the source of the 49 GqlStatus warnings.
    """
    from app.services import hook_manager as hm

    mgr = hm.HookManager()

    with patch.object(
        hm, "dispatch_entity_extraction", return_value=True
    ) as mock_dispatch, patch(
        "app.db.neo4j.get_neo4j"
    ) as mock_get_neo4j:
        await mgr._update_entities(
            project_id="p-1",
            chapter_idx=3,
            chapter_text="some chapter text",
        )

    mock_dispatch.assert_called_once()
    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs["project_id"] == "p-1"
    assert kwargs["chapter_idx"] == 3
    assert kwargs["caller"] == "HookManager.run_post_hooks"
    # Critical: no neo4j driver was even fetched (no inline writes)
    mock_get_neo4j.assert_not_called()


# ---------------------------------------------------------------------------
# 5. _check_character_consistency reads via EntityTimelineService (correct schema)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_character_consistency_uses_entity_timeline_service():
    """Regression: must NOT read flat c.status / c.location -- those fields
    are never written and produce UnknownPropertyKey warnings on every call.
    Must instead call EntityTimelineService.get_active_characters_at.
    """
    from app.services import hook_manager as hm
    from app.services.entity_timeline import CharacterSnapshot

    mgr = hm.HookManager()

    fake_driver = MagicMock()
    fake_driver.session = MagicMock()  # would raise if anyone tries to use it directly

    async def _fake_get_neo4j():
        yield fake_driver

    snap_dead = CharacterSnapshot(
        name="江枫",
        status={"alive": False, "location": "酒馆"},
        chapter_start=1, chapter_end=None,
    )
    snap_alive = CharacterSnapshot(
        name="主角",
        status={"alive": True, "location": "酒馆"},
        chapter_start=1, chapter_end=None,
    )

    fake_service = MagicMock()
    fake_service.get_active_characters_at = AsyncMock(
        return_value=[snap_alive, snap_dead]
    )

    outline = {"events": ["主角与江枫相遇"]}

    with patch("app.db.neo4j.get_neo4j", new=_fake_get_neo4j), \
         patch("app.services.entity_timeline.EntityTimelineService",
               return_value=fake_service):
        # Bypass _extract_character_names to control the input set
        with patch.object(mgr, "_extract_character_names",
                          return_value=["主角", "江枫"]):
            warnings = await mgr._check_character_consistency(
                project_id="p-1",
                chapter_idx=5,
                chapter_outline=outline,
            )

    fake_service.get_active_characters_at.assert_awaited_once_with("p-1", 4)
    # The dead character must trigger exactly one warning
    assert any("江枫" in w for w in warnings), warnings
    assert all("主角" not in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# 6. ExtractionMarker idempotency: second run sees status='completed' -> skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_chapter_async_idempotent_on_completed_marker():
    """_extract_chapter_async must short-circuit when the marker is already
    'completed', without invoking EntityTimelineService at all."""
    from app.tasks import entity_tasks

    completed_record = {"status": "completed"}
    fake_result = MagicMock()
    fake_result.single = AsyncMock(return_value=completed_record)

    fake_session = MagicMock()
    fake_session.run = AsyncMock(return_value=fake_result)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    fake_driver = MagicMock()
    fake_driver.session = MagicMock(return_value=fake_session)

    # Patch init_neo4j to be a no-op + force _driver to our fake
    import app.db.neo4j as neo4j_mod
    with patch.object(neo4j_mod, "init_neo4j", new=AsyncMock(return_value=None)), \
         patch.object(neo4j_mod, "_driver", fake_driver), \
         patch.object(entity_tasks, "_load_chapter_text",
                      new=AsyncMock(side_effect=AssertionError(
                          "chapter loader must not run when marker=completed"))):
        result = await entity_tasks._extract_chapter_async(
            project_id="p-1",
            chapter_idx=2,
            caller="test",
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "already_completed"
