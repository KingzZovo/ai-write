"""Regression: auto-revise rollback must not wipe a fresh chapter to empty.

Live failure (2026-06-29 全流程测试): for FRESH chapters (no prior content),
the C2 auto-revise loop saved several valid, on-genre drafts (ch3: 4775→5003
chars, scored ~8.0), then exhausted max_rounds without crossing the 8.2
threshold. The post-loop rollback restored `baseline_text_before_run` — which
is EMPTY for a fresh chapter — clobbering all the saved work to len=0/draft.

That directly defeats QUALITY_GATE_PERSIST_ON_BLOCK=1 (keep the best blocked
draft rather than stranding the chapter empty). The rollback's intent — "a
known-bad new draft must not replace a known-GOOD prior chapter" — only makes
sense when a prior chapter actually exists. When the baseline is empty, there
is nothing good to restore to, so the best saved draft must survive.
"""
from __future__ import annotations

import pytest

from app.api.generate import resolve_rollback_text


def test_rollback_restores_nonempty_baseline_over_new_draft():
    # Existing chapter had known-good prior content; a bad revise must not win.
    out = resolve_rollback_text(
        baseline_text="原有已确认正文（上一版）。",
        current_text="改坏了的新草稿。",
        persist_on_block=True,
    )
    assert out == "原有已确认正文（上一版）。"


def test_rollback_keeps_best_draft_when_baseline_empty_and_persist_on_block():
    # FRESH chapter (empty baseline) + valid saved draft + persist-on-block:
    # keep the draft instead of wiping to empty. This is the live ch3 case.
    draft = "江临在地下管网追查声呐站的完整一章正文……" * 50
    out = resolve_rollback_text(
        baseline_text="",
        current_text=draft,
        persist_on_block=True,
    )
    assert out == draft


def test_rollback_wipes_to_empty_when_persist_disabled():
    # Legacy behaviour preserved when the flag is off: empty baseline → empty.
    out = resolve_rollback_text(
        baseline_text="",
        current_text="新草稿但不保留。",
        persist_on_block=False,
    )
    assert out == ""


def test_rollback_empty_baseline_empty_current_stays_empty():
    out = resolve_rollback_text(
        baseline_text="",
        current_text="",
        persist_on_block=True,
    )
    assert out == ""
