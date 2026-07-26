"""Source tripwires for cognition-ledger ingestion gating (Q3 review fixes).

The ingestion call sites live deep inside the Celery async-generation task
and the SSE chapter stream — neither is practically unit-testable, so these
tests parse the source AST and pin the structural invariants instead:

1. generation_tasks: ingestion runs exactly once, on the FINAL save, and is
   no longer gated on ``passed`` (2026-07-26 audit — see the celery test below
   for the rationale). Blocked quality-gate / fact-contract branches return
   before the final save, so blocked drafts are structurally never ingested.
2. api.generate: ingestion must run exactly once per stream, AFTER the final
   text is settled — never inside ``_persist_chapter_now`` (which runs at
   draft-save time and again on the final-polish re-save).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

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


def test_celery_ingestion_on_final_save_not_gated_on_passed():
    """generation_tasks: cognition ingestion runs on every FINAL save.

    2026-07-26 audit: the earlier ``if passed:`` gate was lossy — a
    needs_review final save still persists full_text as the chapter's
    canonical content_text (readable, exportable, summarized,
    entity-extracted), and a later manual accept never re-ingests, so
    accepted chapters silently missed the ledger. The SSE path ingests the
    settled text regardless of the evaluation score, withholding only
    aborted/blocked drafts; the celery path now aligns with it: the
    quality-gate / fact-contract blocked branches return before the final
    save, and the single ingestion call site is NOT nested in a `passed`
    guard.
    """
    tree = _parse("tasks/generation_tasks.py")

    all_calls = [n for n in ast.walk(tree) if _is_extract_call(n)]
    assert len(all_calls) == 1, (
        "generation_tasks must ingest into the cognition ledger exactly once "
        f"(on the final save); found {len(all_calls)} call(s) at line(s) "
        f"{[n.lineno for n in all_calls]}"
    )

    gated_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test_names = {
                n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)
            }
            if "passed" in test_names:
                for sub in ast.walk(node):
                    if _is_extract_call(sub):
                        gated_ids.add(id(sub))

    gated = [n for n in all_calls if id(n) in gated_ids]
    assert not gated, (
        "cognition extract_and_update must not be gated on `passed` anymore "
        f"(found inside an if-passed guard at line(s) {[n.lineno for n in gated]}) "
        "— the needs_review final save persists the text as canon and must be "
        "ingested"
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


# ---------------------------------------------------------------------------
# Task A4 — remaining evaluate() callers must feed the cognition ledger
# ---------------------------------------------------------------------------


def _call_name(node: ast.Call) -> str | None:
    return getattr(node.func, "attr", None) or getattr(node.func, "id", None)


def test_standalone_evaluation_task_passes_cognition_ledger():
    """tasks/evaluation_tasks: evaluate() gets cognition_ledger_text from a
    real load_ledger call (chapter -> volume -> project_id reverse lookup).

    The Celery entry opens/closes its own sessions around the LLM call, so
    this is a source tripwire in the same style as the tests above.
    """
    tree = _parse("tasks/evaluation_tasks.py")

    eval_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _call_name(n) == "evaluate"
    ]
    assert eval_calls, "expected an evaluator.evaluate(...) call in evaluation_tasks"
    missing = [
        n.lineno for n in eval_calls
        if "cognition_ledger_text" not in {kw.arg for kw in n.keywords}
    ]
    assert not missing, (
        "evaluator.evaluate(...) without cognition_ledger_text= at line(s) "
        f"{missing} — the standalone evaluation path must feed the ledger to "
        "the cognition_violation check"
    )

    load_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _call_name(n) == "load_ledger"
    ]
    assert load_calls, (
        "expected a load_ledger(...) call in evaluation_tasks — the ledger "
        "text must be loaded from the DB inside the step-1 session"
    )


@pytest.mark.asyncio
async def test_manual_evaluate_endpoint_passes_cognition_ledger(monkeypatch):
    """api/versions POST /evaluate: ledger is loaded via the volume reverse
    lookup and handed to ChapterEvaluator.evaluate as cognition_ledger_text."""
    import uuid
    from types import SimpleNamespace

    from app.api import versions as versions_api
    from app.models.project import Chapter, Volume
    from app.services import character_cognition
    from app.services.chapter_evaluator import ChapterEvaluator, EvaluationResult

    project_id = uuid.uuid4()
    volume_id = uuid.uuid4()
    chapter = SimpleNamespace(
        id=uuid.uuid4(),
        volume_id=volume_id,
        content_text="他推开门，看见了不该看见的东西。" * 10,
        outline_json={"beats": []},
    )
    volume = SimpleNamespace(id=volume_id, project_id=project_id)

    class _FakeDB:
        async def get(self, model, pk):
            if model is Chapter:
                return chapter
            if model is Volume:
                return volume if pk == volume_id else None
            return None

        def add(self, obj):
            pass

        async def flush(self):
            pass

        async def refresh(self, obj):
            pass

    ledger_loads: list = []

    async def _fake_load_ledger(db, pid):
        ledger_loads.append(pid)
        return {"林动": {"knows": ["秘密A"], "does_not_know": ["秘密B"]}}

    monkeypatch.setattr(character_cognition, "load_ledger", _fake_load_ledger)

    captured: dict = {}

    async def _fake_evaluate(self, **kwargs):
        captured.update(kwargs)
        return EvaluationResult(overall=4.0)

    monkeypatch.setattr(ChapterEvaluator, "evaluate", _fake_evaluate)

    await versions_api.evaluate_chapter(
        chapter_id=str(chapter.id),
        body=versions_api.EvaluateRequest(),
        db=_FakeDB(),
    )

    assert ledger_loads == [project_id], (
        "manual /evaluate must reverse-look-up project_id via the chapter's "
        "volume and load the cognition ledger for it"
    )
    assert captured.get("cognition_ledger_text"), (
        "manual /evaluate must pass a non-empty cognition_ledger_text to "
        "ChapterEvaluator.evaluate"
    )
