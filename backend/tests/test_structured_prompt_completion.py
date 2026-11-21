"""Shared Registry finish checks and caller-owned JSON repair boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import prompt_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["length", "max_tokens", "content_filter", "tool_calls"])
async def test_explicit_incomplete_json_uses_only_existing_repair(monkeypatch, finish):
    model = AsyncMock(side_effect=[
        SimpleNamespace(text='{"items": ["未确认"]}', finish_reason=finish),
        SimpleNamespace(text='{"items": ["正常交付"]}', finish_reason="stop"),
    ])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await prompt_registry.run_structured_prompt(
        "outline_chapter", "保留原任务", None, max_tokens=12000,
    )
    assert out == {"items": ["正常交付"]} and model.await_count == 2
    assert "provider_incomplete_response" in model.await_args_list[1].args[3]
    assert all(c.kwargs["max_tokens"] == 12000 for c in model.await_args_list)


@pytest.mark.asyncio
async def test_incomplete_repair_returns_failure_and_original_output(monkeypatch):
    model = AsyncMock(return_value=SimpleNamespace(text='{"items": ["原规划"]}', finish_reason="length"))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await prompt_registry.run_structured_prompt("outline_chapter", "任务", None)
    assert out["parse_error"] is True and out["raw_text"] == model.return_value.text
    assert out["validation_error"] == "provider_incomplete_response"
    assert model.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '{"items":["关键事件"]',
    '{"items":["事件"], "number":NaN}',
    '{"items":["事件"], "number":1e999}',
    '{"items":["被覆盖事件"], "items":["新事件"]}',
    '```json\n{"items":["事件"]}',
])
async def test_strict_caller_retains_bad_output_without_nested_calls(monkeypatch, raw):
    model = AsyncMock(return_value=SimpleNamespace(text=raw, finish_reason="stop"))
    validator = lambda payload: None
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await prompt_registry.run_structured_prompt(
        "scene_planner", "任务", None, strict_json=True,
        retry_on_validation_error=False, output_validator=validator,
    )
    assert out["parse_error"] is True and out["raw_text"] == raw
    assert model.await_count == 1
    assert not {"strict_json", "retry_on_validation_error", "output_validator"} & model.call_args.kwargs.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["stop", "end_turn", "stop_sequence", None])
async def test_normal_and_legacy_unspecified_endings_keep_compatibility(monkeypatch, finish):
    model = AsyncMock(return_value=SimpleNamespace(text='```json\n{"items": ["完整事件"]}\n```', finish_reason=finish))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert await prompt_registry.run_structured_prompt(
        "scene_planner", "任务", None, strict_json=True,
    ) == {"items": ["完整事件"]}
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_other_tasks_keep_legacy_json_repair_unless_strict_requested(monkeypatch):
    model = AsyncMock(return_value=SimpleNamespace(text='{"summary": "原任务"', finish_reason="stop"))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert await prompt_registry.run_structured_prompt("legacy_task", "任务", None) == {"summary": "原任务"}
    assert model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["start", "planner"])
@pytest.mark.parametrize("failure", ["abnormal_finish", "invalid_json"])
async def test_arc_planning_rejects_registry_failure_before_materialization(monkeypatch, entrypoint, failure):
    from fastapi import HTTPException
    from app.api import arc as arc_api

    model = AsyncMock(return_value=SimpleNamespace(
        text='{"title": "未确认弧纲", "beats": []}' if failure == "abnormal_finish" else 'not JSON',
        finish_reason="length" if failure == "abnormal_finish" else "stop",
    ))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    save = AsyncMock(return_value={"should_not_save": True})
    monkeypatch.setattr(arc_api, "_save_generated_arc", save)
    monkeypatch.setattr(arc_api, "_load_current_arc", AsyncMock(return_value=(None, None, None)))
    body = arc_api.StartArcBody(idea="宋岚修复旧书", background="街边旧书店",
                               core_setup="还书", opening_scene="宋岚进店", target_chapters=5)
    if entrypoint == "start":
        with pytest.raises(HTTPException) as error:
            await arc_api._create_arc(object(), "p", body, 1)
        assert error.value.status_code == 502
    else:
        from app.services.arc_outline import generate_arc_outline
        result = await generate_arc_outline(idea=body.idea, background=body.background, core_setup=body.core_setup,
            opening_scene=body.opening_scene, target_chapters=body.target_chapters, db=None, previous_arc={"reported_progress": "原进展"})
        assert result["available"] is False
    assert save.await_count == 0 and model.await_count == 2
