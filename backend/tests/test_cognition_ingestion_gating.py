"""Source tripwires for cognition-ledger ingestion gating (Q3 review fixes).

The ingestion call sites live deep inside the Celery async-generation task
and the SSE chapter stream — neither is practically unit-testable, so these
tests parse the source AST and pin the structural invariants instead:

1. knowledge_tasks: the needs_review final-save path must NOT ingest — every
   ``extract_and_update`` call must sit inside an ``if passed:`` guard body.
2. api.generate: ingestion must run exactly once per stream, AFTER the final
   text is settled — never inside ``_persist_chapter_now`` (which runs at
   draft-save time and again on the final-polish re-save).
"""
from __future__ import annotations

import ast
from pathlib import Path

import app

_APP_DIR = Path(app.__file__).resolve().parent


def _parse(rel_path: str) -> ast.Module:
    return ast.parse((_APP_DIR / rel_path).read_text(encoding="utf-8"))


def _is_extract_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = getattr(func, "id", None) or getattr(func, "attr", None)
    return name == "extract_and_update"


def test_celery_ingestion_wrapped_in_passed_guard():
    """knowledge_tasks: cognition ingestion is review-gated on `passed`."""
    tree = _parse("tasks/knowledge_tasks.py")

    guarded_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test_names = {
                n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)
            }
            if "passed" in test_names:
                # Only the if-body counts; an else-branch call would be the
                # exact bug this guards against.
                for stmt in node.body:
                    for sub in ast.walk(stmt):
                        if _is_extract_call(sub):
                            guarded_ids.add(id(sub))

    all_calls = [n for n in ast.walk(tree) if _is_extract_call(n)]
    assert all_calls, "expected an extract_and_update call in knowledge_tasks"
    unguarded = [n for n in all_calls if id(n) not in guarded_ids]
    assert not unguarded, (
        "cognition extract_and_update call(s) not wrapped in an `if passed:` "
        f"guard at line(s) {[n.lineno for n in unguarded]} — needs_review "
        "saves must not be ingested into the ledger"
    )


def test_sse_ingestion_not_in_persist_helper_and_single_call_site():
    """api.generate: no draft-time ingestion, exactly one post-final call."""
    tree = _parse("api/generate.py")

    persist_fns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "_persist_chapter_now"
    ]
    assert persist_fns, "expected _persist_chapter_now in api.generate"
    for fn in persist_fns:
        calls_inside = [n for n in ast.walk(fn) if _is_extract_call(n)]
        assert not calls_inside, (
            "_persist_chapter_now must not ingest into the cognition ledger "
            "(draft-time extraction self-masks cognition_violation and "
            "double-ingests via the final-polish re-save); found at line(s) "
            f"{[n.lineno for n in calls_inside]}"
        )

    all_calls = [n for n in ast.walk(tree) if _is_extract_call(n)]
    assert len(all_calls) == 1, (
        "api.generate must ingest into the cognition ledger exactly once "
        f"(after the final text is settled); found {len(all_calls)} call(s) "
        f"at line(s) {[n.lineno for n in all_calls]}"
    )
