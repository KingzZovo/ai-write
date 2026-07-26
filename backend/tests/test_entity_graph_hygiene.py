"""Entity-graph hygiene regression tests.

Guards against:
  1. Duplicate RELATES_TO edges: add/update_relationship must MERGE on the
     (project_id, source_name, target_name, type) key (with the constraint
     properties actually SET), never CREATE — pre-fix, every extraction run
     stacked null-property duplicate edges that all got injected into
     prompts. Plus the one-off cleanup for legacy duplicates.
  2. Entity alias duplication: an incoming name matching an existing
     character's profile_json.aliases (「炎帝」 -> 「萧炎」) must fold into
     the canonical character in PG materialization and in the roster
     appearance counts, not create a second character.
  3. World-rule near-duplicate accumulation: a re-worded rule (same
     category, high textual overlap) must UPDATE the existing row instead
     of inserting a contradictory near-duplicate.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fake Neo4j driver: records every (query, params) run() call
# ---------------------------------------------------------------------------


class _RecordingSession:
    def __init__(self, log: list, single_record=None):
        self._log = log
        self._single_record = single_record

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def run(self, query, **params):
        self._log.append((query, params))
        result = MagicMock()
        result.single = AsyncMock(return_value=self._single_record)
        return result


def _recording_driver(log: list, single_record=None):
    driver = MagicMock()
    driver.session = lambda: _RecordingSession(log, single_record)
    return driver


def _relates_to_writes(log: list) -> list[tuple[str, dict]]:
    return [(q, p) for (q, p) in log if "RELATES_TO {" in q]


# ---------------------------------------------------------------------------
# 1a. add_relationship: MERGE idempotency (two extractions -> one edge key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_relationship_merges_on_key_props():
    from app.services.entity_timeline import EntityTimelineService

    log: list = []
    svc = EntityTimelineService(_recording_driver(log))

    # Same relationship extracted by two runs.
    await svc.add_relationship("p1", "萧炎", "美杜莎", "敌对", 3)
    await svc.add_relationship("p1", "萧炎", "美杜莎", "敌对", 5)

    writes = _relates_to_writes(log)
    assert len(writes) == 2
    for query, params in writes:
        # MERGE, never CREATE, for the edge write.
        assert "MERGE (a)-[r:RELATES_TO {" in query
        assert "CREATE (a)-[" not in query
        # Constraint properties are part of the MERGE key (actually SET),
        # so Neo4j uniqueness applies and re-runs match the same edge.
        for prop in ("project_id: $pid", "source_name: $c1",
                     "target_name: $c2", "type: $rtype"):
            assert prop in query, f"missing {prop} in merge key"
        # chapter_start only on create (first sighting wins).
        assert "ON CREATE SET r.chapter_start = $start" in query

    # Both runs produce an identical MERGE key -> a real graph keeps 1 edge.
    keys = {
        (p["pid"], p["c1"], p["c2"], p["rtype"]) for (_, p) in writes
    }
    assert keys == {("p1", "萧炎", "美杜莎", "敌对")}


@pytest.mark.asyncio
async def test_update_relationship_closes_then_merges():
    from app.services.entity_timeline import EntityTimelineService

    log: list = []
    svc = EntityTimelineService(_recording_driver(log))

    await svc.update_relationship("p1", "萧炎", "纳兰嫣然", "敌对", 7)

    # First statement closes open edges; second MERGEs the new-type edge.
    close_q, close_p = log[0]
    assert "SET r.chapter_end = $idx" in close_q
    assert close_p["idx"] == 6

    writes = _relates_to_writes(log)
    assert len(writes) == 1
    query, params = writes[0]
    assert "MERGE (a)-[r:RELATES_TO {" in query
    assert "CREATE (a)-[" not in query
    for prop in ("project_id: $pid", "source_name: $c1",
                 "target_name: $c2", "type: $rtype"):
        assert prop in query
    # Type change re-opens at the new chapter.
    assert "SET r.chapter_start = $start, r.chapter_end = null" in query
    assert params["start"] == 7


# ---------------------------------------------------------------------------
# 1b. dedupe cleanup for legacy null-property duplicate edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedupe_relationships_deletes_dupes_and_backfills_keys():
    from app.services.entity_timeline import EntityTimelineService

    log: list = []
    svc = EntityTimelineService(
        _recording_driver(log, single_record={"deleted": 3})
    )

    deleted = await svc.dedupe_relationships("p1")

    assert deleted == 3
    assert len(log) == 1
    query, params = log[0]
    assert params["pid"] == "p1"
    # Keeps the newest edge per (source, target, type) group...
    assert "ORDER BY coalesce(r.chapter_start, -1) DESC" in query
    assert "r.type AS t" in query
    # ...backfills constraint props on the survivor so MERGE finds it...
    assert "SET keep.project_id = $pid" in query
    assert "keep.source_name = a.name" in query
    assert "keep.target_name = b.name" in query
    # ...and deletes the rest.
    assert "DELETE d" in query


# ---------------------------------------------------------------------------
# 2a. alias-merge on the PG character upsert path
# ---------------------------------------------------------------------------


def _char_row(name, aliases=None):
    return SimpleNamespace(
        name=name,
        profile_json={"aliases": aliases} if aliases is not None else {},
    )


def test_alias_to_canonical_exact_match_only():
    from app.tasks.entity_tasks import _alias_to_canonical

    existing = {
        "萧炎": _char_row("萧炎", ["炎帝", "萧炎"]),  # self-alias ignored
        "药老": _char_row("药老"),
    }
    assert _alias_to_canonical(existing) == {"炎帝": "萧炎"}


def test_alias_to_canonical_never_hijacks_existing_names():
    from app.tasks.entity_tasks import _alias_to_canonical

    # "药老" is claimed as an alias but already has its own row -> no remap.
    existing = {
        "萧炎": _char_row("萧炎", ["药老"]),
        "药老": _char_row("药老"),
    }
    assert _alias_to_canonical(existing) == {}


def test_alias_to_canonical_deterministic_on_conflicting_claims():
    from app.tasks.entity_tasks import _alias_to_canonical

    existing = {
        "萧炎": _char_row("萧炎", ["炎帝"]),
        "萧战": _char_row("萧战", ["炎帝"]),
    }
    # First claimant in sorted-name order wins ("萧战" < "萧炎").
    out = _alias_to_canonical(existing)
    assert out == {"炎帝": "萧战"}


def test_fold_aliases_no_new_row_for_alias_name():
    """New name matching an existing alias folds into the canonical
    character: no new characters row, edges/states re-point."""
    from app.tasks.entity_tasks import _fold_aliases

    existing = {"萧炎": _char_row("萧炎", ["炎帝"])}
    char_names, char_profiles, rels, memberships, at_locs, cstates = (
        _fold_aliases(
            existing,
            ["炎帝", "美杜莎"],
            {"炎帝": {"role": "主角"}, "美杜莎": {"role": "女帝"}},
            [("炎帝", "美杜莎", "敌对")],
            [("炎帝", "炎盟", 10, None)],
            [("炎帝", "中州", 12, None)],
            [("炎帝", 12, None, '{"境界":"斗帝"}')],
        )
    )

    # "炎帝" is gone everywhere; canonical "萧炎" took its place.
    assert char_names == ["美杜莎", "萧炎"]
    to_create = [n for n in char_names if n not in existing]
    assert to_create == ["美杜莎"]  # the alias creates NO new row
    assert rels == [("萧炎", "美杜莎", "敌对")]
    assert memberships == [("萧炎", "炎盟", 10, None)]
    assert at_locs == [("萧炎", "中州", 12, None)]
    assert cstates == [("萧炎", 12, None, '{"境界":"斗帝"}')]
    # The alias node's profile must not clobber the canonical profile.
    assert "炎帝" not in char_profiles
    assert char_profiles["美杜莎"] == {"role": "女帝"}


def test_fold_aliases_noop_without_aliases():
    from app.tasks.entity_tasks import _fold_aliases

    existing = {"萧炎": _char_row("萧炎")}
    args = (
        ["萧炎", "药老"],
        {"药老": {"role": "老师"}},
        [("萧炎", "药老", "师徒")],
        [],
        [],
        [],
    )
    out = _fold_aliases(existing, *args)
    assert out == args


# ---------------------------------------------------------------------------
# 2b. roster alias counting
# ---------------------------------------------------------------------------


def test_count_appearances_folds_alias_into_canonical():
    from app.services.character_roster import count_appearances

    text = "萧炎出手了。炎帝一怒，众人齐呼炎帝之名。"
    counts = count_appearances(text, {"萧炎", "美杜莎"}, {"萧炎": ["炎帝"]})
    # 1x 萧炎 + 2x 炎帝, summed into the canonical tally.
    assert counts == {"萧炎": 3}


def test_count_appearances_alias_never_steals_tracked_name():
    from app.services.character_roster import count_appearances

    # "炎帝" is itself a tracked name -> keeps its own tally.
    text = "萧炎出手了。炎帝一怒。"
    counts = count_appearances(text, {"萧炎", "炎帝"}, {"萧炎": ["炎帝"]})
    assert counts == {"萧炎": 1, "炎帝": 1}


def test_count_appearances_alias_for_untracked_canonical_ignored():
    from app.services.character_roster import count_appearances

    counts = count_appearances("老师来了。", {"萧炎"}, {"药老": ["老师"]})
    assert counts == {}


def test_count_appearances_substring_blanking_still_works():
    from app.services.character_roster import count_appearances

    # Pre-existing behavior: longer names matched first, spans blanked.
    text = "林惊蛰看着林惊。"
    counts = count_appearances(text, {"林惊蛰", "林惊"})
    assert counts == {"林惊蛰": 1, "林惊": 1}


@pytest.mark.asyncio
async def test_load_alias_map_defensive_shapes():
    from app.services.character_roster import load_alias_map

    rows = [
        ("萧炎", {"aliases": ["炎帝", "  ", 5]}),
        ("美杜莎", None),
        ("云韵", {"aliases": "不是列表"}),
        (None, {"aliases": ["x"]}),
    ]
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    out = await load_alias_map(db, "p1")
    assert out == {"萧炎": ["炎帝"]}


@pytest.mark.asyncio
async def test_rebuild_roster_counts_canonical_with_absolute_values():
    """W14: per-chapter alias-folded counts rebuild the roster with ABSOLUTE
    values (idempotent), not `appearance_count + c` increments."""
    from app.services import character_roster

    per_chapter = [
        (12, character_roster.count_appearances(
            "炎帝一怒，炎帝出手。", {"萧炎"}, {"萧炎": ["炎帝"]}
        )),
    ]
    executed = []

    db = MagicMock()

    async def _exec(stmt):
        executed.append(stmt)
        return MagicMock()

    db.execute = AsyncMock(side_effect=_exec)

    await character_roster.rebuild_roster(db, "p1", per_chapter)

    # One upsert (the canonical character only) + one stale-row delete.
    assert len(executed) == 2
    from sqlalchemy.dialects import postgresql
    compiled = executed[0].compile(dialect=postgresql.dialect())
    assert compiled.params.get("character_name") == "萧炎"
    assert compiled.params.get("appearance_count") == 2
    # The ON CONFLICT SET clause writes a literal, never the column + delta.
    sql = str(compiled)
    assert "appearance_count + " not in sql
    assert "DELETE FROM character_appearances" in str(
        executed[1].compile(dialect=postgresql.dialect())
    )


# ---------------------------------------------------------------------------
# 3. world-rule update-vs-insert boundaries
# ---------------------------------------------------------------------------

_RULE_A = "修炼者必须先凝聚斗气旋才能突破到斗师境界"
_RULE_A2 = "修炼者必须先凝聚斗气旋方能突破到斗师境界"  # one char re-worded


def test_rule_text_similar_thresholds():
    from app.tasks.entity_tasks import _rule_text_similar

    assert _rule_text_similar(_RULE_A, _RULE_A) is True
    assert _rule_text_similar(_RULE_A, _RULE_A2) is True  # jaccard > 0.8
    # Containment counts when the shorter side has >= 8 non-space chars.
    assert _rule_text_similar(
        "禁空领域内不得飞行", "在迦南学院的禁空领域内不得飞行，违者废除斗气"
    ) is True
    # Short containment is NOT enough (generic fragments must not merge).
    assert _rule_text_similar("不得飞行", "城内不得飞行违者重罚") is False
    assert _rule_text_similar("斗气大陆没有魔法", "灵魂力量决定炼药师等级") is False
    assert _rule_text_similar("", _RULE_A) is False


def test_world_rule_same_category_high_overlap_updates():
    from app.tasks.entity_tasks import _plan_world_rule_writes

    inserts, updates = _plan_world_rule_writes(
        [("力量体系", _RULE_A)], [("力量体系", _RULE_A2)]
    )
    assert inserts == []
    assert updates == [("力量体系", _RULE_A, _RULE_A2)]


def test_world_rule_different_category_never_merges():
    from app.tasks.entity_tasks import _plan_world_rule_writes

    inserts, updates = _plan_world_rule_writes(
        [("力量体系", _RULE_A)], [("禁忌", _RULE_A2)]
    )
    assert inserts == [("禁忌", _RULE_A2)]
    assert updates == []


def test_world_rule_low_similarity_inserts():
    from app.tasks.entity_tasks import _plan_world_rule_writes

    inserts, updates = _plan_world_rule_writes(
        [("力量体系", "斗气大陆没有魔法")],
        [("力量体系", "灵魂力量决定炼药师等级")],
    )
    assert inserts == [("力量体系", "灵魂力量决定炼药师等级")]
    assert updates == []


def test_world_rule_exact_duplicate_is_noop():
    from app.tasks.entity_tasks import _plan_world_rule_writes

    inserts, updates = _plan_world_rule_writes(
        [("力量体系", _RULE_A)], [("力量体系", _RULE_A)]
    )
    assert inserts == []
    assert updates == []


def test_world_rule_intra_batch_near_dupes_collapse_to_one_insert():
    from app.tasks.entity_tasks import _plan_world_rule_writes

    inserts, updates = _plan_world_rule_writes(
        [], [("力量体系", _RULE_A), ("力量体系", _RULE_A2)]
    )
    # Second near-dupe rewrites the planned insert instead of doubling up.
    assert inserts == [("力量体系", _RULE_A2)]
    assert updates == []
