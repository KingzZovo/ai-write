"""Regression tests for the celery generation-path gap fixes (2026-07-26 audit).

Covers, against app/tasks/generation_tasks.py:

1. Entity-extraction dispatch on celery chapter saves — the background task
   queue previously never enqueued Neo4j extraction, so async-generated
   projects got no states/locations/relationships at all.
2. Deterministic humanizer — the anti-AI pass previously mutated prose with
   the global ``random`` module BEFORE evaluation (irreproducible output,
   unattributable score variance).
3. Whole-book pipeline persist hardening — sanitizer + truncation/refusal
   checks now downgrade to ``draft`` instead of unconditional ``completed``,
   and max_tokens was raised to stop mid-sentence cutoffs.
4. needs_review staging runs the prose sanitizer like every other save path.

The big celery entrypoints open their own sessions around long LLM calls and
are not practically unit-testable; structural invariants are pinned via AST
tripwires (same style as tests/test_cognition_ingestion_gating.py) while the
extracted helpers are unit-tested directly.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import app
from app.tasks import generation_tasks as gt

_APP_DIR = Path(app.__file__).resolve().parent


def _parse_generation_tasks() -> ast.Module:
    return ast.parse(
        (_APP_DIR / "tasks" / "generation_tasks.py").read_text(encoding="utf-8")
    )


def _func(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in generation_tasks.py")


# ---------------------------------------------------------------------------
# 1. Entity dispatch on celery saves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_chapter_entities_calls_dispatch_for_chapter(monkeypatch):
    from app.services import entity_dispatch

    calls: list[tuple] = []

    async def fake_dispatch_for_chapter(chapter, db, *, caller, project_id_hint=None):
        calls.append((chapter, db, caller, project_id_hint))
        return True

    monkeypatch.setattr(entity_dispatch, "dispatch_for_chapter", fake_dispatch_for_chapter)

    ch = SimpleNamespace(id="c1", chapter_idx=3, volume_id="v1")
    await gt._dispatch_chapter_entities(ch, "db-session", "proj-1")

    assert calls == [(ch, "db-session", "tasks.run_async_generation", "proj-1")]


@pytest.mark.asyncio
async def test_dispatch_chapter_entities_never_raises(monkeypatch):
    from app.services import entity_dispatch

    async def boom(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(entity_dispatch, "dispatch_for_chapter", boom)

    # Must swallow: chapter persistence never fails because of the graph queue.
    await gt._dispatch_chapter_entities(SimpleNamespace(chapter_idx=1), None, "p")


def test_every_celery_chapter_version_save_has_entity_dispatch():
    """Every ChapterVersion save site in the async-generation task must be
    paired with an entity-extraction dispatch (the SSE path dispatches on every
    persist; the celery path previously dispatched on none)."""
    impl = _func(_parse_generation_tasks(), "_run_async_generation_impl")

    version_saves = [
        n
        for n in ast.walk(impl)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ChapterVersion"
    ]
    dispatch_calls = [
        n
        for n in ast.walk(impl)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "_dispatch_chapter_entities"
    ]
    assert version_saves, "expected ChapterVersion saves in _run_async_generation_impl"
    assert len(dispatch_calls) >= len(version_saves), (
        f"{len(version_saves)} ChapterVersion save site(s) but only "
        f"{len(dispatch_calls)} _dispatch_chapter_entities call(s) — a celery "
        "save path is missing its Neo4j entity-extraction dispatch"
    )


# ---------------------------------------------------------------------------
# 2. Deterministic humanizer
# ---------------------------------------------------------------------------


def _eligible_line(i: int) -> str:
    # >15 chars before the first comma and total length >60: qualifies for the
    # punctuation-variation branch.
    return (
        f"陈青沿着旧纸巷慢慢往前走了第{i}遍，"
        "巷子两侧的门板都合着口，雨水顺着瓦沿往下滴，他把伞压得更低了一些，"
        "脚步没有停，拐角那家铺子的灯还亮着半扇窗，纸页翻动的声音隔着雨幕传出来。"
    )


def test_humanizer_same_input_same_output():
    text = "\n".join(_eligible_line(i) for i in range(50))
    out1 = gt._humanize_chapter_text(text, seed_key="chapter-1")
    out2 = gt._humanize_chapter_text(text, seed_key="chapter-1")
    assert out1 == out2


def test_humanizer_transformation_branch_still_fires():
    # 50 eligible lines at p=0.3 — the deterministic RNG splits at least one
    # long sentence, so the pass still does its job (this is stable because the
    # RNG is seeded, not a flaky probabilistic assertion).
    text = "\n".join(_eligible_line(i) for i in range(50))
    out = gt._humanize_chapter_text(text, seed_key="chapter-1")
    assert out != text.strip()


def test_humanizer_varies_across_chapters():
    # Different chapter identity -> different seed -> the intended variety
    # across chapters is preserved. Stable: both outputs are deterministic.
    text = "\n".join(_eligible_line(i) for i in range(50))
    out_a = gt._humanize_chapter_text(text, seed_key="chapter-a")
    out_b = gt._humanize_chapter_text(text, seed_key="chapter-b")
    assert out_a != out_b


def test_humanizer_trailing_symmetry_rule_kept():
    out = gt._humanize_chapter_text("他把灯关了，而", seed_key="x")
    assert out == "他把灯关了。"


# ---------------------------------------------------------------------------
# 3. Pipeline persist hardening
# ---------------------------------------------------------------------------


def _pipeline_chapter() -> SimpleNamespace:
    return SimpleNamespace(id="ch-1", content_text=None, word_count=0, status="pending")


def test_pipeline_persist_completed_for_clean_text():
    ch = _pipeline_chapter()
    flagged = gt._persist_pipeline_chapter_text(ch, "他终于把门关上了。" * 5)
    assert flagged is False
    assert ch.status == "completed"
    assert ch.word_count == len(ch.content_text)


def test_pipeline_persist_draft_on_truncation():
    ch = _pipeline_chapter()
    flagged = gt._persist_pipeline_chapter_text(
        ch, "他推开门，看见桌上放着一封没有署名的"
    )
    assert flagged is True
    assert ch.status == "draft"
    # Text is kept — nothing is lost, only the status is downgraded.
    assert ch.content_text.startswith("他推开门")


def test_pipeline_persist_draft_on_refusal():
    ch = _pipeline_chapter()
    flagged = gt._persist_pipeline_chapter_text(
        ch, "我无法创建图片，请换一个请求。"
    )
    assert flagged is True
    assert ch.status == "draft"


def test_pipeline_persist_strips_meta_leakage():
    ch = _pipeline_chapter()
    gt._persist_pipeline_chapter_text(
        ch, "第10章的伏笔在这里回收。他终于把门关上了。"
    )
    assert "第10章" not in ch.content_text
    assert "他终于把门关上了。" in ch.content_text


def test_pipeline_generation_max_tokens_raised():
    """8192 tokens regularly truncated the ≥3000-char requirement mid-sentence."""
    impl = _func(_parse_generation_tasks(), "_run_pipeline_async")
    max_tokens = [
        kw.value.value
        for n in ast.walk(impl)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "generate"
        for kw in n.keywords
        if kw.arg == "max_tokens" and isinstance(kw.value, ast.Constant)
    ]
    assert max_tokens == [16384]


def test_pipeline_has_no_unconditional_completed_status():
    """chapter.status must go through _persist_pipeline_chapter_text, never be
    assigned "completed" directly in the pipeline loop."""
    impl = _func(_parse_generation_tasks(), "_run_pipeline_async")
    bad = [
        n.lineno
        for n in ast.walk(impl)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Attribute)
        and t.attr == "status"
        and isinstance(getattr(t, "value", None), ast.Name)
        and t.value.id == "chapter"
        and isinstance(n.value, ast.Constant)
        and n.value.value == "completed"
    ]
    assert not bad, f"unconditional chapter.status='completed' at line(s) {bad}"


# ---------------------------------------------------------------------------
# 4. needs_review staging sanitization
# ---------------------------------------------------------------------------


def test_stage_needs_review_sanitizes_meta_leakage():
    task = SimpleNamespace(
        status=None, error_message=None, result_text=None,
        progress_text=None, char_count=0,
    )
    ch = SimpleNamespace(content_text=None, word_count=0, status=None)

    out = gt._stage_needs_review_chapter_text(
        task, ch, "[CH-3] 他终于把门关上了。上一章的债，他记得。",
        error_message="quality_gate blocked",
    )

    assert "[CH-3]" not in out
    assert "上一章" not in out
    assert "他终于把门关上了。" in out
    # Sanitized text propagates to every field the UI reads.
    assert task.result_text == out
    assert task.progress_text == out
    assert ch.content_text == out
    assert task.char_count == len(out) == ch.word_count
    assert task.status == "needs_review"
    assert ch.status == "needs_review"


# ---------------------------------------------------------------------------
# 5. Volume-summary backfill on EVERY persist branch (E2E defect 2026-07-26:
#    vol2 ch1 persisted needs_review, all vol1 prerequisites met, yet no
#    VolumeSummary row — the early-return branches skipped the backfill).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_volume_summary_safe_calls_memory_helper(monkeypatch):
    from app.services import memory

    calls: list[str] = []

    async def fake_backfill(volume_id):
        calls.append(volume_id)
        return True

    monkeypatch.setattr(memory, "backfill_prev_volume_summary", fake_backfill)

    await gt._backfill_volume_summary_safe("vol-123")
    assert calls == ["vol-123"]


@pytest.mark.asyncio
async def test_backfill_volume_summary_safe_noop_without_volume_id(monkeypatch):
    from app.services import memory

    called = []

    async def fake_backfill(volume_id):
        called.append(volume_id)
        return True

    monkeypatch.setattr(memory, "backfill_prev_volume_summary", fake_backfill)
    await gt._backfill_volume_summary_safe(None)
    await gt._backfill_volume_summary_safe("")
    assert called == []


@pytest.mark.asyncio
async def test_backfill_volume_summary_safe_never_raises(monkeypatch):
    from app.services import memory

    async def boom(volume_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(memory, "backfill_prev_volume_summary", boom)
    # Must swallow: the backfill is fire-safe and never fails the save.
    await gt._backfill_volume_summary_safe("vol-123")


def test_every_entity_dispatch_site_is_paired_with_volume_backfill():
    """Every chapter-persist branch in the async-generation task dispatches
    entity extraction; each of those sites must ALSO fire the volume-summary
    backfill (the needs_review early returns previously skipped it)."""
    impl = _func(_parse_generation_tasks(), "_run_async_generation_impl")

    dispatch_calls = [
        n
        for n in ast.walk(impl)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "_dispatch_chapter_entities"
    ]
    backfill_calls = [
        n
        for n in ast.walk(impl)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "_backfill_volume_summary_safe"
    ]
    assert dispatch_calls, "expected entity-dispatch sites in _run_async_generation_impl"
    assert len(backfill_calls) >= len(dispatch_calls), (
        f"{len(dispatch_calls)} entity-dispatch site(s) but only "
        f"{len(backfill_calls)} _backfill_volume_summary_safe call(s) — a "
        "persist branch is missing its cross-volume memory backfill"
    )


# ---------------------------------------------------------------------------
# 6. Async style resolution uses the production chain (no raw sample-passage
#    few-shot): settings-declared style_id must go through
#    style_runtime.production_style_text_for_profile, not bare compile_style.
# ---------------------------------------------------------------------------


def test_async_style_resolution_uses_production_helper():
    impl = _func(_parse_generation_tasks(), "_run_async_generation_impl")

    call_names = {
        getattr(n.func, "id", None)
        for n in ast.walk(impl)
        if isinstance(n, ast.Call)
    }
    assert "production_style_text_for_profile" in call_names, (
        "async path must resolve style via "
        "style_runtime.production_style_text_for_profile"
    )
    assert "compile_style" not in call_names, (
        "async path must not call compile_style directly — it injects raw "
        "sample passages that production paths strip"
    )
