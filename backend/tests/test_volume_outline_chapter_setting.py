"""E2E defect (2026-07-26): chapters-per-volume setting ignored by staged
volume outlines.

Live evidence: a project configured for 3-4 chapters/volume (settings_json
``chapters_per_volume_min/max``) — with the same request in user_input — still
produced 10-chapter volume outlines with the log "missing/invalid
chapter_count=None; backfilling meta from fallback".

Pins the fix in app/services/outline_generator.py:
  - ``resolve_chapters_per_volume_pref`` reads/clamps the settings_json keys
  - the staged V1 meta prompt carries an explicit hard-constraint directive
  - ``_fallback_volume_meta`` respects the setting even on meta parse failure
"""
from __future__ import annotations

import json as _json
import re as _re

import pytest

from app.services.outline_generator import (
    OutlineGenerator,
    resolve_chapters_per_volume_pref,
)


# ---------------------------------------------------------------------------
# resolve_chapters_per_volume_pref
# ---------------------------------------------------------------------------


class TestResolveChaptersPerVolumePref:
    def test_min_max_range(self):
        assert resolve_chapters_per_volume_pref(
            {"chapters_per_volume_min": 3, "chapters_per_volume_max": 4}
        ) == (3, 4)

    def test_target_only_collapses_to_point_range(self):
        assert resolve_chapters_per_volume_pref(
            {"chapters_per_volume_target": 20}
        ) == (20, 20)

    def test_no_keys_returns_none(self):
        assert resolve_chapters_per_volume_pref({}) is None
        assert resolve_chapters_per_volume_pref(None) is None
        assert resolve_chapters_per_volume_pref({"target_chapter_words": 4000}) is None

    def test_clamped_to_fifty(self):
        assert resolve_chapters_per_volume_pref(
            {"chapters_per_volume_min": 100, "chapters_per_volume_max": 200}
        ) == (50, 50)

    def test_non_positive_and_garbage_values_ignored(self):
        assert resolve_chapters_per_volume_pref(
            {"chapters_per_volume_min": 0, "chapters_per_volume_max": "abc"}
        ) is None
        # One usable key is enough.
        assert resolve_chapters_per_volume_pref(
            {"chapters_per_volume_min": 0, "chapters_per_volume_max": 4}
        ) == (4, 4)

    def test_numeric_strings_accepted(self):
        assert resolve_chapters_per_volume_pref(
            {"chapters_per_volume_min": "3", "chapters_per_volume_max": "4"}
        ) == (3, 4)

    def test_inverted_range_normalized(self):
        assert resolve_chapters_per_volume_pref(
            {"chapters_per_volume_min": 8, "chapters_per_volume_max": 4}
        ) == (4, 8)


# ---------------------------------------------------------------------------
# _fallback_volume_meta precedence
# ---------------------------------------------------------------------------


_BOOK_OUTLINE_WITH_PLAN = {
    "raw_text": "近未来科幻悬疑：江临追查城市噪音消失的深海陷阱。",
    "volume_plan": [{"idx": 1, "title": "深水无声", "est_chapters": 12}],
}


class TestFallbackVolumeMetaChapterCount:
    def test_setting_beats_plan_estimate(self):
        meta = OutlineGenerator._fallback_volume_meta(
            _BOOK_OUTLINE_WITH_PLAN, 1, preferred_chapter_count=4
        )
        assert meta["chapter_count"] == 4

    def test_user_notes_explicit_count_beats_setting(self):
        meta = OutlineGenerator._fallback_volume_meta(
            _BOOK_OUTLINE_WITH_PLAN,
            1,
            user_notes="chapter_count=6",
            preferred_chapter_count=4,
        )
        assert meta["chapter_count"] == 6

    def test_without_setting_plan_estimate_still_wins(self):
        meta = OutlineGenerator._fallback_volume_meta(_BOOK_OUTLINE_WITH_PLAN, 1)
        assert meta["chapter_count"] == 12

    def test_without_setting_or_plan_legacy_default_ten(self):
        meta = OutlineGenerator._fallback_volume_meta({"raw_text": "x"}, 2)
        assert meta["chapter_count"] == 10

    def test_setting_applies_when_no_plan_item(self):
        meta = OutlineGenerator._fallback_volume_meta(
            {"raw_text": "x"}, 2, preferred_chapter_count=4
        )
        assert meta["chapter_count"] == 4


# ---------------------------------------------------------------------------
# Staged flow: prompt directive + parse-failure fallback (live-E2E replay)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = None
        self.model = "fake"


class _BrokenMetaRouter:
    """V1 meta returns unrecoverable garbage; batch calls return valid JSON."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._meta_done = False

    async def generate(self, task_type=None, messages=None, **kwargs):
        user_content = ""
        for m in messages or []:
            if m.get("role") == "user":
                user_content = m.get("content", "")
        self.calls.append(user_content)
        if task_type == "outline_volume_meta":
            return _FakeResult("对不起，我无法输出 JSON。")
        count_m = _re.search(r"count=(\d+)", user_content)
        count = int(count_m.group(1)) if count_m else 5
        start_m = _re.search(r"start=(\d+)", user_content)
        start = int(start_m.group(1)) if start_m else 1
        batch = {
            "batch": [
                {
                    "chapter_idx": start + j,
                    "title": f"静默第{start + j}章",
                    "summary": "江临下潜追查声呐站。",
                    "key_events": ["下潜", "追查"],
                }
                for j in range(count)
            ]
        }
        return _FakeResult(_json.dumps(batch, ensure_ascii=False))


@pytest.mark.asyncio
async def test_meta_prompt_carries_setting_and_failure_never_starts_batches(
    monkeypatch,
):
    gen = OutlineGenerator(project_id=None)
    gen.router = _BrokenMetaRouter()

    async def fake_pref():
        return (3, 4)

    monkeypatch.setattr(gen, "_load_chapters_per_volume_pref", fake_pref)

    result = await gen._generate_volume_outline_staged(
        _BOOK_OUTLINE_WITH_PLAN, volume_idx=1
    )

    # (a) The V1 meta prompt carries the explicit hard constraint.
    meta_prompt = gen.router.calls[0]
    assert "章数硬约束" in meta_prompt
    assert "3-4" in meta_prompt

    # (b) Repeated invalid meta stops planning; neither project range nor
    # book estimate authorizes inventing a replacement plot.
    assert result["_parse_error"] is True
    assert result["chapter_summaries"] == []
    assert len(gen.router.calls) == 2


@pytest.mark.asyncio
async def test_no_setting_still_requires_valid_meta(monkeypatch):
    gen = OutlineGenerator(project_id=None)
    gen.router = _BrokenMetaRouter()

    async def fake_pref():
        return None

    monkeypatch.setattr(gen, "_load_chapters_per_volume_pref", fake_pref)

    result = await gen._generate_volume_outline_staged(
        _BOOK_OUTLINE_WITH_PLAN, volume_idx=1
    )

    assert "章数硬约束" not in gen.router.calls[0]
    # Missing project settings do not permit a synthetic plan fallback.
    assert result["_parse_error"] is True
    assert result["chapter_summaries"] == []
    assert len(gen.router.calls) == 2


@pytest.mark.asyncio
async def test_pref_loader_returns_none_without_project_id():
    gen = OutlineGenerator(project_id=None)
    assert await gen._load_chapters_per_volume_pref() is None


@pytest.mark.asyncio
async def test_pref_loader_keeps_confirmed_100_chapters_per_volume(monkeypatch):
    """2026-11-21:规范键写「每章 2000 字、每卷 100 章」时,卷纲硬约束必须是 100 章/卷。

    旧实现复用 ``resolve_chapters_per_volume_pref`` 的 [1, 50] 夹取 → 返回 (50, 50):
    卷纲被硬约束成 50 章/卷,每章预算 = 卷目标字数 ÷ 50(=200000/50 = 4000 字/章),
    与已确认书纲的「5 卷 × 100 章、每章 2000 字」自相矛盾(实测故障:设置改了仍按
    4000 字/章算)。
    """
    from types import SimpleNamespace
    from uuid import uuid4

    import app.db.session as db_session

    project = SimpleNamespace(
        target_word_count=1_000_000,
        settings_json={
            "target_chapter_words": 2000,
            "chapters_per_volume_min": 100,
            "chapters_per_volume_max": 100,
            "chapters_per_volume_target": 100,
        },
    )

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, pk):
            return project

    monkeypatch.setattr(db_session, "async_session_factory", lambda: _FakeSession())
    gen = OutlineGenerator(project_id=str(uuid4()))
    assert await gen._load_chapters_per_volume_pref() == (100, 100)
