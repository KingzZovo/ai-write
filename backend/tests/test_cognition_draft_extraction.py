"""有正文但状态被降级的章必须能被提取认知(2026-11-28 长篇系统性缺陷)。

现场证据(《白蜡树下》500 章 / 267 个 AI 生成章):只有 7 章是在"前置认知齐备"时
生成的;260 章平均缺 96.3 章前置认知,最多缺 294 章。按卷号取样,生成第 50 章时
只丢 1 章前文事实,第 200 章丢 61 章,第 450 章丢 258 章,第 500 章丢 293 章 ——
与实测缺口完全吻合。

机制是自增强回路:章 N 评分 7.9 → ``needs_review`` → 提取侧只认 ``completed``
(``allow_review_drafts=False``),这一章的认知**永远**提不出来 → 台账
``_ledger_position_query`` 却**有意**把它算进 boundary,于是静默跳过它的事实 →
章 N+1 在缺少前文事实的条件下写 → 更差 → 更多 ``needs_review``。

正文存在就等于事实存在;评审态是编辑意见,不该决定"书里已经发生的事要不要被记住"。
"""
import pytest

from app.models.project import Chapter
from app.services.entity_sources import resolve_entity_sources
from tests.test_version_application import book  # noqa: F401 - fixture


@pytest.mark.asyncio
async def test_review_draft_with_body_can_be_resolved_when_allowed(book):
    chapter = book.session.get(Chapter, book.cid)
    chapter.status = 'needs_review'
    book.session.commit()

    default = await resolve_entity_sources(book.db, book.pid, [book.cid], lock=False)
    assert str(book.cid) not in default, '默认语义仍是"只认 completed",不能悄悄放宽'

    allowed = await resolve_entity_sources(book.db, book.pid, [book.cid], lock=False,
                                          allow_review_drafts=True)
    assert str(book.cid) in allowed, '降级章有正文时必须能取到来源,否则台账永远缺事实'


@pytest.mark.asyncio
async def test_empty_draft_stays_out_even_when_drafts_are_allowed(book):
    """没有正文就没有事实可提取 —— 放宽状态不等于放过空章。"""
    chapter = book.session.get(Chapter, book.cid)
    chapter.status = 'draft'
    chapter.content_text = ''
    book.session.commit()

    allowed = await resolve_entity_sources(book.db, book.pid, [book.cid], lock=False,
                                          allow_review_drafts=True)
    assert str(book.cid) not in allowed