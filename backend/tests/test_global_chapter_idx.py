"""Book-global chapter axis regression tests (chapters.global_idx).

``Chapter.chapter_idx`` is volume-local (1-based per volume), but half the
memory subsystem used to compare it project-wide. These tests guard the fix,
which puts every cross-volume comparison on one canonical axis —
``Chapter.global_idx`` (= chapters in lower-volume_idx volumes + local idx,
same convention as ``foreshadow_lifecycle.chapter_global_idx``; global ==
local for single-volume projects):

1. ``_set_chapter_global_idx`` (models/project.py) stamps the column on
   insert; migration a1001915 backfills existing rows with the same formula.
2. ExtractionMarker keys no longer collide across volumes: entity_tasks
   claims markers on the global idx, so a completed volume-1 marker cannot
   swallow volume-2's extraction (the "extraction stops at volume 2" bug).
3. ``_resolve_chapter`` loads the RIGHT chapter (by chapter_id, not by an
   ambiguous project-wide local idx) and extract_and_update runs on the
   global idx (so Neo4j / character_states.chapter_start are global).
4. Timeline anchors and the strand tracker query/order on global_idx
   (volume-local idx interleaved all volumes into a fake chronology).
5. character_locations "current location" comparisons use the global idx.
6. HookManager._generate_summary scopes its chapter lookup by volume_id
   (project-wide idx match raised MultipleResultsFound from volume 2 on).

All tests run offline (fake Neo4j driver + fake async sessions), following
the patterns of tests/test_foreshadow_tracking_chain.py.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import MultipleResultsFound

PID = "f14712d6-6dc6-4cfb-b05f-e107fa02b63d"
VOL1_ID = str(uuid.uuid4())
VOL2_ID = str(uuid.uuid4())
CH_V1_1 = str(uuid.uuid4())  # volume 1, local idx 1, global 1
CH_V2_1 = str(uuid.uuid4())  # volume 2, local idx 1, global 751


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def scalar(self):
        if self._scalar is not None:
            return self._scalar
        row = self.first()
        return row[0] if row else None

    def scalar_one_or_none(self):
        if len(self._rows) > 1:
            raise MultipleResultsFound(
                "Multiple rows were found when one or none was required"
            )
        row = self.first()
        if row is None:
            return None
        return row[0] if isinstance(row, (tuple, list)) else row


def _compile(stmt):
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


# ---------------------------------------------------------------------------
# 1. Insert-time stamping: _set_chapter_global_idx
# ---------------------------------------------------------------------------


class _FakeConn:
    """Sync connection stub for the before_insert listener."""

    def __init__(self, base):
        self._base = base
        self.calls = 0

    def execute(self, stmt):
        self.calls += 1
        return _FakeResult(scalar=self._base)


def test_listener_single_volume_global_equals_local():
    from app.models.project import Chapter, _set_chapter_global_idx

    ch = Chapter(volume_id=VOL1_ID, chapter_idx=5, title="第5章")
    _set_chapter_global_idx(None, _FakeConn(base=0), ch)
    assert ch.global_idx == 5


def test_listener_second_volume_offsets_by_earlier_chapters():
    from app.models.project import Chapter, _set_chapter_global_idx

    ch = Chapter(volume_id=VOL2_ID, chapter_idx=1, title="第1章")
    _set_chapter_global_idx(None, _FakeConn(base=750), ch)
    assert ch.global_idx == 751


def test_listener_respects_preset_value():
    from app.models.project import Chapter, _set_chapter_global_idx

    ch = Chapter(volume_id=VOL2_ID, chapter_idx=1, title="第1章", global_idx=99)
    conn = _FakeConn(base=750)
    _set_chapter_global_idx(None, conn, ch)
    assert ch.global_idx == 99
    assert conn.calls == 0


def test_listener_fails_open_to_null():
    from app.models.project import Chapter, _set_chapter_global_idx

    class _BoomConn:
        def execute(self, stmt):
            raise RuntimeError("db down")

    ch = Chapter(volume_id=VOL2_ID, chapter_idx=1, title="第1章")
    _set_chapter_global_idx(None, _BoomConn(), ch)
    assert ch.global_idx is None


def test_migration_chains_onto_a1001914():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "a1001915_chapter_global_idx.py"
    )
    spec = importlib.util.spec_from_file_location("a1001915_chapter_global_idx", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    assert mig.revision == "a1001915"
    assert mig.down_revision == "a1001914"
    # Backfill uses the same base+local formula as chapter_global_idx.
    assert "global_idx" in " ".join(
        str(c) for c in mig.upgrade.__code__.co_consts if isinstance(c, str)
    )


# ---------------------------------------------------------------------------
# 2+3. entity_tasks: chapter resolution + marker keyed on global idx
# ---------------------------------------------------------------------------


_CHAPTER_ROWS = [
    # id, chapter_idx (local), global_idx, content_text, volume_idx
    {"id": CH_V1_1, "chapter_idx": 1, "global_idx": 1,
     "content_text": "第一卷第一章正文", "volume_idx": 1},
    {"id": CH_V2_1, "chapter_idx": 1, "global_idx": 751,
     "content_text": "第二卷第一章正文", "volume_idx": 2},
]


class _ResolveFakeDB:
    """Emulates the chapters/volumes join used by _resolve_chapter."""

    def __init__(self, rows, vol_counts=None):
        self.rows = [dict(r) for r in rows]
        # rows for foreshadow_lifecycle.get_volume_first_global_idx:
        # [(volume_idx, chapter_count), ...]
        self.vol_counts = list(vol_counts or [])
        self.sqls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM volumes v" in sql:  # raw-text volume-count query
            self.sqls.append(sql)
            return _FakeResult(self.vol_counts)
        sql, p = _compile(stmt)
        self.sqls.append(sql)
        cand = list(self.rows)
        if "chapters.id" in sql:
            wanted = {str(v) for v in p.values()}
            cand = [r for r in cand if r["id"] in wanted]
        elif "chapters.chapter_idx" in sql:
            idxs = {v for v in p.values() if isinstance(v, int)}
            cand = [r for r in cand if r["chapter_idx"] in idxs]
            if "ORDER BY volumes.volume_idx DESC" in sql:
                cand.sort(key=lambda r: -r["volume_idx"])
        return _FakeResult(
            [
                (r["global_idx"], r["chapter_idx"], r["content_text"], r["volume_idx"])
                for r in cand
            ]
        )


@pytest.mark.asyncio
async def test_resolve_chapter_by_chapter_id_picks_right_volume(monkeypatch):
    from app.db import session as session_mod
    from app.tasks.entity_tasks import _resolve_chapter

    db = _ResolveFakeDB(_CHAPTER_ROWS)
    monkeypatch.setattr(session_mod, "async_session_factory", lambda: db)

    resolved = await _resolve_chapter(PID, 1, CH_V2_1)
    assert resolved == (751, "第二卷第一章正文")


@pytest.mark.asyncio
async def test_resolve_chapter_without_id_prefers_highest_volume(monkeypatch):
    """Legacy id-less dispatches come from the volume being written; the
    old code picked an arbitrary volume's chapter N (no ORDER BY at all)."""
    from app.db import session as session_mod
    from app.tasks.entity_tasks import _resolve_chapter

    db = _ResolveFakeDB(_CHAPTER_ROWS)
    monkeypatch.setattr(session_mod, "async_session_factory", lambda: db)

    resolved = await _resolve_chapter(PID, 1, None)
    assert resolved == (751, "第二卷第一章正文")
    assert any("ORDER BY volumes.volume_idx DESC" in s for s in db.sqls)


@pytest.mark.asyncio
async def test_resolve_chapter_null_global_idx_falls_back_to_computation(monkeypatch):
    """Rows predating the backfill (global_idx NULL) still resolve via
    foreshadow_lifecycle.chapter_global_idx (750 earlier chapters + local 1)."""
    from app.db import session as session_mod
    from app.tasks.entity_tasks import _resolve_chapter

    rows = [dict(_CHAPTER_ROWS[1], global_idx=None)]
    db = _ResolveFakeDB(rows, vol_counts=[(1, 750)])
    monkeypatch.setattr(session_mod, "async_session_factory", lambda: db)

    resolved = await _resolve_chapter(PID, 1, CH_V2_1)
    assert resolved == (751, "第二卷第一章正文")


@pytest.mark.asyncio
async def test_resolve_chapter_missing_returns_none(monkeypatch):
    from app.db import session as session_mod
    from app.tasks.entity_tasks import _resolve_chapter

    db = _ResolveFakeDB([])
    monkeypatch.setattr(session_mod, "async_session_factory", lambda: db)

    assert await _resolve_chapter(PID, 9, None) is None


class _MarkerNeoSession:
    """Records ExtractionMarker cypher; claim status keyed by global idx."""

    def __init__(self, driver):
        self._driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **kwargs):
        self._driver.calls.append((query, kwargs))
        result = MagicMock()
        if "MERGE (m:ExtractionMarker" in query:
            status = self._driver.statuses.get(kwargs.get("idx"), "new")
            result.single = AsyncMock(return_value={"status": status})
        else:
            result.single = AsyncMock(return_value=None)
        return result


class _MarkerNeoDriver:
    def __init__(self, statuses=None):
        self.statuses = dict(statuses or {})
        self.calls: list[tuple[str, dict]] = []

    def session(self, *args, **kwargs):
        return _MarkerNeoSession(self)

    def marker_calls(self, fragment):
        return [
            (q, kw) for q, kw in self.calls
            if "ExtractionMarker" in q and fragment in q
        ]


@pytest.fixture
def _marker_env(monkeypatch):
    """Offline harness for _extract_chapter_async marker/global-idx tests."""
    from app.db import neo4j as neo4j_mod
    from app.services import entity_timeline as et_mod
    from app.tasks import entity_tasks

    driver = _MarkerNeoDriver()
    monkeypatch.setattr(neo4j_mod, "init_neo4j", AsyncMock())
    monkeypatch.setattr(neo4j_mod, "_driver", driver)

    fake_service = MagicMock()
    fake_service.initialize_graph = AsyncMock()
    fake_service.extract_and_update = AsyncMock()
    monkeypatch.setattr(
        et_mod, "EntityTimelineService", MagicMock(return_value=fake_service)
    )
    monkeypatch.setattr(
        entity_tasks, "_materialize_entities_to_postgres",
        AsyncMock(return_value={}),
    )
    return SimpleNamespace(driver=driver, service=fake_service)


@pytest.mark.asyncio
async def test_marker_and_extraction_use_global_idx(_marker_env, monkeypatch):
    """Volume-2 chapter 1 must claim marker idx=751 and extract at 751."""
    from app.tasks import entity_tasks

    monkeypatch.setattr(
        entity_tasks, "_resolve_chapter",
        AsyncMock(return_value=(751, "第二卷第一章正文")),
    )
    result = await entity_tasks._extract_chapter_async(
        project_id=PID, chapter_idx=1, caller="test", chapter_id=CH_V2_1
    )

    assert result["status"] == "ok"
    assert result["global_idx"] == 751

    claims = _marker_env.driver.marker_calls("MERGE")
    assert len(claims) == 1
    assert claims[0][1]["idx"] == 751

    _marker_env.service.extract_and_update.assert_awaited_once_with(
        PID, 751, "第二卷第一章正文"
    )

    completes = [
        (q, kw) for q, kw in _marker_env.driver.calls
        if "'completed'" in q and "ExtractionMarker" in q
    ]
    assert len(completes) == 1
    assert completes[0][1]["idx"] == 751


@pytest.mark.asyncio
async def test_completed_vol1_marker_does_not_block_vol2(_marker_env, monkeypatch):
    """THE volume-2 bug: vol-1 ch-1's completed marker (idx=1) used to make
    vol-2 ch-1 (same local idx) a permanent no-op. On the global axis the
    keys differ (1 vs 751), so extraction proceeds."""
    from app.tasks import entity_tasks

    _marker_env.driver.statuses[1] = "completed"  # vol-1 ch-1 already done

    monkeypatch.setattr(
        entity_tasks, "_resolve_chapter",
        AsyncMock(return_value=(751, "第二卷第一章正文")),
    )
    result = await entity_tasks._extract_chapter_async(
        project_id=PID, chapter_idx=1, caller="test", chapter_id=CH_V2_1
    )

    assert result["status"] == "ok"
    _marker_env.service.extract_and_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_marker_still_skips_same_chapter(_marker_env, monkeypatch):
    """Idempotency preserved: re-dispatching the SAME chapter skips."""
    from app.tasks import entity_tasks

    _marker_env.driver.statuses[1] = "completed"

    monkeypatch.setattr(
        entity_tasks, "_resolve_chapter",
        AsyncMock(return_value=(1, "第一卷第一章正文")),
    )
    result = await entity_tasks._extract_chapter_async(
        project_id=PID, chapter_idx=1, caller="test", chapter_id=CH_V1_1
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "already_completed"
    _marker_env.service.extract_and_update.assert_not_awaited()
    # PG read models still converge on the skip path.
    entity_tasks._materialize_entities_to_postgres.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4+5. context_pack: timeline anchors + character_locations on global axis
# ---------------------------------------------------------------------------


class _CtxFakeDB:
    """Answers only the queries under test; everything else is empty."""

    def __init__(self, timeline_rows):
        self.timeline_rows = list(timeline_rows)
        self.sqls: list[str] = []
        self.captured: dict[str, dict] = {}

    async def execute(self, stmt, params=None):
        try:
            sql, p = _compile(stmt)
        except Exception:
            sql, p = str(stmt), dict(params or {})
        self.sqls.append(sql)
        if "character_locations" in sql:
            self.captured["character_locations"] = p
            return _FakeResult([])
        if "chapters" in sql and "global_idx" in sql and "summary" in sql:
            self.captured["timeline"] = p
            return _FakeResult(self.timeline_rows)
        return _FakeResult([])


@pytest.mark.asyncio
async def test_timeline_anchors_and_locations_query_global_axis():
    from app.services.context_pack import ContextPack, ContextPackBuilder

    # DB returns rows ordered on the global axis, spanning two volumes.
    rows = [
        SimpleNamespace(global_idx=149, summary="主角在第一卷末尾突破境界成功"),
        SimpleNamespace(global_idx=150, summary="第一卷终章：宗门大比落幕收官"),
        SimpleNamespace(global_idx=751, summary="第二卷开篇：主角抵达北境边城"),
    ]
    db = _CtxFakeDB(rows)
    builder = ContextPackBuilder(db=db)
    pack = ContextPack()

    with patch(
        "app.services.strand_tracker.StrandTrackerService"
    ) as mock_tracker_cls:
        mock_tracker_cls.return_value.analyze_strands = AsyncMock(
            return_value=pack.strand_tracker
        )
        await builder._build_facts(pack, PID, chapter_idx=2, global_chapter_idx=752)

    # Timeline anchors carry monotonic BOOK-GLOBAL indices (no interleaving
    # of each volume's local 1..N ranges).
    anchor_idxs = [a.chapter_idx for a in pack.timeline_anchors]
    assert anchor_idxs == [149, 150, 751]
    assert anchor_idxs == sorted(anchor_idxs)

    # The timeline query filtered on the global idx (752), not local (2).
    assert 752 in db.captured["timeline"].values()
    assert 2 not in db.captured["timeline"].values()

    # character_locations "current location" also compares at global 752.
    assert 752 in db.captured["character_locations"].values()
    assert 2 not in db.captured["character_locations"].values()


# ---------------------------------------------------------------------------
# 4b. strand tracker: global window, global last-appearance indices
# ---------------------------------------------------------------------------


class _StrandFakeDB:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sqls: list[str] = []
        self.params: dict = {}

    async def execute(self, stmt, params=None):
        sql, p = _compile(stmt)
        self.sqls.append(sql)
        self.params = p
        return _FakeResult(self.rows)


@pytest.mark.asyncio
async def test_strand_tracker_queries_and_reports_global_idx():
    from app.services.strand_tracker import StrandTrackerService

    rows = [
        SimpleNamespace(
            global_idx=748,
            summary="主角接下任务，与对手战斗，突破危机",
            content_text="", outline_json={},
        ),
        SimpleNamespace(
            global_idx=750,
            summary="兄弟重逢相拥，彼此信任，心疼落泪",
            content_text="", outline_json={},
        ),
    ]
    db = _StrandFakeDB(rows)
    svc = StrandTrackerService(db=db)

    tracker = await svc.analyze_strands(PID, 752, lookback=20)

    sql = db.sqls[0]
    assert "global_idx" in sql
    assert "JOIN volumes" in sql
    # Window is [752-20, 752] on the global axis.
    assert 752 in db.params.values()
    assert 732 in db.params.values()

    # Last-appearance chapters are book-global, so gap arithmetic against a
    # global current idx stays meaningful.
    assert tracker.last_quest_chapter == 748
    assert tracker.last_fire_chapter == 750


# ---------------------------------------------------------------------------
# 5b. related_chapters: state-signal path re-enabled on the global axis
# ---------------------------------------------------------------------------


class _RelatedFakeDB:
    def __init__(self):
        self.sqls: list[str] = []
        self.state_params: dict = {}

    async def execute(self, stmt, params=None):
        sql, p = _compile(stmt)
        self.sqls.append(sql)
        if "count" in sql.lower() and "chapters" in sql:
            return _FakeResult(scalar=40)
        if "foreshadows" in sql:
            return _FakeResult([("灰袍人袖口的青铜齿轮印记", 5)])
        if "character_appearances" in sql:
            return _FakeResult([])
        if "character_states" in sql:
            self.state_params = p
            return _FakeResult([("林昭", 750)])
        if "chapters" in sql and "global_idx" in sql:
            return _FakeResult([("章节摘要", "章节标题")])
        return _FakeResult([])


@pytest.mark.asyncio
async def test_related_chapters_state_signal_uses_global_window():
    from app.services.related_chapters import find_related_chapters

    db = _RelatedFakeDB()
    outline = {"summary": "主角想起灰袍人袖口的青铜齿轮印记，林昭同行北上"}

    ranked = await find_related_chapters(
        db, PID, None, 752, outline, min_signals=2, state_window=3
    )

    # The state look-back window ran on the global axis around 752.
    assert 752 in db.state_params.values() or 749 in db.state_params.values()

    by_chapter = {e["chapter"]: e for e in ranked}
    assert 750 in by_chapter  # recent state change (global idx)
    assert 5 in by_chapter    # foreshadow planted chapter (already global)
    assert any("状态" in r for r in by_chapter[750]["reasons"])


# ---------------------------------------------------------------------------
# 6. hook_manager._generate_summary: volume-scoped chapter lookup
# ---------------------------------------------------------------------------


class _SummaryFakeDB:
    """Two volumes carry the same local chapter_idx. The fake honors the
    filters present in the compiled SQL: without the volume_id filter, both
    rows match and scalar_one_or_none raises MultipleResultsFound — exactly
    the pre-fix failure mode."""

    def __init__(self, chapters):
        self.chapters = list(chapters)
        self.committed = False
        self.sqls: list[str] = []

    async def execute(self, stmt, params=None):
        sql, p = _compile(stmt)
        self.sqls.append(sql)
        rows = list(self.chapters)
        if "chapters.volume_id" in sql:
            wanted = {str(v) for v in p.values()}
            rows = [c for c in rows if str(c.volume_id) in wanted]
        if "chapters.chapter_idx" in sql:
            idxs = {v for v in p.values() if isinstance(v, int)}
            rows = [c for c in rows if c.chapter_idx in idxs]
        return _FakeResult(rows)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_generate_summary_scopes_lookup_by_volume():
    from app.services import hook_manager as hm

    ch_v1 = SimpleNamespace(volume_id=VOL1_ID, chapter_idx=1, summary=None)
    ch_v2 = SimpleNamespace(volume_id=VOL2_ID, chapter_idx=1, summary=None)
    db = _SummaryFakeDB([ch_v1, ch_v2])

    fake_router = MagicMock()
    fake_router.generate_with_tier_fallback = AsyncMock(
        return_value=MagicMock(text="第二卷第一章：主角抵达北境。")
    )

    mgr = hm.HookManager(db=db)
    with patch.object(
        hm, "get_model_router_async", new=AsyncMock(return_value=fake_router)
    ):
        # Would raise MultipleResultsFound without the volume_id filter.
        await mgr._generate_summary(
            project_id=PID,
            volume_id=VOL2_ID,
            chapter_idx=1,
            chapter_text="正文……",
        )

    assert ch_v2.summary == "第二卷第一章：主角抵达北境。"
    assert ch_v1.summary is None
    assert db.committed is True


# ---------------------------------------------------------------------------
# 6b. hook_manager._resolve_global_idx: local -> global conversion
# ---------------------------------------------------------------------------


class _GlobalIdxFakeDB:
    """Returns volume_idx for the volume lookup and (volume_idx, count)
    pairs for the raw-text earlier-volume chapter-count query."""

    def __init__(self, volume_idx, vol_counts):
        self.volume_idx = volume_idx
        self.vol_counts = list(vol_counts)

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM volumes v" in sql:
            return _FakeResult(self.vol_counts)
        return _FakeResult([(self.volume_idx,)])


@pytest.mark.asyncio
async def test_hook_manager_resolves_global_idx_for_volume_two():
    from app.services.hook_manager import HookManager

    db = _GlobalIdxFakeDB(volume_idx=2, vol_counts=[(1, 750)])
    mgr = HookManager(db=db)
    assert await mgr._resolve_global_idx(PID, VOL2_ID, 3) == 753


@pytest.mark.asyncio
async def test_hook_manager_global_idx_fail_safe_falls_back_to_local():
    from app.services.hook_manager import HookManager

    class _BoomDB:
        async def execute(self, stmt, params=None):
            raise RuntimeError("db down")

    mgr = HookManager(db=_BoomDB())
    assert await mgr._resolve_global_idx(PID, VOL2_ID, 3) == 3
