"""Unit tests for the pure retention/partition-selection logic.

Covers app/tasks/retention_tasks.py (v1.11 scaling groundwork):
partition-name computation, pre-creation month ranges, bound parsing,
drop-selection boundaries, and keep-last-K version selection. Pure
functions only — no DB, no DDL.
"""

from datetime import date, datetime, timezone

from app.tasks.retention_tasks import (
    add_months,
    months_to_precreate,
    parse_upper_bound,
    partition_name,
    retention_cutoff,
    select_partitions_to_drop,
    select_version_ids_to_delete,
)


# ---------------------------------------------------------------------------
# Month math + naming
# ---------------------------------------------------------------------------

def test_add_months_basic_and_year_rollover():
    assert add_months(date(2026, 7, 26), 0) == date(2026, 7, 1)
    assert add_months(date(2026, 7, 26), 1) == date(2026, 8, 1)
    assert add_months(date(2026, 11, 2), 3) == date(2027, 2, 1)
    assert add_months(date(2026, 1, 31), -1) == date(2025, 12, 1)
    assert add_months(date(2026, 1, 15), -13) == date(2024, 12, 1)


def test_partition_name_zero_padding():
    assert partition_name(2026, 8) == "llm_call_logs_y2026m08"
    assert partition_name(2027, 12) == "llm_call_logs_y2027m12"


def test_months_to_precreate_covers_current_through_ahead():
    got = months_to_precreate(date(2026, 11, 15), 3)
    assert got == [
        (date(2026, 11, 1), date(2026, 12, 1)),
        (date(2026, 12, 1), date(2027, 1, 1)),
        (date(2027, 1, 1), date(2027, 2, 1)),
        (date(2027, 2, 1), date(2027, 3, 1)),
    ]
    # ahead=0 still ensures the current month exists; negative clamps to 0.
    assert len(months_to_precreate(date(2026, 7, 1), 0)) == 1
    assert len(months_to_precreate(date(2026, 7, 1), -5)) == 1


# ---------------------------------------------------------------------------
# relpartbound parsing
# ---------------------------------------------------------------------------

def test_parse_upper_bound_range_partition():
    expr = "FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00')"
    assert parse_upper_bound(expr) == date(2026, 9, 1)


def test_parse_upper_bound_minvalue_hist_partition():
    expr = "FOR VALUES FROM (MINVALUE) TO ('2026-08-01 00:00:00+00')"
    assert parse_upper_bound(expr) == date(2026, 8, 1)


def test_parse_upper_bound_default_and_garbage():
    assert parse_upper_bound("DEFAULT") is None
    assert parse_upper_bound(None) is None
    assert parse_upper_bound("") is None
    assert parse_upper_bound("FOR VALUES FROM ('x') TO (MAXVALUE)") is None


# ---------------------------------------------------------------------------
# Drop selection boundaries
# ---------------------------------------------------------------------------

def _parts():
    return [
        ("llm_call_logs_hist", date(2026, 8, 1)),
        ("llm_call_logs_y2026m08", date(2026, 9, 1)),
        ("llm_call_logs_y2026m09", date(2026, 10, 1)),
        ("llm_call_logs_default", None),
    ]


def test_retention_cutoff_is_first_kept_month():
    assert retention_cutoff(date(2026, 7, 26), 6) == date(2026, 1, 1)
    assert retention_cutoff(date(2027, 2, 3), 6) == date(2026, 8, 1)


def test_no_drops_within_retention_window():
    # today=2026-10: cutoff 2026-04; every partition's upper > cutoff.
    assert select_partitions_to_drop(_parts(), date(2026, 10, 15), 6) == []


def test_drop_exact_boundary_upper_equal_cutoff():
    # today=2027-02 -> cutoff 2026-08-01. hist upper == cutoff: its newest
    # possible row (< 2026-08-01) is older than every kept month -> drop.
    # y2026m08 (upper 2026-09-01) still holds rows from a kept month -> keep.
    assert select_partitions_to_drop(_parts(), date(2027, 2, 3), 6) == [
        "llm_call_logs_hist"
    ]


def test_drop_multiple_expired_but_never_default():
    # today=2027-03 -> cutoff 2026-09-01: hist (upper 08-01) and m08
    # (upper 09-01, i.e. August rows) expired; m09 holds September -> keep.
    got = select_partitions_to_drop(_parts(), date(2027, 3, 1), 6)
    assert got == ["llm_call_logs_hist", "llm_call_logs_y2026m08"]
    assert "llm_call_logs_default" not in got

    # One month later September ages out too.
    got = select_partitions_to_drop(_parts(), date(2027, 4, 1), 6)
    assert got == [
        "llm_call_logs_hist",
        "llm_call_logs_y2026m08",
        "llm_call_logs_y2026m09",
    ]


def test_retention_disabled_drops_nothing():
    assert select_partitions_to_drop(_parts(), date(2030, 1, 1), 0) == []
    assert select_partitions_to_drop(_parts(), date(2030, 1, 1), -3) == []


# ---------------------------------------------------------------------------
# chapter_versions keep-last-K
# ---------------------------------------------------------------------------

def _ts(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=timezone.utc)


def test_keep_last_zero_keeps_everything():
    rows = [("v1", "ch1", _ts(1), 0), ("v2", "ch1", _ts(2), 0)]
    assert select_version_ids_to_delete(rows, 0) == []
    assert select_version_ids_to_delete(rows, -1) == []


def test_keeps_newest_k_per_chapter():
    rows = [
        ("v1", "ch1", _ts(1), 0),
        ("v2", "ch1", _ts(2), 0),
        ("v3", "ch1", _ts(3), 0),
        ("v4", "ch2", _ts(1), 0),  # ch2 is under the limit -> untouched
    ]
    assert select_version_ids_to_delete(rows, 2) == ["v1"]


def test_active_version_never_deleted_even_if_old():
    rows = [
        ("v1", "ch1", _ts(1), 1),  # oldest but active
        ("v2", "ch1", _ts(2), 0),
        ("v3", "ch1", _ts(3), 0),
    ]
    assert select_version_ids_to_delete(rows, 1) == ["v2"]


def test_exactly_k_versions_deletes_nothing():
    rows = [("v1", "ch1", _ts(1), 0), ("v2", "ch1", _ts(2), 0)]
    assert select_version_ids_to_delete(rows, 2) == []


def test_null_created_at_sorts_oldest_and_ties_break_on_id():
    rows = [
        ("v-null", "ch1", None, 0),
        ("v-new", "ch1", _ts(5), 0),
        ("vb", "ch1", _ts(3), 0),
        ("va", "ch1", _ts(3), 0),  # same timestamp as vb; id breaks the tie
    ]
    # keep 2 newest: v-new, then vb (id 'vb' > 'va' on equal timestamps).
    assert select_version_ids_to_delete(rows, 2) == ["va", "v-null"]
