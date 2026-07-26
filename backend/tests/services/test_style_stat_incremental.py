"""W14: incremental style/roster recompute.

The recompute task used to reload EVERY chapter's content_text per accepted
chapter (O(n^2) over a book's lifetime) and inflate roster counts via
non-idempotent `appearance_count + c` upserts. These tests pin the fix:

- per-chapter stats (compute_chapter_style_stats) aggregate to EXACTLY the
  whole-book computation (aggregate_style_stats == compute_style_stats),
- staleness selection recomputes only the changed chapter,
- the roster rebuild is idempotent (run twice -> same counts) and preserves
  alias folding,
- the task end-to-end loads content only for stale chapters + the n-gram
  recency window, never the whole book.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services.character_roster import (
    aggregate_appearances,
    count_appearances,
    rebuild_roster,
)
from app.services.style_stat import (
    aggregate_style_stats,
    compute_chapter_style_stats,
    compute_style_stats,
)
from app.tasks.style_tasks import _RECENT_WINDOW, _stale_chapters


# --- fixture: a small book with tics, repeats, empties ----------------------


def _chapter_text(i: int) -> str:
    parts = [f"第{i}章开场，主角萧炎踏入了第{i}处秘境之中。"]
    if i % 3 == 0:
        parts.append("他不是不想退，而是无路可退。")
    if i % 4 == 0:
        parts.append("这是一句横跨多章逐字重复的长句子。")
    if i % 5 == 0:
        parts.insert(0, "清晨，天光微亮。")
    parts.append("江湖夜雨十年灯。" * 3)
    if i % 2 == 0:
        parts.append("他走了。")
    return "".join(parts)


def _book(n: int = 25) -> list[tuple[int, str]]:
    # every 7th chapter is empty (unwritten) -- must not be counted.
    return [(i, "" if i % 7 == 0 else _chapter_text(i)) for i in range(1, n + 1)]


def _aggregate_inputs(chapters):
    """Build (per_chapter_rows, recent_texts) the way the task does."""
    rows = [
        (idx, compute_chapter_style_stats(text))
        for idx, text in chapters
        if compute_chapter_style_stats(text) is not None
    ]
    recent = sorted(chapters_nonempty := [(i, t) for i, t in chapters if t and t.strip()])[-_RECENT_WINDOW:]
    return rows, [t for _i, t in recent]


# --- per-chapter stats ------------------------------------------------------


def test_compute_chapter_style_stats_empty_returns_none():
    assert compute_chapter_style_stats("") is None
    assert compute_chapter_style_stats("   \n  ") is None


def test_compute_chapter_style_stats_shape():
    s = compute_chapter_style_stats(_chapter_text(3))
    assert s is not None
    assert s["tics"]["corrective_not_but"] == 1
    assert "他不是不想退，而是无路可退" in s["sentences"]
    # sentences are deduped and >= 10 chars ("江湖夜雨十年灯" is 7 -> dropped)
    assert "江湖夜雨十年灯" not in s["sentences"]
    assert s["last_sentence_len"] is not None
    assert s["opening_time"] is False
    assert compute_chapter_style_stats(_chapter_text(5))["opening_time"] is True


# --- aggregate == whole-book computation ------------------------------------


def test_aggregate_equals_whole_book_computation():
    chapters = _book()
    names = {"萧炎"}
    rows, recent_texts = _aggregate_inputs(chapters)
    assert aggregate_style_stats(rows, recent_texts, names) == compute_style_stats(
        chapters, names
    )


def test_aggregate_after_single_chapter_edit_converges():
    """Editing one chapter -> replace just that chapter's row -> same result
    as recomputing the whole book from scratch."""
    chapters = _book()
    names = {"萧炎"}
    rows, _ = _aggregate_inputs(chapters)

    edited = dict(chapters)
    edited[10] = _chapter_text(10) + "他不是慌了，而是彻底怒了。沉默了片刻。"
    new_chapters = sorted(edited.items())

    rows = [
        (idx, s) if idx != 10 else (10, compute_chapter_style_stats(edited[10]))
        for idx, s in rows
    ]
    _, recent_texts = _aggregate_inputs(new_chapters)
    assert aggregate_style_stats(rows, recent_texts, names) == compute_style_stats(
        new_chapters, names
    )


def test_aggregate_empty():
    assert aggregate_style_stats([], [], set()) == {"chapter_count": 0}


# --- staleness selection: only the changed chapter --------------------------

_T0 = dt.datetime(2026, 7, 26, 12, 0, tzinfo=dt.timezone.utc)
_T1 = dt.datetime(2026, 7, 26, 13, 0, tzinfo=dt.timezone.utc)


def test_stale_chapters_only_changed_one():
    meta = [("c1", 1, _T0), ("c2", 2, _T0), ("c3", 3, _T1)]
    existing = {"c1": (_T0, 1), "c2": (_T0, 2), "c3": (_T0, 3)}  # c3 edited
    assert _stale_chapters(meta, existing) == [("c3", 3, _T1)]


def test_stale_chapters_missing_row_and_moved_idx():
    meta = [("c1", 1, _T0), ("c2", 5, _T0), ("c4", 4, _T0)]
    existing = {"c1": (_T0, 1), "c2": (_T0, 2)}  # c2 moved, c4 new
    assert _stale_chapters(meta, existing) == [("c2", 5, _T0), ("c4", 4, _T0)]


def test_stale_chapters_full_marks_all():
    meta = [("c1", 1, _T0), ("c2", 2, _T0)]
    existing = {"c1": (_T0, 1), "c2": (_T0, 2)}
    assert _stale_chapters(meta, existing) == []
    assert _stale_chapters(meta, existing, full=True) == meta


# --- roster: idempotent aggregation + alias folding -------------------------


def test_aggregate_appearances_min_max_sum():
    per_chapter = [
        (3, {"老王": 2}),
        (7, {"老王": 1, "小李": 4}),
        (5, {"老王": 3}),
    ]
    totals = aggregate_appearances(per_chapter)
    assert totals["老王"] == {"first_seen": 3, "last_seen": 7, "count": 6}
    assert totals["小李"] == {"first_seen": 7, "last_seen": 7, "count": 4}


def test_aggregate_appearances_idempotent():
    per_chapter = [(1, {"老王": 2}), (2, {"老王": 5})]
    once = aggregate_appearances(per_chapter)
    twice = aggregate_appearances(per_chapter)
    assert once == twice == {"老王": {"first_seen": 1, "last_seen": 2, "count": 7}}


def test_alias_folding_preserved_through_per_chapter_counts():
    alias_map = {"萧炎": ["炎帝"]}
    per_chapter = [
        (1, count_appearances("萧炎出手。", {"萧炎"}, alias_map)),
        (2, count_appearances("炎帝一怒，炎帝再怒。", {"萧炎"}, alias_map)),
    ]
    totals = aggregate_appearances(per_chapter)
    assert totals == {"萧炎": {"first_seen": 1, "last_seen": 2, "count": 3}}


def _capture_db(executed):
    db = MagicMock()

    async def _exec(stmt):
        executed.append(stmt.compile(dialect=postgresql.dialect()))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_exec)
    return db


@pytest.mark.asyncio
async def test_rebuild_roster_twice_writes_identical_absolute_counts():
    per_chapter = [(1, {"老王": 2}), (4, {"老王": 1})]

    async def _run():
        executed = []
        await rebuild_roster(_capture_db(executed), "p1", per_chapter)
        return executed

    first, second = await _run(), await _run()
    params1 = [c.params for c in first]
    params2 = [c.params for c in second]
    assert params1 == params2  # convergent, not additive
    upsert = first[0]
    assert upsert.params["appearance_count"] == 3
    assert upsert.params["first_seen_chapter"] == 1
    assert upsert.params["last_seen_chapter"] == 4
    assert "appearance_count + " not in str(upsert)


@pytest.mark.asyncio
async def test_rebuild_roster_filters_deleted_names_and_prunes_stale_rows():
    executed = []
    await rebuild_roster(
        _capture_db(executed), "p1",
        [(1, {"老王": 2, "已删角色": 9})],
        valid_names={"老王"},
    )
    inserts = [c for c in executed if "INSERT INTO character_appearances" in str(c)]
    deletes = [c for c in executed if "DELETE FROM character_appearances" in str(c)]
    assert len(inserts) == 1 and inserts[0].params["character_name"] == "老王"
    assert len(deletes) == 1  # rows outside the surviving set are removed


# --- task end-to-end: dispatch recomputes only the stale chapter ------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Routes the task's statements against an in-memory store, persisting
    chapter_style_stats upserts so a second run sees the cached rows."""

    def __init__(self, store):
        self.store = store
        self.content_selects: list[list] = []
        self.stat_upserts: list[dict] = []
        self.roster_upserts: list[dict] = []
        self.style_upserts: list[dict] = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.committed = True

    async def execute(self, stmt):
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params
        if "INSERT INTO chapter_style_stats" in sql:
            self.stat_upserts.append(dict(params))
            self.store["cached"][params["chapter_id"]] = (
                params["chapter_id"], params["global_idx"],
                params["stats_json"], params["appearances_json"],
                params["source_updated_at"],
            )
            return _FakeResult([])
        if "INSERT INTO character_appearances" in sql:
            self.roster_upserts.append(dict(params))
            return _FakeResult([])
        if "INSERT INTO style_stats" in sql:
            self.style_upserts.append(dict(params))
            return _FakeResult([])
        if sql.startswith("DELETE"):
            return _FakeResult([])
        if "FROM volumes" in sql:
            return _FakeResult(self.store["volumes"])
        if "chapters.chapter_idx" in sql:
            return _FakeResult(self.store["meta"])
        if "chapter_style_stats.source_updated_at" in sql:
            return _FakeResult(
                [(cid, src, gidx) for cid, gidx, _s, _a, src in self.store["cached"].values()]
            )
        if "chapter_style_stats.stats_json" in sql:
            return _FakeResult(
                [(cid, gidx, s, a) for cid, gidx, s, a, _src in self.store["cached"].values()]
            )
        if "chapters.content_text" in sql:
            ids = [v for v in params.values() if isinstance(v, list)]
            ids = ids[0] if ids else []
            self.content_selects.append(list(ids))
            return _FakeResult(
                [(cid, self.store["contents"][cid]) for cid in ids if cid in self.store["contents"]]
            )
        if "profile_json" in sql:
            return _FakeResult([])  # no aliases
        if "FROM characters" in sql:
            return _FakeResult([("萧炎",)])
        if "FROM locations" in sql or "FROM organizations" in sql:
            return _FakeResult([])
        raise AssertionError(f"unrouted statement: {sql[:120]}")


@pytest.mark.asyncio
async def test_task_recomputes_only_stale_chapter_and_aggregates(monkeypatch):
    from app.tasks.style_tasks import _recompute_style_stats_async

    t1, t2 = _chapter_text(1), _chapter_text(2)
    t3 = _chapter_text(3) + "萧炎与萧炎的对手先后离场。"
    contents = {"ch1": t1, "ch2": t2, "ch3": t3}
    meta = [
        ("ch1", 1, 1, _T0),
        ("ch2", 2, 2, _T0),
        ("ch3", 3, 3, _T1),  # just accepted -> no cached row yet
    ]
    store = {
        "volumes": [("v1", 1)],
        "meta": meta,
        "contents": contents,
        "cached": {
            "ch1": ("ch1", 1, compute_chapter_style_stats(t1),
                    count_appearances(t1, {"萧炎"}), _T0),
            "ch2": ("ch2", 2, compute_chapter_style_stats(t2),
                    count_appearances(t2, {"萧炎"}), _T0),
        },
    }
    db = _FakeDB(store)
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: db)

    out = await _recompute_style_stats_async("p1", "test")

    # Only ch3's content was loaded for recompute; the other content pull is
    # the O(window) n-gram recency window, never a whole-book reload.
    assert out["recomputed_chapters"] == 1
    assert db.content_selects[0] == ["ch3"]
    assert len(db.stat_upserts) == 1
    assert db.stat_upserts[0]["chapter_id"] == "ch3"
    assert db.committed

    # Whole-book aggregate written from cached rows == direct computation.
    expected = compute_style_stats([(1, t1), (2, t2), (3, t3)], {"萧炎"})
    assert db.style_upserts[0]["stats_json"] == expected
    assert out["chapter_count"] == 3

    # Roster rebuilt with absolute, alias-safe totals.
    roster = {u["character_name"]: u for u in db.roster_upserts}
    expected_totals = aggregate_appearances(
        [(i, count_appearances(t, {"萧炎"})) for i, t in [(1, t1), (2, t2), (3, t3)]]
    )
    assert roster["萧炎"]["appearance_count"] == expected_totals["萧炎"]["count"]
    assert roster["萧炎"]["first_seen_chapter"] == expected_totals["萧炎"]["first_seen"]
    assert roster["萧炎"]["last_seen_chapter"] == expected_totals["萧炎"]["last_seen"]

    # Second run: nothing stale -> no per-chapter recompute, no extra content
    # beyond the recency window, and the roster counts DO NOT inflate.
    db2 = _FakeDB(store)
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: db2)
    out2 = await _recompute_style_stats_async("p1", "test")
    assert out2["recomputed_chapters"] == 0
    assert len(db2.stat_upserts) == 0
    assert len(db2.content_selects) == 1  # recency window only
    assert db2.style_upserts[0]["stats_json"] == expected
    roster2 = {u["character_name"]: u for u in db2.roster_upserts}
    assert roster2["萧炎"]["appearance_count"] == roster["萧炎"]["appearance_count"]
