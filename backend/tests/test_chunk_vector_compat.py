"""切块算法变更后,【原文回读】不能因为边界重排而静默归零。

2026-11-17 把章级切块从"按字数凑段落"改为结构感知(对白不切断、场景分隔优先)。
`filter_current_memory` 是原文召回的唯一把关点,它原先要求
`payload["text"] == parts[payload["chunk_seq"]]`;边界一变,所有既有 point 都不匹配,
于是整本书的 chunk 召回会被静默过滤干净(和"换嵌入模型导致召回 0 点"同类)。

这里锁死的规则:
- 正文改过(sha256 变了)→ 旧点必须失败;
- 正文没改,只是切块边界变了 → 只要这段文本仍逐字存在于当前正文,就算有效。
"""

from __future__ import annotations

import hashlib

from app.services.vector_provenance import _chunk_vector_matches

TEXT = "周芸把信封压在碗下。\n\n船家收起缆绳,江面起了雾。\n\n她把存根折好放进抽屉。"
PARTS = ["周芸把信封压在碗下。", "船家收起缆绳,江面起了雾。", "她把存根折好放进抽屉。"]
DIGEST = hashlib.sha256(TEXT.encode()).hexdigest()


def _payload(**over):
    base = {"chunk_seq": 1, "text": PARTS[1], "source_content_hash": DIGEST}
    base.update(over)
    return base


def test_exact_current_chunk_is_kept():
    assert _chunk_vector_matches(_payload(), (DIGEST, PARTS, TEXT)) is True


def test_edited_chapter_invalidates_old_point():
    """正文字节变了:旧引文可能已不存在,必须失败(这是引文可信度的底线)。"""
    stale = _payload(source_content_hash="0" * 64)
    assert _chunk_vector_matches(stale, (DIGEST, PARTS, TEXT)) is False


def test_rechunked_boundaries_still_keep_the_excerpt():
    """切块算法变了(seq 越界 / 文本与新边界不同),但正文没改:仍算当前记忆。"""
    # 新算法切得更粗:只剩一个 chunk,旧点的 seq=1 越界。
    assert _chunk_vector_matches(_payload(chunk_seq=1), (DIGEST, [TEXT], TEXT)) is True
    # 新算法在同一 seq 上放了别的文本,旧引文仍逐字存在。
    assert _chunk_vector_matches(_payload(text="江面起了雾。"), (DIGEST, ["完全不同的一段。"], TEXT)) is True


def test_excerpt_absent_from_current_text_is_rejected():
    """形式上是"旧边界",但那段文字在当前正文里找不到 —— 不能当当前记忆。"""
    payload = _payload(text="这一段已经不在正文里了。")
    assert _chunk_vector_matches(payload, (DIGEST, PARTS, TEXT)) is False


def test_missing_or_non_string_text_is_rejected():
    assert _chunk_vector_matches(_payload(text=None), (DIGEST, PARTS, TEXT)) is False
    assert _chunk_vector_matches(_payload(text=""), (DIGEST, PARTS, TEXT)) is False
    assert _chunk_vector_matches(_payload(text=["不是字符串"]), (DIGEST, PARTS, TEXT)) is False


def test_bad_seq_type_falls_back_to_substring_check():
    """seq 不是 int 时不能直接崩,也不能一概丢弃。"""
    assert _chunk_vector_matches(_payload(chunk_seq="1"), (DIGEST, PARTS, TEXT)) is True
    assert _chunk_vector_matches(_payload(chunk_seq=None, text="查无此段"), (DIGEST, PARTS, TEXT)) is False