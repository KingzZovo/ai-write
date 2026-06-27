from __future__ import annotations


def test_arc_state_roundtrip_and_defaults() -> None:
    from app.services.arc_loop import ArcState, parse_arc_state, serialize_arc_state

    raw = {
        "volume_idx": 1,
        "_arc": {
            "is_arc": True,
            "title": "边境小城御敌",
            "core_setup": "主角在边境小城的大敌",
            "opening_scene": "有人上门找茬",
            "target_chapters": 20,
            "status": "active",
            "chapters_written": 0,
            "running_outline": "",
            "next_direction": None,
            "suggestions": [],
        },
    }
    state = parse_arc_state(raw)
    assert state is not None
    assert state.title == "边境小城御敌"
    assert state.target_chapters == 20
    assert state.status == "active"
    assert state.chapters_written == 0

    out = serialize_arc_state(state, volume_idx=1)
    assert out["volume_idx"] == 1
    assert out["_arc"]["title"] == "边境小城御敌"
    assert out["_arc"]["is_arc"] is True


def test_parse_arc_state_returns_none_for_non_arc() -> None:
    from app.services.arc_loop import parse_arc_state

    assert parse_arc_state({"volume_idx": 3, "core_conflict": "x"}) is None
    assert parse_arc_state({}) is None
    assert parse_arc_state(None) is None


def test_advance_after_writing_chapter_awaits_direction() -> None:
    from app.services.arc_loop import (
        ArcState, advance_arc_state, ARC_STATUS_AWAITING,
    )

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=20, status="active", chapters_written=0)
    new = advance_arc_state(state, event="chapter_written",
                            running_outline_append="第1章：主角出场。")
    assert new.chapters_written == 1
    assert new.status == ARC_STATUS_AWAITING
    assert "第1章" in new.running_outline
    assert new.next_direction is None


def test_advance_to_completed_when_target_reached() -> None:
    from app.services.arc_loop import ArcState, advance_arc_state, ARC_STATUS_COMPLETED

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=2, status="active", chapters_written=1)
    new = advance_arc_state(state, event="chapter_written",
                            running_outline_append="第2章。")
    assert new.chapters_written == 2
    assert new.status == ARC_STATUS_COMPLETED


def test_advance_set_direction_returns_to_active() -> None:
    from app.services.arc_loop import ArcState, advance_arc_state, ARC_STATUS_ACTIVE

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=20, status="awaiting_direction", chapters_written=1)
    new = advance_arc_state(state, event="set_direction",
                            next_direction="主角发现跑不了，打算狐假虎威")
    assert new.status == ARC_STATUS_ACTIVE
    assert new.next_direction == "主角发现跑不了，打算狐假虎威"


def test_advance_set_direction_blocked_when_completed() -> None:
    from app.services.arc_loop import ArcState, advance_arc_state, ARC_STATUS_COMPLETED

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=2, status="completed", chapters_written=2)
    new = advance_arc_state(state, event="set_direction", next_direction="x")
    assert new.status == ARC_STATUS_COMPLETED


import pytest


def test_build_arc_outline_prompt_forbids_long_range() -> None:
    from app.services.arc_loop import build_arc_outline_prompt

    prompt = build_arc_outline_prompt(
        idea="玄幻，主角穿越到边境小城",
        background="功法体系XX，战力体系YY",
        core_setup="主角在边境小城有大敌",
        opening_scene="有人上门找茬",
        target_chapters=20,
    )
    assert "只规划" in prompt
    assert "20" in prompt
    assert "伏笔" in prompt
    assert "有人上门找茬" in prompt


@pytest.mark.asyncio
async def test_generate_arc_outline_happy(monkeypatch) -> None:
    import app.services.arc_loop as al

    async def fake_structured(task_type, user_content, db, **kwargs):
        assert task_type == "arc_outline"
        return {
            "title": "边境小城御敌",
            "beats": [
                {"chapter": 1, "beat": "主角穿越，遇上门挑衅"},
                {"chapter": 2, "beat": "狐假虎威吓退对手"},
            ],
        }

    monkeypatch.setattr(al, "run_structured_prompt", fake_structured)

    result = await al.generate_arc_outline(
        idea="玄幻穿越", background="体系XX", core_setup="有大敌",
        opening_scene="上门找茬", target_chapters=20, db=object(),
        project_id="p",
    )
    assert result["title"] == "边境小城御敌"
    assert len(result["beats"]) == 2


@pytest.mark.asyncio
async def test_generate_arc_outline_degrades(monkeypatch) -> None:
    import app.services.arc_loop as al

    async def boom(*a, **k):
        raise RuntimeError("relay 503")

    monkeypatch.setattr(al, "run_structured_prompt", boom)

    result = await al.generate_arc_outline(
        idea="x", background="y", core_setup="z", opening_scene="w",
        target_chapters=20, db=object(), project_id="p",
    )
    assert result["available"] is False
