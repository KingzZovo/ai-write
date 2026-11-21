"""Persist-status decision for chapter saves (PR-BLOCKED-STATUS 2026-07-26).

E2E found: a quality-gate-blocked chapter (too_short<0.70, 787 chars) was
persisted by persist-on-block and marked ``completed``. Truncation and
refusal already downgrade to draft; a block-persisted best-effort draft
must get the same treatment — README/CHANGELOG promised exactly that.
"""

from app.services.chapter_quality_gate import chapter_persist_status

# Ends with terminal punctuation, contains no refusal boilerplate.
CLEAN = "他把收音机搬回柜台，锁了门。巷子里的灯还亮着。"
TRUNCATED = "他把收音机搬回柜台，锁了门，然后"
REFUSAL = "您登录了吗？我目前似乎无法为您创建任何图片。"


def test_clean_text_no_meta_is_completed():
    assert chapter_persist_status(CLEAN, None) == "completed"


def test_clean_text_passed_gate_is_completed():
    meta = {"status": "passed", "rewrite_rounds": 1}
    assert chapter_persist_status(CLEAN, meta) == "completed"


def test_truncated_text_is_draft():
    assert chapter_persist_status(TRUNCATED, None) == "draft"


def test_refusal_text_is_draft():
    assert chapter_persist_status(REFUSAL, None) == "draft"


def test_persisted_on_block_is_draft_even_when_text_looks_clean():
    meta = {"status": "blocked", "warning_reason": "too_short<0.70", "persisted_on_block": True}
    assert chapter_persist_status(CLEAN, meta) == "draft"


def test_needs_review_block_persist_is_draft():
    meta = {"status": "needs_review", "persisted_on_block": True}
    assert chapter_persist_status(CLEAN, meta) == "draft"


def test_meta_without_block_flag_does_not_downgrade():
    # A gate that ran but passed cleanly must not force draft.
    meta = {"status": "passed", "persisted_on_block": False}
    assert chapter_persist_status(CLEAN, meta) == "completed"
