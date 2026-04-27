"""v1.5.0 C4-5: cascade planner & task surface unit tests.

Pure unit tests covering ``app/services/cascade_planner.py`` plus the
minimal public surface of ``app/tasks/cascade.py`` (Celery task name,
lock retry countdown, terminal-status set).

Rationale: the planner is intentionally side-effect-free (only SELECTs);
the Celery task already has end-to-end coverage from the C4-4 SSE smoke
(``cascade_triggered`` event + cascade_tasks row -> 'skipped'
outline_handler_not_implemented). Here we exercise *all* the pure-Python
decision points that lie between issues_json and CascadeTaskCandidate.

No DB, Celery, or HTTP fixtures are used: the two ``_load_*`` helpers
are monkeypatched onto the planner module.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import cascade_planner as cp
from app.services.cascade_planner import (
    ALLOWED_SEVERITIES,
    ALLOWED_TARGET_TYPES,
    CRITICAL_ISSUE_COUNT,
    CascadeTaskCandidate,
    DEFAULT_OVERALL_THRESHOLD,
    IN_SCOPE_DIMENSIONS,
    _build_issue_summary,
    _match_character_names,
    _normalise_issues,
    _truncate,
    plan_cascade,
    should_trigger_cascade,
)
from app.tasks import cascade as cascade_task


# --- helpers ---------------------------------------------------------------


def _ch(name: str):
    """Stub Character with .id (UUID) + .name (str) -- planner only reads these."""
    return SimpleNamespace(id=uuid4(), name=name)


def _outline(_id=None):
    """Stub Outline with .id only -- planner just coerces it to str."""
    return SimpleNamespace(id=_id or uuid4())


@pytest.fixture
def stub_planner_loaders(monkeypatch):
    """Monkeypatch ``_load_target_outline`` / ``_load_project_characters``
    with in-memory stubs. Tests configure ``state.outline`` and
    ``state.characters`` directly."""

    state = SimpleNamespace(outline=None, characters=[])

    async def fake_outline(_db, _project_id):
        return state.outline

    async def fake_characters(_db, _project_id):
        return list(state.characters)

    monkeypatch.setattr(cp, "_load_target_outline", fake_outline)
    monkeypatch.setattr(cp, "_load_project_characters", fake_characters)
    return state


# --- should_trigger_cascade ------------------------------------------------


class TestShouldTriggerCascade:
    def test_below_threshold_and_exhausted_triggers(self):
        assert should_trigger_cascade(overall=3.5, rounds_exhausted=True) is True

    def test_at_threshold_does_not_trigger(self):
        # Strict less-than: == threshold is treated as "good enough".
        assert (
            should_trigger_cascade(
                overall=DEFAULT_OVERALL_THRESHOLD, rounds_exhausted=True
            )
            is False
        )

    def test_above_threshold_does_not_trigger(self):
        assert (
            should_trigger_cascade(overall=9.5, rounds_exhausted=True) is False
        )

    def test_not_exhausted_does_not_trigger(self):
        # Even a 0.0 overall must not fire cascade until auto_revise gives up.
        assert should_trigger_cascade(overall=0.0, rounds_exhausted=False) is False

    def test_overall_none_does_not_trigger(self):
        assert (
            should_trigger_cascade(overall=None, rounds_exhausted=True) is False
        )

    def test_custom_threshold_respected(self):
        assert (
            should_trigger_cascade(overall=8.0, rounds_exhausted=True, threshold=7.0)
            is False
        )
        assert (
            should_trigger_cascade(overall=6.0, rounds_exhausted=True, threshold=7.0)
            is True
        )


# --- _normalise_issues -----------------------------------------------------


class TestNormaliseIssues:
    def test_none_returns_empty(self):
        assert _normalise_issues(None) == []

    def test_list_of_dicts_passthrough(self):
        items = [
            {"dimension": "plot_coherence", "description": "x"},
            {"dimension": "style_adherence", "description": "y"},
        ]
        assert _normalise_issues(items) == items

    def test_list_filters_non_mappings(self):
        items = [
            {"dimension": "plot_coherence"},
            "not a mapping",
            42,
            {"dimension": "character_consistency"},
        ]
        out = _normalise_issues(items)
        assert len(out) == 2
        assert all(isinstance(o, dict) for o in out)

    def test_dict_of_dimensions_is_flattened_and_dimension_set(self):
        # Tolerates the raw evaluator payload shape if anyone hands it to us.
        raw = {
            "plot_coherence": {
                "issues": [{"description": "a"}, {"description": "b"}],
            },
            "style_adherence": {"issues": [{"description": "c"}]},
        }
        out = _normalise_issues(raw)
        assert len(out) == 3
        for item in out:
            assert "dimension" in item
        dims = {i["dimension"] for i in out}
        assert dims == {"plot_coherence", "style_adherence"}

    def test_string_input_returns_empty(self):
        assert _normalise_issues("garbage") == []


# --- _truncate / _build_issue_summary --------------------------------------


class TestTruncateAndSummary:
    def test_truncate_short_unchanged(self):
        assert _truncate("hello", limit=240) == "hello"

    def test_truncate_long_gets_ellipsis(self):
        text = "a" * 300
        out = _truncate(text, limit=50)
        assert len(out) <= 50
        assert out.endswith("\u2026")

    def test_truncate_strips_whitespace(self):
        assert _truncate("  spaced  ", limit=240) == "spaced"

    def test_build_issue_summary_caps_at_5_lines(self):
        # 10 issues -> 5 rendered + 1 "+N more" footer.
        issues = [
            {"dimension": f"d{i}", "location": f"L{i}", "description": f"desc {i}"}
            for i in range(10)
        ]
        summary = _build_issue_summary(issues)
        lines = summary.split("\n")
        assert len(lines) == 6
        assert lines[-1].startswith("...")

    def test_build_issue_summary_includes_dimension_and_location(self):
        out = _build_issue_summary(
            [
                {
                    "dimension": "plot_coherence",
                    "location": "ch3",
                    "description": "broken arc",
                }
            ]
        )
        assert "[plot_coherence@ch3]" in out
        assert "broken arc" in out

    def test_build_issue_summary_omits_empty_location(self):
        out = _build_issue_summary(
            [
                {
                    "dimension": "character_consistency",
                    "description": "tone shift",
                }
            ]
        )
        assert "@" not in out
        assert "[character_consistency]" in out


# --- _match_character_names ------------------------------------------------


class TestMatchCharacterNames:
    def test_substring_match(self):
        chars = [_ch("\u963f\u91cc"), _ch("\u5f20\u4e09"), _ch("\u674e\u56db")]
        out = _match_character_names(
            "\u963f\u91cc \u4e0e \u5f20\u4e09 \u5728\u5c4b\u9876\u4e89\u5435",
            chars,
        )
        names = {c.name for c in out}
        assert names == {"\u963f\u91cc", "\u5f20\u4e09"}

    def test_no_match_returns_empty(self):
        chars = [_ch("\u963f\u91cc"), _ch("\u5f20\u4e09")]
        assert (
            _match_character_names(
                "\u6ca1\u6709\u4eba\u7269\u5728\u573a", chars
            )
            == []
        )

    def test_dedupes_when_same_id_passed_twice(self):
        c1 = _ch("\u963f\u91cc")
        out = _match_character_names("\u963f\u91cc\u963f\u91cc", [c1, c1])
        assert len(out) == 1

    def test_blank_description_returns_empty(self):
        chars = [_ch("\u963f\u91cc")]
        assert _match_character_names("", chars) == []


# --- plan_cascade ----------------------------------------------------------


@pytest.mark.asyncio
class TestPlanCascade:
    async def test_empty_issues_returns_empty(self, stub_planner_loaders):
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=None,
        )
        assert out == []

    async def test_only_out_of_scope_returns_empty(self, stub_planner_loaders):
        # style_adherence and narrative_pacing must NEVER cascade -- they
        # are chapter-local concerns auto_revise already handles.
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=[
                {"dimension": "style_adherence", "description": "x"},
                {"dimension": "narrative_pacing", "description": "y"},
            ],
        )
        assert out == []

    async def test_plot_and_foreshadow_dedup_to_single_outline_candidate(
        self, stub_planner_loaders
    ):
        outline = _outline()
        stub_planner_loaders.outline = outline
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=[
                {"dimension": "plot_coherence", "description": "arc broken"},
                {
                    "dimension": "foreshadow_handling",
                    "description": "missing payoff",
                },
            ],
        )
        assert len(out) == 1
        cand = out[0]
        assert cand.target_entity_type == "outline"
        assert cand.target_entity_id == str(outline.id)
        # 2 issues < CRITICAL_ISSUE_COUNT (3) -> default 'high'.
        assert cand.severity == "high"
        assert cand.issue_count == 2
        assert set(cand.contributing_dimensions) == {
            "plot_coherence",
            "foreshadow_handling",
        }

    async def test_three_plot_issues_escalate_to_critical(
        self, stub_planner_loaders
    ):
        stub_planner_loaders.outline = _outline()
        issues = [
            {"dimension": "plot_coherence", "description": f"issue {i}"}
            for i in range(CRITICAL_ISSUE_COUNT)
        ]
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=issues,
        )
        assert len(out) == 1
        assert out[0].severity == "critical"
        assert out[0].issue_count == CRITICAL_ISSUE_COUNT

    async def test_no_outline_drops_plot_issues(self, stub_planner_loaders):
        # Defensive: missing outline => skip, never crash.
        stub_planner_loaders.outline = None
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=[{"dimension": "plot_coherence", "description": "x"}],
        )
        assert out == []

    async def test_character_match_targets_named_chars_only(
        self, stub_planner_loaders
    ):
        ali = _ch("\u963f\u91cc")
        zhang = _ch("\u5f20\u4e09")
        li = _ch("\u674e\u56db")
        stub_planner_loaders.characters = [ali, zhang, li]
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=[
                {
                    "dimension": "character_consistency",
                    "description": "\u963f\u91cc \u7684\u8bed\u6c14\u524d\u540e\u77db\u76fe",
                }
            ],
        )
        assert len(out) == 1
        assert out[0].target_entity_type == "character"
        assert out[0].target_entity_id == str(ali.id)

    async def test_character_unmatched_fans_out_to_all(
        self, stub_planner_loaders
    ):
        chars = [_ch("\u963f\u91cc"), _ch("\u5f20\u4e09")]
        stub_planner_loaders.characters = chars
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=[
                {
                    "dimension": "character_consistency",
                    "description": "\u672a\u63d0\u53ca\u59d3\u540d\u7684\u4eba\u7269\u8bed\u6c14\u4e0d\u4e00\u81f4",
                }
            ],
        )
        # No name match -> fan out to ALL chars ("better over-cover").
        assert len(out) == len(chars)
        target_ids = {c.target_entity_id for c in out}
        assert target_ids == {str(c.id) for c in chars}
        for c in out:
            assert c.target_entity_type == "character"

    async def test_no_characters_skips_character_issues(
        self, stub_planner_loaders
    ):
        stub_planner_loaders.characters = []
        out = await plan_cascade(
            db=None,
            project_id="p",
            source_chapter_id="c",
            source_evaluation_id="e",
            issues_json=[
                {"dimension": "character_consistency", "description": "x"},
            ],
        )
        assert out == []

    async def test_candidate_carries_source_ids_and_in_allowed_sets(
        self, stub_planner_loaders
    ):
        stub_planner_loaders.outline = _outline()
        out = await plan_cascade(
            db=None,
            project_id="proj-1",
            source_chapter_id="chap-1",
            source_evaluation_id="eval-1",
            issues_json=[{"dimension": "plot_coherence", "description": "x"}],
        )
        assert len(out) == 1
        cand = out[0]
        assert cand.project_id == "proj-1"
        assert cand.source_chapter_id == "chap-1"
        assert cand.source_evaluation_id == "eval-1"
        # All emitted candidates must satisfy the DB CHECK constraints.
        assert cand.target_entity_type in ALLOWED_TARGET_TYPES
        assert cand.severity in ALLOWED_SEVERITIES


# --- planner module-level constants ----------------------------------------


class TestCascadePlannerConstants:
    def test_target_types_match_db_check_constraint(self):
        # Mirrors ck_cascade_tasks_target_entity_type.
        assert ALLOWED_TARGET_TYPES == frozenset(
            {"chapter", "outline", "character", "world_rule"}
        )

    def test_severities_match_db_check_constraint(self):
        # Mirrors ck_cascade_tasks_severity.
        assert ALLOWED_SEVERITIES == frozenset({"high", "critical"})

    def test_in_scope_dimensions_excludes_chapter_local(self):
        assert "style_adherence" not in IN_SCOPE_DIMENSIONS
        assert "narrative_pacing" not in IN_SCOPE_DIMENSIONS
        assert "plot_coherence" in IN_SCOPE_DIMENSIONS
        assert "foreshadow_handling" in IN_SCOPE_DIMENSIONS
        assert "character_consistency" in IN_SCOPE_DIMENSIONS

    def test_default_threshold_and_critical_count_sane(self):
        assert 0.0 < DEFAULT_OVERALL_THRESHOLD <= 10.0
        assert CRITICAL_ISSUE_COUNT >= 2


# --- cascade.py public surface ---------------------------------------------


class TestCascadeTaskConstants:
    def test_run_cascade_task_name_stable(self):
        # The C4-4 generate.py wires SSE -> Celery via this exact name
        # string (send_task name=...). Renaming silently breaks cascade.
        assert cascade_task.RUN_CASCADE_TASK == "cascade.run_cascade_task"

    def test_terminal_statuses_match_db_check_constraint(self):
        assert cascade_task._TERMINAL_STATUSES == frozenset(
            {"done", "failed", "skipped"}
        )

    def test_lock_retry_countdown_positive_int(self):
        assert isinstance(cascade_task.LOCK_RETRY_COUNTDOWN, int)
        assert cascade_task.LOCK_RETRY_COUNTDOWN > 0


# --- _handle_outline_target -------------------------------------------------


@pytest.mark.asyncio
class TestHandleOutlineTarget:
    """C4-7: real outline cascade handler.

    Validates the in-place revision write, idempotency, and defensive
    error paths. No DB / Celery / LLM fixtures: db is a stub namespace
    with async ``.get`` / ``.commit``.
    """

    def _outline_row(self, project_id="p", content=None, version=1, is_confirmed=1):
        return SimpleNamespace(
            id=uuid4(),
            project_id=project_id,
            content_json=content if content is not None else {},
            version=version,
            is_confirmed=is_confirmed,
        )

    async def _stub_db(self, outline_obj):
        async def fake_get(_cls, _id):
            return outline_obj

        async def fake_commit():
            return None

        return SimpleNamespace(get=fake_get, commit=fake_commit)

    async def test_outline_revision_recorded(self):
        outline = self._outline_row()
        db = await self._stub_db(outline)
        out = await cascade_task._handle_outline_target(
            db=db,
            target_entity_id=str(outline.id),
            project_id="p",
            issue_summary="[plot_coherence@ch3] arc broken\n[foreshadow_handling] missed payoff",
            severity="high",
            source_chapter_id="ch-1",
        )
        assert out["status"] == "done"
        assert out["reason"] == "outline_revision_recorded"
        cj = outline.content_json
        assert isinstance(cj, dict)
        revs = cj["cascade_revisions"]
        assert len(revs) == 1
        rev = revs[0]
        assert rev["rev_key"] == "ch-1:high"
        assert rev["source_chapter_id"] == "ch-1"
        assert rev["severity"] == "high"
        assert "applied_at" in rev
        hints = cj["cascade_hints"]
        assert "[plot_coherence@ch3] arc broken" in hints
        assert "[foreshadow_handling] missed payoff" in hints
        assert outline.version == 2
        # is_confirmed must be preserved (see handler comment).
        assert outline.is_confirmed == 1

    async def test_outline_revision_idempotent(self):
        existing_rev = {
            "rev_key": "ch-1:high",
            "source_chapter_id": "ch-1",
            "severity": "high",
            "issue_summary": "old",
            "applied_at": "2026-01-01T00:00:00+00:00",
        }
        outline = self._outline_row(
            content={
                "cascade_revisions": [existing_rev],
                "cascade_hints": ["old"],
            }
        )
        db = await self._stub_db(outline)
        out = await cascade_task._handle_outline_target(
            db=db,
            target_entity_id=str(outline.id),
            project_id="p",
            issue_summary="new summary",
            severity="high",
            source_chapter_id="ch-1",
        )
        assert out["status"] == "skipped"
        assert out["reason"] == "outline_revision_already_recorded"
        # state unchanged
        assert outline.version == 1
        assert outline.is_confirmed == 1
        assert outline.content_json["cascade_revisions"] == [existing_rev]
        assert outline.content_json["cascade_hints"] == ["old"]

    async def test_outline_target_not_found(self):
        async def fake_get(_cls, _id):
            return None

        async def fake_commit():
            return None

        db = SimpleNamespace(get=fake_get, commit=fake_commit)
        out = await cascade_task._handle_outline_target(
            db=db,
            target_entity_id="missing-id",
            project_id="p",
            issue_summary="x",
            severity="high",
            source_chapter_id="ch-1",
        )
        assert out["status"] == "failed"
        assert "not_found" in out["reason"]

    async def test_outline_project_mismatch(self):
        outline = self._outline_row(project_id="other-project")
        db = await self._stub_db(outline)
        out = await cascade_task._handle_outline_target(
            db=db,
            target_entity_id=str(outline.id),
            project_id="p",
            issue_summary="x",
            severity="high",
            source_chapter_id="ch-1",
        )
        assert out["status"] == "failed"
        assert out["reason"] == "outline_target_project_mismatch"

    async def test_outline_severity_distinguishes_rev_key(self):
        # Same chapter but different severity -> independent revision keys.
        outline = self._outline_row()
        db = await self._stub_db(outline)
        out1 = await cascade_task._handle_outline_target(
            db=db,
            target_entity_id=str(outline.id),
            project_id="p",
            issue_summary="a",
            severity="high",
            source_chapter_id="ch-1",
        )
        out2 = await cascade_task._handle_outline_target(
            db=db,
            target_entity_id=str(outline.id),
            project_id="p",
            issue_summary="b",
            severity="critical",
            source_chapter_id="ch-1",
        )
        assert out1["status"] == "done"
        assert out2["status"] == "done"
        revs = outline.content_json["cascade_revisions"]
        assert {r["rev_key"] for r in revs} == {"ch-1:high", "ch-1:critical"}
        assert outline.version == 3

    async def test_outline_missing_target_entity_id(self):
        async def fake_get(_cls, _id):
            return None

        async def fake_commit():
            return None

        db = SimpleNamespace(get=fake_get, commit=fake_commit)
        out = await cascade_task._handle_outline_target(
            db=db,
            target_entity_id="",
            project_id="p",
            issue_summary="x",
            severity="high",
            source_chapter_id="ch-1",
        )
        assert out["status"] == "failed"
        assert out["reason"] == "outline_target_missing_id"
