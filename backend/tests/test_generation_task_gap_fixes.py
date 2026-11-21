"""Regression tests for the celery generation-path gap fixes (2026-07-26 audit).

Covers, against app/tasks/generation_tasks.py:

1. Entity-extraction dispatch on celery chapter saves — the background task
   queue previously never enqueued Neo4j extraction, so async-generated
   projects got no states/locations/relationships at all.
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
import inspect
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
    """2026-11-21 契约更新:章后收尾收敛成**唯一实现**。

    旧契约要求"每个 ChapterVersion 保存点都直接调 _dispatch_chapter_entities"
    —— 那是三入口各写一套时代的补丁式约束。现在五个步骤(摘要/实体派发/卷桥接/
    伏笔/事实检查)都在 app/services/chapter_finalize.py,所以契约改成:

    1. 每个保存点都调统一收尾;
    2. 不允许绕过它直接派发实体或做卷桥接(唯一执行链)。
    """
    impl = _func(_parse_generation_tasks(), "_run_async_generation_impl")

    version_saves = [
        n for n in ast.walk(impl)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ChapterVersion"
    ]
    finalize_calls = [
        n for n in ast.walk(impl)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "finalize_chapter"
    ]
    bypass = [
        n for n in ast.walk(impl)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) in ("_dispatch_chapter_entities",
                                            "_backfill_volume_summary_safe")
    ]
    assert version_saves, "expected ChapterVersion saves in _run_async_generation_impl"
    assert finalize_calls, "队列入口必须调用统一收尾 finalize_chapter"
    assert len(finalize_calls) >= len(version_saves), (
        f"{len(version_saves)} save site(s) but only {len(finalize_calls)} finalize_chapter call(s)"
    )
    assert not bypass, "不允许绕过统一收尾直接派发实体/卷桥接(唯一执行链)"


def test_every_entity_dispatch_site_is_paired_with_volume_backfill():
    """实体派发与卷桥接必须在**同一处**发生 —— 现在那"一处"就是统一收尾。

    旧契约的由来是"needs_review 早返回漏掉了卷桥接",所以要求两件事配对出现。
    收敛之后二者都在 chapter_finalize.finalize_chapter 里,于是改为:
    该实现必须同时包含实体派发与卷桥接,且队列入口不得自行调它们。
    """
    from app.services import chapter_finalize as cf

    src = inspect.getsource(cf)
    assert "dispatch_for_chapter" in src, "统一收尾必须包含实体派发"
    assert "backfill_prev_volume_summary" in src, "统一收尾必须包含卷桥接"

    impl = _func(_parse_generation_tasks(), "_run_async_generation_impl")
    bypass = [
        n for n in ast.walk(impl)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) in ("_dispatch_chapter_entities",
                                            "_backfill_volume_summary_safe")
    ]
    assert not bypass, "队列入口不得绕过统一收尾自己调这两件事"


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
