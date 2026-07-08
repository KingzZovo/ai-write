"""Tests for the foreshadow debt health score (Q4, adapted from QMAI).

Scoring contract (max 100, floor 0):
- status == "planted" and stalled >= 5 chapters since planted_chapter -> critical, -15 each
- status in ("ripening", "ready") (advanced past planted) and stalled >= 10
  chapters since planted_chapter -> warning, -5 each
- resolved foreshadows are skipped entirely
- total unresolved beyond a soft cap of 5 -> -2 per extra item
- render_debt_warning gates on score < 60
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.foreshadow_manager import (
    CRITICAL_STALL_CHAPTERS,
    DEBT_GATE_THRESHOLD,
    UNRESOLVED_SOFT_CAP,
    WARNING_STALL_CHAPTERS,
    compute_debt_score,
    render_debt_warning,
)


def _fs(
    description: str = "神秘玉佩",
    status: str = "planted",
    planted_chapter: int = 0,
    type: str = "plot",
    resolved_chapter: int | None = None,
) -> dict:
    return {
        "description": description,
        "status": status,
        "planted_chapter": planted_chapter,
        "type": type,
        "resolved_chapter": resolved_chapter,
    }


class TestComputeDebtScore:
    def test_empty_list_is_perfect_health(self):
        debt = compute_debt_score([], current_chapter_idx=10)
        assert debt["score"] == 100
        assert debt["criticals"] == []
        assert debt["warnings"] == []
        assert debt["unresolved"] == 0

    def test_planted_stalled_exactly_five_chapters_is_critical(self):
        debt = compute_debt_score(
            [_fs(status="planted", planted_chapter=0)],
            current_chapter_idx=CRITICAL_STALL_CHAPTERS,  # age == 5
        )
        assert len(debt["criticals"]) == 1
        assert debt["score"] == 85

    def test_planted_stalled_four_chapters_is_not_critical(self):
        debt = compute_debt_score(
            [_fs(status="planted", planted_chapter=1)],
            current_chapter_idx=CRITICAL_STALL_CHAPTERS,  # age == 4
        )
        assert debt["criticals"] == []
        assert debt["score"] == 100

    def test_advanced_stalled_ten_chapters_is_warning(self):
        for status in ("ripening", "ready"):
            debt = compute_debt_score(
                [_fs(status=status, planted_chapter=0)],
                current_chapter_idx=WARNING_STALL_CHAPTERS,  # age == 10
            )
            assert len(debt["warnings"]) == 1, status
            assert debt["criticals"] == [], status
            assert debt["score"] == 95, status

    def test_advanced_stalled_nine_chapters_is_not_warning(self):
        debt = compute_debt_score(
            [_fs(status="ripening", planted_chapter=1)],
            current_chapter_idx=WARNING_STALL_CHAPTERS,  # age == 9
        )
        assert debt["warnings"] == []
        assert debt["score"] == 100

    def test_resolved_is_skipped_entirely(self):
        debt = compute_debt_score(
            [_fs(status="resolved", planted_chapter=0, resolved_chapter=3)],
            current_chapter_idx=50,
        )
        assert debt["score"] == 100
        assert debt["unresolved"] == 0
        assert debt["criticals"] == []
        assert debt["warnings"] == []

    def test_unresolved_soft_cap_penalty(self):
        # 7 freshly planted foreshadows: no stall penalties, but 2 over the
        # soft cap of 5 -> -2 each.
        items = [
            _fs(description=f"伏笔{i}", planted_chapter=10)
            for i in range(UNRESOLVED_SOFT_CAP + 2)
        ]
        debt = compute_debt_score(items, current_chapter_idx=10)
        assert debt["unresolved"] == 7
        assert debt["criticals"] == []
        assert debt["warnings"] == []
        assert debt["score"] == 100 - 2 * 2

    def test_score_floor_is_zero(self):
        items = [
            _fs(description=f"伏笔{i}", planted_chapter=0) for i in range(10)
        ]
        debt = compute_debt_score(items, current_chapter_idx=100)
        assert debt["score"] == 0

    def test_entries_carry_description_for_prompt_and_api(self):
        debt = compute_debt_score(
            [_fs(description="灭门旧案", status="planted", planted_chapter=0)],
            current_chapter_idx=20,
        )
        assert debt["criticals"][0]["description"] == "灭门旧案"
        assert debt["criticals"][0]["planted_chapter"] == 0
        assert debt["criticals"][0]["age"] == 20

    def test_negative_age_does_not_penalize(self):
        # Foreshadow planted in a later chapter than current (e.g. outline
        # pre-planting): must not count as stalled.
        debt = compute_debt_score(
            [_fs(status="planted", planted_chapter=30)],
            current_chapter_idx=10,
        )
        assert debt["criticals"] == []
        assert debt["score"] == 100

    def test_accepts_orm_like_objects(self):
        class FakeForeshadow:
            description = "断剑来历"
            status = "planted"
            planted_chapter = 0
            type = "mystery"
            resolved_chapter = None

        debt = compute_debt_score([FakeForeshadow()], current_chapter_idx=8)
        assert len(debt["criticals"]) == 1
        assert debt["criticals"][0]["description"] == "断剑来历"


class TestRenderDebtWarning:
    def test_score_below_threshold_renders_warning(self):
        items = [
            _fs(description=f"超期伏笔{i}", status="planted", planted_chapter=0)
            for i in range(3)
        ]
        debt = compute_debt_score(items, current_chapter_idx=20)
        assert debt["score"] == 55  # 100 - 3*15
        assert debt["score"] < DEBT_GATE_THRESHOLD

        text = render_debt_warning(debt)
        assert text
        assert "55" in text
        assert "超期伏笔0" in text
        assert "禁止新埋" in text

    def test_score_at_or_above_threshold_renders_empty(self):
        items = [_fs(description="单条超期", status="planted", planted_chapter=0)]
        debt = compute_debt_score(items, current_chapter_idx=20)
        assert debt["score"] == 85
        assert render_debt_warning(debt) == ""

    def test_empty_or_malformed_debt_renders_empty(self):
        assert render_debt_warning({}) == ""
        assert render_debt_warning(None) == ""  # type: ignore[arg-type]


class TestContextPackDebtInjection:
    def test_system_prompt_contains_debt_warning_in_foreshadow_section(self):
        from app.services.context_pack import ContextPack

        warning = "【伏笔债务警报】健康分 40/100，本章优先推进或回收既有伏笔，禁止新埋伏笔。"
        pack = ContextPack(foreshadow_debt_warning=warning)
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "【伏笔追踪】" in prompt
        assert warning in prompt

    def test_system_prompt_omits_debt_warning_when_empty(self):
        from app.services.context_pack import ContextPack

        pack = ContextPack(foreshadow_debt_warning="")
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "伏笔债务警报" not in prompt


class TestBuildFactsWiring:
    """Mutation guard: ``ContextPackBuilder._build_facts`` must wire
    ``render_debt_warning(compute_debt_score(fs_rows, chapter_idx))`` into
    ``pack.foreshadow_debt_warning``.

    Drives the real ``_build_facts`` with a stub async db session: the
    Foreshadow query returns stalled rows that trip the debt gate; every
    other query returns empty results (each block is fail-safe).
    """

    @pytest.mark.asyncio
    async def test_build_facts_assigns_rendered_debt_warning(self):
        from app.models.project import Foreshadow
        from app.services.context_pack import ContextPack, ContextPackBuilder

        class _FakeForeshadowRow:
            status = "planted"
            planted_chapter = 0
            type = "plot"
            resolved_chapter = None
            narrative_proximity = 0.5

            def __init__(self, description: str) -> None:
                self.description = description
                self.resolve_conditions_json: list = []
                self.resolution_blueprint_json: dict = {}

        # 3 planted foreshadows stalled for 20 chapters -> score 55 (< 60
        # gate), so render_debt_warning yields a non-empty alert.
        fs_rows = [_FakeForeshadowRow(f"超期伏笔{i}") for i in range(3)]
        chapter_idx = 20

        async def _fake_execute(stmt, *args, **kwargs):
            try:
                entities = [d.get("entity") for d in stmt.column_descriptions]
            except Exception:
                entities = []
            rows = fs_rows if Foreshadow in entities else []
            result = MagicMock()
            result.all = MagicMock(return_value=[])
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=rows)
            result.scalars = MagicMock(return_value=scalars)
            return result

        fake_db = MagicMock()
        fake_db.execute = _fake_execute

        builder = ContextPackBuilder.__new__(ContextPackBuilder)
        builder._db = fake_db
        builder._owns_db = False

        async def _noop(*args, **kwargs):
            return None

        # Neutralize sub-builders that need Neo4j / extra services.
        builder._enrich_characters_from_neo4j = _noop
        builder._build_strand_tracker = _noop

        pack = ContextPack()
        await builder._build_facts(pack, "proj-1", chapter_idx)

        expected = render_debt_warning(
            compute_debt_score(fs_rows, chapter_idx)
        )
        assert expected, "precondition: stub rows must trip the debt gate"
        assert pack.foreshadow_triplets, (
            "stub foreshadow rows should reach the foreshadow block"
        )
        assert pack.foreshadow_debt_warning == expected


class _FakeForeshadowRow:
    """Minimal ORM-shaped foreshadow row for stub db sessions."""

    status = "planted"
    type = "plot"
    resolved_chapter = None
    narrative_proximity = 0.5

    def __init__(self, description: str, planted_chapter: int = 0) -> None:
        self.description = description
        self.planted_chapter = planted_chapter
        self.resolve_conditions_json: list = []
        self.resolution_blueprint_json: dict = {}


def _stub_db_with_foreshadows(fs_rows):
    """Async-execute stub: Foreshadow ORM queries return ``fs_rows``,
    everything else returns empty results (every block is fail-safe)."""
    from app.models.project import Foreshadow

    async def _fake_execute(stmt, *args, **kwargs):
        try:
            entities = [d.get("entity") for d in stmt.column_descriptions]
        except Exception:
            entities = []
        rows = fs_rows if Foreshadow in entities else []
        result = MagicMock()
        result.all = MagicMock(return_value=[])
        result.scalar_one_or_none = MagicMock(return_value=None)
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=rows)
        result.scalars = MagicMock(return_value=scalars)
        return result

    fake_db = MagicMock()
    fake_db.execute = _fake_execute
    return fake_db


def _make_builder(fake_db):
    """ContextPackBuilder on a stub db with Neo4j/strand sub-builders nooped."""
    from app.services.context_pack import ContextPackBuilder

    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._db = fake_db
    builder._owns_db = False

    async def _noop(*args, **kwargs):
        return None

    builder._enrich_characters_from_neo4j = _noop
    builder._build_strand_tracker = _noop
    return builder


class TestDebtUsesGlobalChapterIdx:
    """Task A2: foreshadow debt must compare against book-global chapter idx.

    Convention (verified against the codebase, despite docstrings claiming
    0-based): ``Foreshadow.planted_chapter`` is written via
    ``foreshadow_lifecycle.chapter_global_idx(db, pid, vol.volume_idx,
    chapter.chapter_idx)`` (api/generate.py ~L502, chapter_outline_expander
    L238) where ``chapter.chapter_idx`` is the DB value materialized 1-based
    per volume (api/volumes.py ``i + 1``). ``ContextPackBuilder.build()``
    receives that same volume-local DB value, so applying the identical
    conversion (earlier-volume chapter-count offset + local idx) lands in
    the same domain as ``planted_chapter``. Example: vol1 has 10 chapters,
    vol2 ch1 (local 1) -> global 11.

    Before the fix the volume-local idx was fed to ``compute_debt_score``,
    making ages negative from volume 2 onward and silently suppressing all
    debt alerts.
    """

    @pytest.mark.asyncio
    async def test_build_facts_debt_uses_global_chapter_idx(self):
        from app.services.context_pack import ContextPack

        # vol 2 (volume_idx=2) chapter 1 (local chapter_idx=1); vol 1 has
        # 10 chapters -> global idx 11. Three foreshadows planted at global
        # ch3, still "planted": age = 11 - 3 = 8 >= 5 -> 3 criticals ->
        # score 55 < 60 -> debt warning non-empty.
        fs_rows = [_FakeForeshadowRow(f"卷一伏笔{i}", planted_chapter=3) for i in range(3)]
        local_chapter_idx = 1
        global_chapter_idx = 11

        # Regression doc: with the local idx, age = 1 - 3 = -2 -> skipped.
        assert render_debt_warning(
            compute_debt_score(fs_rows, local_chapter_idx)
        ) == "", "precondition: local idx must NOT trip the gate"
        expected = render_debt_warning(
            compute_debt_score(fs_rows, global_chapter_idx)
        )
        assert expected, "precondition: global idx must trip the gate"

        builder = _make_builder(_stub_db_with_foreshadows(fs_rows))
        pack = ContextPack()
        await builder._build_facts(
            pack, "proj-1", local_chapter_idx,
            global_chapter_idx=global_chapter_idx,
        )
        assert pack.foreshadow_debt_warning == expected

    @pytest.mark.asyncio
    async def test_build_wires_resolved_global_idx_into_debt(self):
        """``build()`` must resolve the global idx and feed it to the debt
        computation (mutation guard for the wiring, resolver monkeypatched)."""
        fs_rows = [_FakeForeshadowRow(f"卷一伏笔{i}", planted_chapter=3) for i in range(3)]
        builder = _make_builder(_stub_db_with_foreshadows(fs_rows))

        async def _noop(*args, **kwargs):
            return None

        builder._build_proximity = _noop
        builder._build_rag = _noop

        async def _fake_resolve(project_id, volume_id, chapter_idx):
            return 11

        builder._resolve_global_chapter_idx = _fake_resolve

        pack = await builder.build("proj-1", "vol-2", 1)

        expected = render_debt_warning(compute_debt_score(fs_rows, 11))
        assert expected, "precondition: global idx must trip the gate"
        assert pack.foreshadow_debt_warning == expected

    @pytest.mark.asyncio
    async def test_resolve_global_chapter_idx_adds_prior_volume_offset(self):
        """Resolver = Volume.volume_idx lookup + chapter_global_idx (sum of
        chapter counts of volumes with volume_idx < current, then + local)."""

        async def _fake_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "COUNT(" in str(stmt):
                # get_volume_first_global_idx SQL: vol 1 has 10 chapters.
                result.all = MagicMock(return_value=[(1, 10)])
            else:
                # select(Volume.volume_idx).where(Volume.id == ...)
                result.scalar_one_or_none = MagicMock(return_value=2)
            return result

        fake_db = MagicMock()
        fake_db.execute = _fake_execute
        builder = _make_builder(fake_db)

        gidx = await builder._resolve_global_chapter_idx("proj-1", "vol-2", 1)
        assert gidx == 11

    @pytest.mark.asyncio
    async def test_resolve_global_chapter_idx_fail_safe_returns_local(self):
        """DB error during resolution -> fall back to the local idx (debt may
        under-report but context building never breaks)."""

        async def _boom(stmt, *args, **kwargs):
            raise RuntimeError("db down")

        fake_db = MagicMock()
        fake_db.execute = _boom
        builder = _make_builder(fake_db)

        assert await builder._resolve_global_chapter_idx("proj-1", "vol-2", 7) == 7

    @pytest.mark.asyncio
    async def test_build_survives_resolver_exception_and_uses_local_idx(self):
        """Even if the resolver itself raises, build() must not crash and the
        debt falls back to the old local-idx behavior."""
        fs_rows = [_FakeForeshadowRow(f"超期伏笔{i}", planted_chapter=3) for i in range(3)]
        builder = _make_builder(_stub_db_with_foreshadows(fs_rows))

        async def _noop(*args, **kwargs):
            return None

        builder._build_proximity = _noop
        builder._build_rag = _noop

        async def _broken_resolve(project_id, volume_id, chapter_idx):
            raise RuntimeError("resolver exploded")

        builder._resolve_global_chapter_idx = _broken_resolve

        local_chapter_idx = 20
        pack = await builder.build("proj-1", "vol-2", local_chapter_idx)

        expected = render_debt_warning(
            compute_debt_score(fs_rows, local_chapter_idx)
        )
        assert expected, "precondition: local idx 20 trips the gate"
        assert pack.foreshadow_debt_warning == expected
