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


# Source resolution and versioned projection now have real PG/Neo4j coverage
# in test_entity_projection.py.

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
async def test_timeline_anchors_and_locations_query_global_axis(monkeypatch):
    from app.services.context_pack import ContextPack, ContextPackBuilder
    from app.services.entity_timeline import WorldSnapshot
    from tests.support.world_rules import install_rule_graph
    install_rule_graph(monkeypatch, PID)
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    # This test covers SQL chronology. Supply a successful empty graph result
    # instead of relying on the product swallowing an uninitialized driver.
    graph = object()
    async def driver():
        yield graph
    snapshot = AsyncMock(return_value=WorldSnapshot())
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    monkeypatch.setattr('app.services.entity_context.verified_world_snapshot', snapshot)

    # DB returns rows ordered on the global axis, spanning two volumes.
    rows = [
        SimpleNamespace(global_idx=149, summary="主角在第一卷末尾突破境界成功"),
        SimpleNamespace(global_idx=150, summary="第一卷终章：宗门大比落幕收官"),
        SimpleNamespace(global_idx=751, summary="第二卷开篇：主角抵达北境边城"),
    ]
    db = _CtxFakeDB(rows)
    builder = ContextPackBuilder(db=db)
    pack = ContextPack(history_before_global_idx=752)

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
    assert "completed" in db.captured["timeline"].values()
    timeline_sql = next(sql for sql in db.sqls if "summary_source_hash" in sql)
    assert "chapters.global_idx < " in timeline_sql

    # character_locations "current location" also compares at global 752.
    assert 752 in db.captured["character_locations"].values()
    assert 2 not in db.captured["character_locations"].values()
    snapshot.assert_awaited_once_with(db, graph, PID, 752)


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
        db, PID, None, 752, outline, min_signals=2, state_window=3,
        historical_roster=[], foreshadow_references=[{
            'description': '灰袍人袖口的青铜齿轮印记', 'planted_chapter': 5,
            'source_state': 'current', 'status': 'planted',
        }],
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


@pytest.mark.asyncio
async def test_generate_summary_delegates_scoped_source():
    from app.services import hook_manager as hm
    from app.services import chapter_summarizer as cs

    db = MagicMock()
    helper = AsyncMock(return_value=(True, "摘要"))
    mgr = hm.HookManager(db=db)
    with patch.object(cs, "summarize_scoped_chapter", helper):
        await mgr._generate_summary(PID, VOL2_ID, 1, "正文……")
    helper.assert_awaited_once_with(
        db=db, project_id=PID, volume_id=VOL2_ID,
        chapter_idx=1, chapter_text="正文……",
    )


# ---------------------------------------------------------------------------
# 6b. hook_manager._resolve_global_idx: local -> global conversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_manager_reads_persisted_global_idx_for_volume_two():
    from app.services.hook_manager import HookManager

    db = SimpleNamespace(execute=AsyncMock(return_value=_FakeResult([(913,)])))
    mgr = HookManager(db=db)
    assert await mgr._resolve_global_idx(PID, VOL2_ID, 3) == 913
    query = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "chapters.global_idx" in str(query)
    assert "volumes.project_id =" in str(query) and uuid.UUID(PID) in query.params.values()
    assert uuid.UUID(VOL2_ID) in query.params.values() and 3 in query.params.values()


@pytest.mark.asyncio
async def test_hook_manager_global_idx_failure_does_not_fall_back_to_local():
    from app.services.hook_manager import HookManager
    from app.services.context_errors import RequiredContextUnavailable

    db = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("db down")))
    with pytest.raises(RequiredContextUnavailable):
        await HookManager(db=db)._resolve_global_idx(PID, VOL2_ID, 3)
