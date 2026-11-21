"""The actual prompt must carry current neighbouring prose after author edits."""
import pytest
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db
from tests.test_context_cross_volume import seed
from app.models.project import Chapter
from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.context_budget import ContextBudgetExceeded


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'needs_review'])
async def test_edited_previous_prose_survives_missing_summary_and_final_budget(runtime_db, status):
    r = runtime_db
    _, target, _, _ = await seed(r)
    revised = '钥匙在陈川衣袋，清单锁在值班柜。\n沈青收好了笔，离开车站。'
    async with r.sessions() as db:
        chapter = await db.scalar(select(Chapter).where(Chapter.chapter_idx == 100))
        chapter.content_text = revised
        chapter.status = status
        await db.commit()
    async with r.sessions() as db:
        pack = ContextPack()
        await ContextPackBuilder(db=db)._build_proximity(pack, r.project, target, 1)
        prompt = pack.to_system_prompt(token_budget=8000)
        assert revised in prompt
        assert '上一卷末，负伤过桥' not in prompt
        assert '后卷正文。' not in pack.previous_chapter_text
        assert 'VOL-1/CH-100' in pack.previous_chapter_text
        assert '正文全文' in pack.previous_chapter_text
        assert ('待审草稿参考' in pack.previous_chapter_text) == (status != 'completed')
        trace = next(x for x in pack.budget_trace if x['source'] == 'previous_chapter_text')
        assert trace['required'] and trace['status'] == 'included'


@pytest.mark.asyncio
async def test_long_previous_chapter_is_bounded_and_tail_is_labelled(runtime_db):
    r = runtime_db
    _, target, _, _ = await seed(r)
    async with r.sessions() as db:
        chapter = await db.scalar(select(Chapter).where(Chapter.chapter_idx == 100))
        chapter.content_text = '很早的正文。' * 100000 + '\n最后完整一段：门已经锁好。'
        await db.commit()
    async with r.sessions() as db:
        pack = ContextPack()
        await ContextPackBuilder(db=db)._build_proximity(pack, r.project, target, 1)
        assert '最后完整一段：门已经锁好。' in pack.previous_chapter_text
        assert '结尾节选' in pack.previous_chapter_text
        assert len(pack.previous_chapter_text) < 3900
        assert '很早的正文' not in pack.previous_chapter_text


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['empty', 'duplicate', 'moved'])
async def test_nearest_source_is_not_silently_replaced_with_unrelated_prose(runtime_db, change):
    r = runtime_db
    v1, target, _, alien = await seed(r)
    async with r.sessions() as db:
        chapter = await db.scalar(select(Chapter).where(Chapter.chapter_idx == 100))
        chapter.content_text = '唯一前章正文。'
        if change == 'empty':
            chapter.content_text = ''
        elif change == 'duplicate':
            db.add(Chapter(volume_id=v1, chapter_idx=100, title='重复章', content_text='任意重复章正文。'))
        else:
            chapter.volume_id = alien
        await db.commit()
    async with r.sessions() as db:
        pack = ContextPack()
        await ContextPackBuilder(db=db)._build_proximity(pack, r.project, target, 1)
        text = pack.previous_chapter_text
        assert '唯一前章正文。' not in text and '任意重复章正文。' not in text
        if change == 'empty':
            assert '尚无正文' in text and '原稿。' not in text
        elif change == 'duplicate':
            assert '章号重复' in text
        else:
            assert '原稿。' in text


def test_budget_cannot_silently_omit_required_previous_prose():
    pack = ContextPack(previous_chapter_text='【紧邻前章当前正文】\n' + '当前原文。' * 500,
                       current_outline={'summary': '接着写'})
    with pytest.raises(ContextBudgetExceeded, match='previous_chapter_text'):
        pack.to_system_prompt(token_budget=200)


@pytest.mark.asyncio
async def test_cached_orm_status_cannot_mislabel_new_author_draft(runtime_db):
    r = runtime_db
    _, target, _, _ = await seed(r)
    async with r.sessions() as stale:
        old = await stale.scalar(select(Chapter).where(Chapter.chapter_idx == 100))
        old.status = 'completed'
        await stale.commit()
        async with r.sessions() as author:
            changed = await author.get(Chapter, old.id)
            changed.content_text = '新改稿：钥匙没有交出。'
            changed.status = 'needs_review'
            await author.commit()
        pack = ContextPack()
        await ContextPackBuilder(db=stale)._build_proximity(pack, r.project, target, 1)
        assert '新改稿：钥匙没有交出。' in pack.previous_chapter_text
        assert '待审草稿参考' in pack.previous_chapter_text
        assert 'status=needs_review' in pack.previous_chapter_text
        assert old.status == 'completed'  # Reading did not refresh away caller state.
