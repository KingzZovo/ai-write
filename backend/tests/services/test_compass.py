"""C4 / F3: narrative compass -- scale derivation, clamp, completion checklist.

Pure-logic tests pin the range invariants (scale is always a range, never a
single number), the +/-30% clamp, the six-point completion gate, the
JSON-tolerant parse, and the ContextPack injection tripwire.
"""

from __future__ import annotations

from app.services.compass_service import (
    _parse_json_object,
    assess_completion_readiness,
    clamp_scale,
    derive_estimated_scale,
    render_compass_anchor,
)


# --- derive_estimated_scale (always a range) --------------------------------


def test_derive_scale_is_a_range():
    scale = derive_estimated_scale(2_000_000)
    assert scale["min_chapters"] < scale["max_chapters"]
    assert scale["min_volumes"] <= scale["max_volumes"]
    assert scale["derived_chapters"] > 0
    # the derived midpoint sits inside the band
    assert scale["min_chapters"] <= scale["derived_chapters"] <= scale["max_chapters"]


def test_derive_scale_monotonic():
    small = derive_estimated_scale(500_000)
    big = derive_estimated_scale(2_000_000)
    assert big["max_chapters"] > small["max_chapters"]


def test_derive_scale_empty_on_missing():
    assert derive_estimated_scale(None) == {}
    assert derive_estimated_scale(0) == {}


# --- clamp_scale (stays a valid range within +/-30%) ------------------------


def test_clamp_keeps_within_band_and_range():
    derived = derive_estimated_scale(1_000_000)  # derived ~250 chapters
    dch = derived["derived_chapters"]
    # an absurd LLM proposal must be reined back inside +/-30%
    clamped = clamp_scale({"min_chapters": 1, "max_chapters": 99999}, derived)
    assert clamped["min_chapters"] >= int(dch * 0.7)
    assert clamped["max_chapters"] <= int(round(dch * 1.3))
    assert clamped["min_chapters"] < clamped["max_chapters"]


def test_clamp_noop_without_derived():
    assert clamp_scale({"min_chapters": 5, "max_chapters": 9}, {}) == {"min_chapters": 5, "max_chapters": 9}


# --- assess_completion_readiness (six-point gate) ---------------------------


def _compass(threads=None, ending="主角在权力与良知间抉择", scale=None):
    return {
        "ending_direction": ending,
        "open_threads": threads or [],
        "estimated_scale": scale or {"min_chapters": 100, "max_chapters": 150},
    }


def test_completion_blocked_below_min_chapters():
    r = assess_completion_readiness(_compass(), written_chapters=50, unresolved_foreshadows=0)
    assert r["can_complete"] is False
    assert any("下限" in b for b in r["blockers"])


def test_completion_blocked_by_active_threads():
    c = _compass(threads=[{"thread": "复仇线", "status": "active"}])
    r = assess_completion_readiness(c, written_chapters=200, unresolved_foreshadows=0)
    assert r["can_complete"] is False
    assert any("复仇线" in b for b in r["blockers"])


def test_completion_blocked_by_unresolved_foreshadows():
    r = assess_completion_readiness(_compass(), written_chapters=200, unresolved_foreshadows=3)
    assert r["can_complete"] is False
    assert any("伏笔" in b for b in r["blockers"])


def test_completion_clear_path_has_manual_check():
    r = assess_completion_readiness(_compass(), written_chapters=120, unresolved_foreshadows=0)
    assert r["can_complete"] is True
    assert any("终局命题" in m for m in r["manual_checks"])


def test_completion_over_max_warns():
    r = assess_completion_readiness(_compass(), written_chapters=200, unresolved_foreshadows=0)
    assert any("上限" in w for w in r["warnings"])


def test_completion_steady_state_heuristic_warns():
    r = assess_completion_readiness(
        _compass(), written_chapters=120, unresolved_foreshadows=0,
        recent_summaries=["主角喝茶散步", "村里日常", "买菜做饭", "闲聊", "睡觉"],
    )
    assert any("日常稳态" in w for w in r["warnings"])


# --- JSON-tolerant parse ----------------------------------------------------


def test_parse_json_object_fenced_and_prose():
    assert _parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_object('好的：{"a": 2} 完毕') == {"a": 2}
    assert _parse_json_object("not json") == {}
    assert _parse_json_object("[1,2,3]") == {}  # non-dict -> {}


# --- render + injection tripwire --------------------------------------------


def test_render_compass_anchor_and_empty():
    block = render_compass_anchor(_compass(), current_chapter_idx=42, max_chars=400)
    assert 0 < len(block) <= 400
    assert "方向锚" in block and "终局命题" in block
    assert render_compass_anchor({}) == ""


def test_context_pack_injects_compass_anchor():
    from app.services.context_pack import ContextPack

    pack = ContextPack()
    pack.compass_anchor = "【方向锚】终局命题：主角在权力与良知间抉择"
    out = pack.to_system_prompt()
    assert "方向锚" in out

    assert "方向锚" not in ContextPack().to_system_prompt()


def test_volume_regen_wired_to_compass_update():
    """C4: regenerate_volume must update the compass after saving the new
    volume outline. Source tripwire (the plan's biggest regression risk was
    missing this wiring)."""
    import inspect

    from app.api import volumes

    src = inspect.getsource(volumes)
    assert "update_on_new_volume(" in src


def test_compass_refresh_endpoint_registered():
    """The compass router must be mounted so /compass endpoints are reachable."""
    import inspect

    from app import main

    src = inspect.getsource(main)
    assert "compass_api.router" in src
