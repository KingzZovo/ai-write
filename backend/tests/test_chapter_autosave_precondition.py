"""自动保存的"作者先改过就别覆盖"判定(2026-11-17,A2)。

真实场景:桌面流式生成要几十秒到几分钟,作者在这期间改完并保存;流结束时的自动保存
原先无条件覆盖 `chapters.content_text`,把作者刚存的稿子换掉。
这里锁死判定本身;流水线侧的接线(行锁 + 冲突落候选)在 `api/generate.py`。
"""

from __future__ import annotations

from app.services.chapter_autosave import author_edited_since, content_hash


def test_hash_is_stable_and_distinguishes_edits():
    assert content_hash("周芸把信封压在碗下。") == content_hash("周芸把信封压在碗下。")
    assert content_hash("甲") != content_hash("乙")
    assert content_hash("") == content_hash(None)


def test_no_baseline_means_no_blocking():
    """请求开始时读不到基线(新章/未传 chapter_id)时不得拦正常保存。"""
    assert author_edited_since(None, "任何正文") is False


def test_unchanged_text_does_not_count_as_edit():
    baseline = content_hash("原稿")
    assert author_edited_since(baseline, "原稿") is False


def test_edited_text_counts_as_edit():
    baseline = content_hash("原稿")
    assert author_edited_since(baseline, "作者改过的稿子") is True


def test_author_writing_into_empty_chapter_counts_as_edit():
    """生成开始时是空章,期间作者写了内容 —— 必须算改过,不能覆盖。"""
    baseline = content_hash("")
    assert author_edited_since(baseline, "作者新写的一段") is True


def test_whitespace_only_difference_still_counts():
    """保守:只要字节不同就算改过(宁可多留候选,也不覆盖作者)。"""
    baseline = content_hash("原稿")
    assert author_edited_since(baseline, "原稿 ") is True