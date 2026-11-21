"""Real SQL coverage for cross-volume windows and volume-outline ownership."""
import pytest
from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, Outline, Project, Volume, VolumeSummary
from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.summary_provenance import summary_source_hash


async def seed(r):
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Outline.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: VolumeSummary.__table__.create(c))
    async with r.sessions() as db:
        first = await db.get(Chapter, r.chapter)
        v1 = await db.get(Volume, first.volume_id)
        v2 = Volume(project_id=r.project, volume_idx=2, title='第二卷')
        v3 = Volume(project_id=r.project, volume_idx=3, title='第三卷')
        other = Project(title='别人的书')
        db.add_all([v2, v3, other]); await db.flush()
        alien = Volume(project_id=other.id, volume_idx=1, title='别人的卷')
        db.add(alien); await db.flush()
        # Deliberate chapter-number gaps: last five summaries, not idx-5.
        db.add(Chapter(volume_id=v1.id, chapter_idx=100, title='卷末', summary='上一卷末，负伤过桥', content_text='上卷末正文，尚未放下密信', outline_json={'summary':'上卷细纲'}))
        current = Chapter(volume_id=v2.id, chapter_idx=1, title='新卷', outline_json={'summary':'本章唯一任务'}, content_text='当前正文')
        db.add_all([current, Chapter(volume_id=v3.id, chapter_idx=1, title='将来', content_text='后卷正文。', summary='未来事实不可提前获知'),
                    Chapter(volume_id=alien.id, chapter_idx=99, title='秘密', summary='别人的秘密')])
        db.add_all([Outline(project_id=r.project, level='volume', version=1, content_json={'volume_idx':2,'title':'正确卷大纲'}),
                    Outline(project_id=r.project, level='volume', version=99, content_json={'volume_idx':3,'title':'错误卷大纲'}),
                    Outline(project_id=r.project, level='volume', version=100, content_json={'volume_idx':2,'volume_id':str(v3.id),'title':'ID冲突大纲'}),
                    Outline(project_id=r.project, level='volume', version=101, content_json={'title':'无归属大纲'}),
                    VolumeSummary(volume_id=v1.id, summary_text='过去卷摘要'),
                    VolumeSummary(volume_id=v3.id, summary_text='未来卷摘要')])
        for chapter in [first, *(obj for obj in db.new if isinstance(obj, Chapter))]:
            if chapter.summary:
                chapter.summary_source_hash = summary_source_hash(chapter.title, chapter.content_text, chapter.summary)
        await db.commit()
        return v1.id, v2.id, v3.id, alien.id


@pytest.mark.asyncio
async def test_new_volume_receives_previous_chapters_without_future_leak(runtime_db):
    r=runtime_db; _, v2, _, _ = await seed(r)
    async with r.sessions() as db:
        pack=ContextPack()
        await ContextPackBuilder(db=db)._build_proximity(pack,r.project,v2,1)
        past='\n'.join(pack.recent_summaries)
        assert '上一卷末，负伤过桥' in past and 'VOL-1/CH-100' in past
        assert '原摘要' in past and '过去卷摘要' not in past  # Legacy aggregate has no source receipt.
        assert '未来' not in past and '别人的秘密' not in past
        assert pack.current_content == '当前正文'
        assert pack.current_outline == {'summary':'本章唯一任务'}
        assert 'VOL-3/CH-1' in '\n'.join(pack.future_outlines)
        assert pack.volume_outline['title'] == '正确卷大纲'
        prompt=pack.to_system_prompt()
        assert '上一卷末，负伤过桥' in prompt


@pytest.mark.asyncio
async def test_first_volume_has_no_later_volume_summary(runtime_db):
    r=runtime_db; v1, _, _, _ = await seed(r)
    async with r.sessions() as db:
        pack=ContextPack()
        await ContextPackBuilder(db=db)._build_proximity(pack,r.project,v1,1)
        assert pack.recent_summaries == []
        assert pack.volume_outline == {}


@pytest.mark.asyncio
async def test_proximity_rejects_other_project_volume(runtime_db):
    r=runtime_db; _, _, _, alien = await seed(r)
    async with r.sessions() as db:
        with pytest.raises(ValueError, match='不属于'):
            await ContextPackBuilder(db=db)._build_proximity(ContextPack(),r.project,alien,99)


@pytest.mark.asyncio
async def test_expansion_receives_previous_volume_prose(runtime_db, monkeypatch):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.services.chapter_outline_expander import _gather_context
    from app.services import foreshadow_lifecycle
    r=runtime_db; _, v2, _, _ = await seed(r)
    monkeypatch.setattr(foreshadow_lifecycle, 'load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    async with r.sessions() as db:
        chapter=await db.scalar(select(Chapter).where(Chapter.volume_id == v2, Chapter.chapter_idx == 1))
        ctx=await _gather_context(db, chapter)
        assert ctx['prev_text'] == '上卷末正文，尚未放下密信'
        assert ctx['prev_outline'] == {'summary':'上卷细纲'}
        assert ctx['volume_outline']['title'] == '正确卷大纲'


@pytest.mark.asyncio
async def test_duplicate_local_index_does_not_choose_arbitrary_prose(runtime_db):
    r=runtime_db; _, v2, _, _ = await seed(r)
    async with r.sessions() as db:
        db.add(Chapter(volume_id=v2, chapter_idx=1, title='重复章', content_text='不应选中'))
        await db.commit()
        with pytest.raises(ValueError, match='章号重复'):
            await ContextPackBuilder(db=db)._build_proximity(ContextPack(),r.project,v2,1)


@pytest.mark.asyncio
async def test_confirmed_volume_plan_precedes_newer_unapproved_revision(runtime_db):
    r=runtime_db; _, v2, _, _ = await seed(r)
    async with r.sessions() as db:
        db.add(Outline(project_id=r.project, level='volume', version=0, is_confirmed=1,
                       content_json={'volume_id':str(v2),'title':'作者已确认大纲'}))
        await db.commit()
        pack=ContextPack()
        await ContextPackBuilder(db=db)._build_proximity(pack,r.project,v2,1)
        assert pack.volume_outline['title'] == '作者已确认大纲'
