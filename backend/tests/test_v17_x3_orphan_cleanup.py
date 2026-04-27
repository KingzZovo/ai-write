"""v1.7 X3 unit tests: pure logic of compute_orphan_ids in the
cleanup_orphan_qdrant_slices script.

We import the module by file path so we don't depend on the script
being on PYTHONPATH (it lives under /scripts not /backend/app).
"""
import importlib.util
import pathlib


def _load_script_module():
    script_path = pathlib.Path("/app/scripts/cleanup_orphan_qdrant_slices.py")
    if not script_path.exists():
        # Local dev path inside the repo
        script_path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cleanup_orphan_qdrant_slices.py"
    spec = importlib.util.spec_from_file_location("cleanup_orphan_qdrant_slices", str(script_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_compute_orphan_ids_marks_unknown_slice_id_as_orphan():
    mod = _load_script_module()
    points = [
        (1, "sid-A"),  # in PG -> kept
        (2, "sid-B"),  # in PG -> kept
        (3, "sid-X"),  # NOT in PG -> orphan
        (4, "sid-Y"),  # NOT in PG -> orphan
    ]
    pg = {"sid-A", "sid-B", "sid-C"}  # sid-C exists in PG but not in Qdrant; that's fine

    orphans, orphan_n, kept = mod.compute_orphan_ids(points, pg)

    assert orphan_n == 2
    assert kept == 2
    assert set(orphans) == {3, 4}


def test_compute_orphan_ids_treats_missing_slice_id_as_orphan():
    """Points whose payload is missing slice_id are also orphans."""
    mod = _load_script_module()
    points = [
        (10, None),    # no slice_id -> orphan
        (11, "sid-A"), # kept
    ]
    pg = {"sid-A"}

    orphans, orphan_n, kept = mod.compute_orphan_ids(points, pg)

    assert orphan_n == 1
    assert kept == 1
    assert orphans == [10]


def test_compute_orphan_ids_idempotent_on_clean_state():
    """After a successful cleanup, the next run finds 0 orphans."""
    mod = _load_script_module()
    points = [(1, "a"), (2, "b"), (3, "c")]
    pg = {"a", "b", "c"}

    orphans, orphan_n, kept = mod.compute_orphan_ids(points, pg)

    assert orphan_n == 0
    assert kept == 3
    assert orphans == []


def test_compute_orphan_ids_empty_pg_makes_everything_orphan():
    """If PG has no slices, every Qdrant point is orphan."""
    mod = _load_script_module()
    points = [(1, "a"), (2, "b")]
    pg: set[str] = set()

    orphans, orphan_n, kept = mod.compute_orphan_ids(points, pg)

    assert orphan_n == 2
    assert kept == 0
    assert set(orphans) == {1, 2}


def test_compute_orphan_ids_supports_string_point_ids():
    """Qdrant supports both int and UUID-string point IDs."""
    mod = _load_script_module()
    points = [
        ("point-uuid-1", "sid-A"),
        ("point-uuid-2", "sid-X"),
    ]
    pg = {"sid-A"}

    orphans, orphan_n, kept = mod.compute_orphan_ids(points, pg)

    assert orphan_n == 1
    assert kept == 1
    assert orphans == ["point-uuid-2"]
