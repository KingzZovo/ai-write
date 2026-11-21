"""Provider output is complete, bounded and untrusted until Registry validation."""
import asyncio
from copy import deepcopy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import arc as api
from app.services import arc_outline as planner, arc_loop, prompt_cache, prompt_registry
from tests.support.arc_outline import arc_chapter, arc_meta, arc_outline
from tests.test_arc_planning import generate_arc, response
from tests.test_chapter_order_maintenance import book


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_count", "missing_state", "bool_state", "blank_title"])
async def test_meta_repair_keeps_the_original_request_and_rejected_output(monkeypatch, change):
    bad = arc_meta(4)
    if change == "wrong_count": bad["chapter_count"] = 3
    elif change == "missing_state": bad.pop("volume_end_state")
    elif change == "bool_state": bad["volume_start_state"] = True
    else: bad["title"] = "   "
    model = AsyncMock(side_effect=[response(bad), response(arc_meta(4)),
        response({"chapter_summaries": [arc_chapter(i) for i in range(1, 5)]})])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await generate_arc()
    assert out["available"] is True and model.await_count == 3
    first, repair = model.await_args_list[:2]
    assert first.args[0] == repair.args[0] == "arc_outline_meta"
    assert first.args[1] in repair.args[1]
    assert json.dumps(bad, ensure_ascii=False) in repair.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["meta", "batch"])
@pytest.mark.parametrize("fault", ["truncated", "duplicate_key", "nonfinite", "abnormal_finish"])
async def test_stage_repairs_raw_delivery_without_nested_registry_retry(monkeypatch, stage, fault):
    meta = arc_meta(4)
    batch = {"chapter_summaries": [arc_chapter(i) for i in range(1, 5)]}
    raw = json.dumps(meta if stage == "meta" else batch, ensure_ascii=False)
    if fault == "truncated": raw = raw[:-1]
    elif fault == "duplicate_key": raw = raw[:-1] + ', "extra": 1, "extra": 2}'
    elif fault == "nonfinite": raw = raw[:-1] + ', "extra": 1e999}'
    bad = response(raw, "length" if fault == "abnormal_finish" else "stop")
    replies = [bad, response(meta), response(batch)] if stage == "meta" else [response(meta), bad, response(batch)]
    model = AsyncMock(side_effect=replies)
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert (await generate_arc())["available"] is True
    assert model.await_count == 3
    repair = model.await_args_list[1 if stage == "meta" else 2]
    assert raw in repair.args[1]
    assert "宋岚借了林禾的书，封面松脱。" in repair.args[1]
    assert "max_tokens" not in repair.kwargs  # Inherit the configured route, not a smaller repair budget.


@pytest.mark.asyncio
async def test_failed_meta_never_starts_chapter_planning(monkeypatch):
    model = AsyncMock(return_value=response({"title": "半截元信息"}))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    result = await generate_arc(40)
    assert result["available"] is False and result["failed_stage"] == "meta"
    assert result["partial_outline"]["chapter_summaries"] == []
    assert model.await_count == 2
    assert all(call.args[0] == "arc_outline_meta" for call in model.await_args_list)


@pytest.mark.asyncio
async def test_maximum_arc_uses_at_most_one_repair_per_stage(monkeypatch):
    tried = set()
    async def run(task, user, db, background="", **kwargs):
        key = task
        if task == "arc_outline_meta":
            valid = arc_meta(40)
        else:
            start, end = map(int, re.search(r"start=(\d+), end=(\d+)", user).groups())
            key = (task, start)
            valid = {"chapter_summaries": [arc_chapter(i) for i in range(start, end + 1)]}
        if key not in tried:
            tried.add(key)
            return response({"incomplete": "原计划必须完整修复"})
        return response(valid)
    model = AsyncMock(side_effect=run)
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert (await generate_arc(40))["available"] is True
    assert model.await_count == 18


@pytest.mark.asyncio
async def test_timeout_on_both_attempts_keeps_the_same_phase_deadline(monkeypatch):
    deadlines = []
    async def bounded(coro, timeout):
        deadlines.append(timeout)
        coro.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(planner.asyncio, "wait_for", bounded)
    model = AsyncMock()
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    result = await generate_arc()
    assert result["available"] is False and result["failed_stage"] == "meta"
    assert deadlines == [planner.ARC_PHASE_TIMEOUT_SECONDS] * 2
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_an_accepted_batch_stops_all_later_calls(monkeypatch):
    model = AsyncMock(side_effect=[response(arc_meta(11)),
        response({"chapter_summaries": [arc_chapter(i) for i in range(1, 6)]}), asyncio.CancelledError()])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    with pytest.raises(asyncio.CancelledError):
        await generate_arc(11)
    assert model.await_count == 3


@pytest.mark.parametrize("scope", ["meta", "chapter"])
@pytest.mark.parametrize("control", ["_chapter_id", "_order_unavailable", "raw_text", "chapter_id", "global_idx"])
def test_models_cannot_inject_internal_identity_into_descriptive_ledgers(scope, control):
    value = arc_meta(4) if scope == "meta" else arc_chapter(1)
    value["foreshadows" if scope == "meta" else "foreshadows_planted"] = [
        {"description": "保留署名", control: "不能由模型指定保存身份"}]
    with pytest.raises(ValueError):
        if scope == "meta": planner.validate_arc_meta(value, 4)
        else: planner.validate_arc_chapter(value, 1)


@pytest.mark.parametrize("scope", ["meta", "chapter"])
def test_too_long_database_title_is_rejected_without_clipping(scope):
    value = arc_meta(4) if scope == "meta" else arc_chapter(1)
    value["title"] = "书" * 501
    with pytest.raises(ValueError):
        if scope == "meta": planner.validate_arc_meta(value, 4)
        else: planner.validate_arc_chapter(value, 1)
    assert len(value["title"]) == 501


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "duplicate", "version", "bool_version", "failed"])
async def test_chapter_brief_returns_conflict_for_invalid_current_plan(book, change):
    from tests.test_arc_persistence import create_arc
    _, _, outline, _ = await create_arc(book)
    plan = deepcopy(outline.content_json)
    plan["beats"] = [{"chapter": 1, "beat": "不能借此旧方向继续写"}]
    if change == "missing": plan["chapter_summaries"].pop(0)
    elif change == "duplicate": plan["chapter_summaries"].append(deepcopy(plan["chapter_summaries"][0]))
    elif change == "version": plan["arc_plan_version"] = 2
    elif change == "bool_version": plan["arc_plan_version"] = True
    else: plan["available"] = False
    outline.content_json = plan
    book.session.commit()
    with pytest.raises(HTTPException) as exc:
        await api.chapter_brief(str(book.pid), book.db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_seeded_arc_stage_routes_inherit_existing_parent_settings_only(monkeypatch):
    stages = [p for p in prompt_registry.BUILTIN_PROMPTS if p["task_type"] in ("arc_outline_meta", "arc_outline_batch")]
    assert len(stages) == 2
    monkeypatch.setattr(prompt_registry, "BUILTIN_PROMPTS", stages)
    added = []
    db = SimpleNamespace(scalar=AsyncMock(return_value=0), add=added.append, flush=AsyncMock())
    registry = prompt_registry.PromptRegistry(db)
    parent = SimpleNamespace(endpoint_id=uuid4(), model_name="configured-model", temperature=0.73,
                             max_tokens=24576, model_tier="flagship", system_prompt="完整分卷的旧任务")
    monkeypatch.setattr(registry, "get", AsyncMock(return_value=parent))
    assert await registry.seed_builtins() == 2
    for asset in added:
        assert asset.mode == "structured"
        for key in ("endpoint_id", "model_name", "temperature", "max_tokens", "model_tier"):
            assert getattr(asset, key) == getattr(parent, key)
        assert asset.system_prompt != parent.system_prompt


@pytest.mark.asyncio
async def test_actual_registry_uses_stage_roles_and_unchanged_route_budget_on_repair(monkeypatch):
    from tests.test_c3_prompt_cache import _make_asset
    prompt_cache.reset_for_tests()
    stages = {p["task_type"]: p for p in prompt_registry.BUILTIN_PROMPTS if p["task_type"] in ("arc_outline_meta", "arc_outline_batch")}
    assets = {name: _make_asset(task_type=name, system_prompt=spec["system_prompt"]) for name, spec in stages.items()}
    for asset in assets.values():
        asset.max_tokens = 24576
        asset.model_name = "configured-model"
        asset.mode = "structured"
    snapshots = {name: prompt_cache._snapshot_from_asset(asset) for name, asset in assets.items()}
    lookup = AsyncMock(side_effect=lambda name, db: snapshots[name])
    monkeypatch.setattr(prompt_cache, "get_snapshot", lookup)
    call = AsyncMock(side_effect=[response(arc_meta(4)), response({"chapter_summaries": []}),
        response({"chapter_summaries": [arc_chapter(i) for i in range(1, 5)]})])
    monkeypatch.setattr(prompt_registry, "get_model_router_async", AsyncMock(return_value=SimpleNamespace(generate_with_tier_fallback=call)))
    try:
        result = await planner.generate_arc_outline(idea="修书", background="保留署名", core_setup="归还",
            opening_scene="检查", target_chapters=4, db=object(), project_id="p")
        assert result["available"] is True and call.await_count == 3
        for request in call.await_args_list:
            task, messages = request.args
            assert messages[0]["content"] == stages[task]["system_prompt"]
            assert request.kwargs["route"].max_tokens == 24576
            assert request.kwargs["route"].model == "configured-model"
            assert "max_tokens" not in request.kwargs
        assert call.await_args_list[-1].kwargs["temperature"] == 0.0
    finally:
        prompt_cache.reset_for_tests()
