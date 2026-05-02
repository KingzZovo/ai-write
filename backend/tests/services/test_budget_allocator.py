"""Unit tests for the budget allocator (v1.3.0 chunk-29).

Pure algorithm tests -- no DB, no FastAPI, no fixtures.
"""

from __future__ import annotations

import pytest

from app.services.budget_allocator import (
    CHAPTER_DEFAULT,
    PROJECT_DEFAULT,
    VOLUME_DEFAULT,
    allocate_even,
    allocate_project_budget,
    allocate_weighted,
)


# ---------------------------------------------------------------------------
# allocate_even
# ---------------------------------------------------------------------------


def test_allocate_even_exact_split():
    out = allocate_even(600_000, 3)
    assert out == [200_000, 200_000, 200_000]
    assert sum(out) == 600_000


def test_allocate_even_remainder_goes_to_last():
    out = allocate_even(10, 3)
    assert out == [3, 3, 4]
    assert sum(out) == 10


def test_allocate_even_n_equals_one():
    assert allocate_even(3_000_000, 1) == [3_000_000]


def test_allocate_even_total_zero():
    assert allocate_even(0, 5) == [0, 0, 0, 0, 0]


def test_allocate_even_invalid_n():
    with pytest.raises(ValueError):
        allocate_even(100, 0)
    with pytest.raises(ValueError):
        allocate_even(100, -1)


def test_allocate_even_invalid_total():
    with pytest.raises(ValueError):
        allocate_even(-1, 3)


# ---------------------------------------------------------------------------
# allocate_weighted
# ---------------------------------------------------------------------------


def test_allocate_weighted_basic():
    out = allocate_weighted(1000, [1.0, 1.0, 2.0])
    # 250 / 250 / 500 (last absorbs any rounding residue)
    assert sum(out) == 1000
    assert out[2] >= out[0]
    assert out[2] >= out[1]


def test_allocate_weighted_zero_weights_falls_back():
    out = allocate_weighted(10, [0.0, 0.0, 0.0])
    assert out == allocate_even(10, 3)
    assert sum(out) == 10


def test_allocate_weighted_rejects_negative():
    with pytest.raises(ValueError):
        allocate_weighted(100, [1.0, -1.0])


def test_allocate_weighted_rejects_empty():
    with pytest.raises(ValueError):
        allocate_weighted(100, [])


# ---------------------------------------------------------------------------
# allocate_project_budget
# ---------------------------------------------------------------------------


def _mk_vol(vid: str, idx: int, current: int, chapters: list[dict] | None = None) -> dict:
    return {
        "id": vid,
        "volume_idx": idx,
        "current_target": current,
        "chapters": chapters or [],
    }


def _mk_ch(cid: str, idx: int, current: int) -> dict:
    return {"id": cid, "chapter_idx": idx, "current_target": current}


def test_project_budget_empty_volumes():
    plan = allocate_project_budget(
        project_total=PROJECT_DEFAULT, volumes=[], force=False
    )
    assert plan["volumes"] == []
    assert plan["volume_sum"] == 0
    assert plan["volumes_changed"] == 0
    assert plan["chapters_changed"] == 0


def test_project_budget_even_split_5_volumes():
    volumes = [_mk_vol(f"v{i}", i, VOLUME_DEFAULT) for i in range(1, 6)]
    plan = allocate_project_budget(
        project_total=3_000_000, volumes=volumes, force=False
    )
    # 3_000_000 / 5 == 600_000 exact; all should be 600_000
    new_targets = [v["new_target"] for v in plan["volumes"]]
    assert new_targets == [600_000, 600_000, 600_000, 600_000, 600_000]
    assert plan["volume_sum"] == 3_000_000
    assert plan["volumes_changed"] == 5


def test_project_budget_remainder_absorbed_by_last():
    volumes = [_mk_vol(f"v{i}", i, VOLUME_DEFAULT) for i in range(1, 4)]
    plan = allocate_project_budget(
        project_total=10, volumes=volumes, force=True
    )
    new_targets = [v["new_target"] for v in plan["volumes"]]
    assert new_targets == [3, 3, 4]
    assert plan["volume_sum"] == 10


def test_project_budget_preserves_user_override_without_force():
    # Middle volume has been manually set to 123456 (not the default).
    volumes = [
        _mk_vol("v1", 1, VOLUME_DEFAULT),
        _mk_vol("v2", 2, 123_456),
        _mk_vol("v3", 3, VOLUME_DEFAULT),
    ]
    plan = allocate_project_budget(
        project_total=3_000_000, volumes=volumes, force=False
    )
    # Only v1 and v3 get rewritten; v2 keeps its user value.
    assert plan["volumes"][0]["changed"] is True
    assert plan["volumes"][1]["changed"] is False
    assert plan["volumes"][1]["new_target"] == 123_456
    assert plan["volumes"][2]["changed"] is True
    assert plan["volumes_changed"] == 2


def test_project_budget_force_overwrites_user_values():
    volumes = [
        _mk_vol("v1", 1, 999_999),
        _mk_vol("v2", 2, 1_234_567),
    ]
    plan = allocate_project_budget(
        project_total=2_000_000, volumes=volumes, force=True
    )
    new_targets = [v["new_target"] for v in plan["volumes"]]
    assert new_targets == [1_000_000, 1_000_000]
    assert all(v["changed"] for v in plan["volumes"])


def test_project_budget_chapters_allocated_from_volume_target():
    # 4 default-valued chapters under a default-valued volume;
    # project_total=400_000 -> volume 400_000 -> each chapter 100_000
    # (different from 50_000 default, so chapters_changed must fire).
    chapters = [_mk_ch(f"c{i}", i, CHAPTER_DEFAULT) for i in range(1, 5)]
    volumes = [_mk_vol("v1", 1, VOLUME_DEFAULT, chapters=chapters)]
    plan = allocate_project_budget(
        project_total=400_000, volumes=volumes, force=False
    )
    v0 = plan["volumes"][0]
    assert v0["new_target"] == 400_000
    ch_new = [c["new_target"] for c in v0["chapters"]]
    assert ch_new == [100_000, 100_000, 100_000, 100_000]
    assert sum(ch_new) == v0["new_target"]
    assert plan["chapters_changed"] == 4


def test_project_budget_chapters_preserve_user_override():
    # c2 is user-customized; allocator must leave it alone when force=False
    chapters = [
        _mk_ch("c1", 1, CHAPTER_DEFAULT),
        _mk_ch("c2", 2, 77_777),
        _mk_ch("c3", 3, CHAPTER_DEFAULT),
    ]
    volumes = [_mk_vol("v1", 1, VOLUME_DEFAULT, chapters=chapters)]
    plan = allocate_project_budget(
        project_total=300_000, volumes=volumes, force=False
    )
    ch_plans = plan["volumes"][0]["chapters"]
    assert ch_plans[1]["changed"] is False
    assert ch_plans[1]["new_target"] == 77_777
    assert ch_plans[0]["changed"] is True
    assert ch_plans[2]["changed"] is True


def test_project_budget_big_roundtrip_300w_5_30():
    # 3_000_000 over 5 volumes, 30 chapters each -> every chapter is 20_000
    volumes = []
    for vi in range(1, 6):
        chs = [_mk_ch(f"v{vi}c{ci}", ci, CHAPTER_DEFAULT) for ci in range(1, 31)]
        volumes.append(_mk_vol(f"v{vi}", vi, VOLUME_DEFAULT, chapters=chs))
    plan = allocate_project_budget(
        project_total=3_000_000, volumes=volumes, force=True
    )
    assert plan["volume_sum"] == 3_000_000
    for v in plan["volumes"]:
        assert v["new_target"] == 600_000
        ch_sum = sum(c["new_target"] for c in v["chapters"])
        assert ch_sum == v["new_target"]
        for c in v["chapters"]:
            assert c["new_target"] == 20_000


def test_project_budget_rejects_negative_total():
    with pytest.raises(ValueError):
        allocate_project_budget(
            project_total=-1, volumes=[_mk_vol("v1", 1, VOLUME_DEFAULT)]
        )


def test_project_budget_default_constants():
    # Guard against accidental change of the documented defaults.
    assert PROJECT_DEFAULT == 3_000_000
    assert VOLUME_DEFAULT == 200_000
    assert CHAPTER_DEFAULT == 50_000
