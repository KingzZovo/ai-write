import pytest
from tests.test_pipeline_control import runtime_db
from tests.test_context_cross_volume import seed
from app.models.project import Chapter
from app.services.evaluation_neighbors import load_evaluation_neighbors
from app.services.chapter_evaluator import _build_user_prompt
from sqlalchemy import select


@pytest.mark.asyncio
async def test_neighbors_cross_volumes_keep_future_separate(runtime_db):
    r = runtime_db
    _, v2, _, _ = await seed(r)
    async with r.sessions() as db:
        current = await db.scalar(select(Chapter).where(Chapter.volume_id == v2))
        neighbors = await load_evaluation_neighbors(db, current.id, r.project)
        assert '上一卷末，负伤过桥' in neighbors.previous_context
        assert 'VOL-1/CH-100' in neighbors.previous_context
        assert '待审草稿' in neighbors.previous_context
        assert '尚未放下密信' in neighbors.previous_tail
        assert '未来事实' not in neighbors.previous_context
        assert 'VOL-3/CH-1' in neighbors.following_context
        assert '不能据此认定本章事件已发生' in neighbors.following_context
        assert '别人的秘密' not in neighbors.previous_context + neighbors.following_context
        prompt = _build_user_prompt('当前正文', {}, neighbors.previous_context, '', [],
                                    following_context=neighbors.following_context)
        assert prompt.index('后续章节衔接参考（不是已发生事实）') < prompt.index('未来事实')


@pytest.mark.asyncio
async def test_unwritten_neighbor_is_not_silently_skipped(runtime_db):
    r = runtime_db
    _, v2, _, _ = await seed(r)
    async with r.sessions() as db:
        current = await db.scalar(select(Chapter).where(Chapter.volume_id == v2))
        current.chapter_idx = 20
        db.add(Chapter(volume_id=v2, chapter_idx=5, title='尚未写作', content_text='', summary=''))
        await db.commit()
        neighbors = await load_evaluation_neighbors(db, current.id, r.project)
        assert 'CH-5' in neighbors.previous_context
        assert '尚无正文或摘要' in neighbors.previous_context
        assert neighbors.previous_tail == ''
        assert '上卷末正文' not in neighbors.previous_context


@pytest.mark.asyncio
async def test_neighbors_reject_wrong_project_and_duplicate_index(runtime_db):
    from uuid import uuid4
    r = runtime_db
    async with r.sessions() as db:
        with pytest.raises(ValueError, match='不属于'):
            await load_evaluation_neighbors(db, r.chapter, uuid4())
        current = await db.get(Chapter, r.chapter)
        db.add(Chapter(volume_id=current.volume_id, chapter_idx=current.chapter_idx, title='重复'))
        await db.commit()
        with pytest.raises(ValueError, match='重复'):
            await load_evaluation_neighbors(db, current.id, r.project)


@pytest.mark.asyncio
async def test_previous_prose_is_bounded_in_sql_and_labeled(runtime_db):
    r = runtime_db
    _, v2, _, _ = await seed(r)
    async with r.sessions() as db:
        current = await db.scalar(select(Chapter).where(Chapter.volume_id == v2))
        previous = await db.scalar(select(Chapter).where(Chapter.chapter_idx == 100))
        previous.content_text = '正文开头不应读取' + '中' * 5000 + '最后状态。'
        previous.status = 'completed'
        await db.commit()
        neighbors = await load_evaluation_neighbors(db, current.id, r.project)
        assert len(neighbors.previous_tail) == 3500
        assert neighbors.previous_tail.endswith('最后状态。')
        assert '正文开头不应读取' not in neighbors.previous_context
        assert '正文结尾节选' in neighbors.previous_context
        assert '待审草稿' not in neighbors.previous_context
