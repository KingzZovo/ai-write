"""空章回填:停止分支只把候选留在 task 里,章节为空时必须落库。

没有这些用例时,卷 1 的 35/38/39/40/41 长期是 ``draft`` + ``body=0`` ——
正文其实生成成功了,却因为局部修订一条都定不到位置(`spans_revised == 0`)
而走"停止分支",候选只写进 ``GenerationTask.result_text``,章节一个字都没落。

契约边界(不许放宽):
- **只回填空章**,绝不覆盖作者已有稿 —— 那是 test_revision_stop_terminal 锁住的语义;
- 回填后状态是 ``needs_review``(候选未经评审通过),不能冒充 ``completed``;
- 必须同步 ``status.saved_hash``,否则 ``_outcome`` 的防伪校验仍判 failed。
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.production.chapter_executor import _backfill_empty_chapter, content_hash


@pytest.mark.asyncio
async def test_backfills_empty_chapter_from_retained_candidate():
    chapter = NS(id='c1', content_text='', word_count=0, status='draft')
    task = NS(result_text='第一段。\n\n第二段。')
    status = NS(saved_hash=None, word_count=0)
    db = NS(flush=AsyncMock())

    await _backfill_empty_chapter(db, chapter, task, status)

    assert chapter.content_text == '第一段。\n\n第二段。'
    assert chapter.word_count == len(chapter.content_text)
    assert chapter.status == 'needs_review'
    # 防伪校验按 saved_hash 比对,不同步就会让这一章依旧判 failed。
    assert status.saved_hash == content_hash(chapter.content_text)
    assert status.word_count == len(chapter.content_text)
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_never_overwrites_author_draft():
    chapter = NS(id='c2', content_text='作者当前稿。', word_count=6, status='draft')
    task = NS(result_text='模型候选正文。')
    status = NS(saved_hash='keep-me', word_count=6)
    db = NS(flush=AsyncMock())

    await _backfill_empty_chapter(db, chapter, task, status)

    assert chapter.content_text == '作者当前稿。'
    assert status.saved_hash == 'keep-me'
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_candidate_leaves_chapter_untouched():
    chapter = NS(id='c3', content_text='   ', word_count=0, status='draft')
    task = NS(result_text='')
    status = NS(saved_hash=None, word_count=0)
    db = NS(flush=AsyncMock())

    await _backfill_empty_chapter(db, chapter, task, status)

    assert chapter.content_text == '   '
    assert chapter.status == 'draft'
    assert status.saved_hash is None
    db.flush.assert_not_awaited()