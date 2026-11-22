"""已有正文的章默认不重写(2026-11-22)。

为什么不重写:每次重跑都会改写正文 → ``content_hash`` 变 → 该章此前按旧 hash
绑定的认知记录全部作废,而下一章的认知边界要求"它之前所有章**当前正文**的认知"
为 completed —— 于是边界立刻失效、整卷自锁。实测卷 1 反复在 completed=0~2
打转,认知表里 completed 有 11 条却一条都用不上。

需要真重写时传 ``options.force_rewrite=true``。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.production.chapter_executor import _should_skip_existing_chapter


def _chapter(body="正文", status="completed"):
    return SimpleNamespace(content_text=body, status=status)


def test_completed_chapter_with_body_is_skipped():
    assert _should_skip_existing_chapter(_chapter(), {}) is True
    assert _should_skip_existing_chapter(_chapter(), None) is True


def test_force_rewrite_opts_out():
    assert _should_skip_existing_chapter(_chapter(), {"force_rewrite": True}) is False


def test_falsey_force_rewrite_still_skips():
    """只有真值才强制重写,避免 'false'/'0' 这类字符串意外触发全量重写。"""
    assert _should_skip_existing_chapter(_chapter(), {"force_rewrite": False}) is True


def test_draft_chapter_is_still_generated():
    """有正文但仍是草稿(例如 needs_review 之后被置回)—— 照旧走生成。"""
    assert _should_skip_existing_chapter(_chapter(status="draft"), {}) is False


def test_chapter_without_body_is_generated():
    assert _should_skip_existing_chapter(_chapter(body=""), {}) is False
    assert _should_skip_existing_chapter(_chapter(body="   \n "), {}) is False


def test_missing_chapter_is_not_skipped():
    assert _should_skip_existing_chapter(None, {}) is False
