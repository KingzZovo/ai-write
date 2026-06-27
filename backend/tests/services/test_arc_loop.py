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
