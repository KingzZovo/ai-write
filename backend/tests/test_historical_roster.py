"""Real PostgreSQL historical mention retrieval, independent of stale caches."""
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import delete, update
from app.models.project import Chapter, Character, CharacterAppearance, Volume, Project
from app.services.character_roster import load_historical_roster, render_roster_block
from app.services.context_pack import ContextPack, ContextPackBuilder, CharacterCard
from tests.test_pipeline_control import runtime_db

@pytest_asyncio.fixture
async def roster_db(runtime_db):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Character.__table__.create(c))
        await conn.run_sync(lambda c: CharacterAppearance.__table__.create(c))
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.global_idx = 1; ch.status = 'completed'; ch.content_text = '沈青提起陈川。老陈还没有回来。'
        db.add_all([Character(project_id=r.project, name='沈青'),
                    Character(project_id=r.project, name='陈川', profile_json={'aliases':['老陈']}),
                    Character(project_id=r.project, name='后来者'),
                    CharacterAppearance(project_id=r.project, character_name='后来者',
                                        last_seen_chapter=99, appearance_count=9999)])
        db.add(Chapter(volume_id=ch.volume_id, chapter_idx=2, global_idx=2,
                       title='当前章', status='completed', content_text='后来者' * 50))
        db.add(Chapter(volume_id=ch.volume_id, chapter_idx=3, global_idx=3,
                       title='未来章', status='completed', content_text='后来者' * 100))
        await db.commit()
    return r

@pytest.mark.asyncio
async def test_current_future_and_stale_totals_do_not_rank_or_render(roster_db, monkeypatch):
    r=roster_db
    async with r.sessions() as db:
        rows=await load_historical_roster(db,r.project,before_global_idx=2)
        assert {x['character_name']:x['appearance_count'] for x in rows}=={'沈青':1,'陈川':2}
        assert all(x['last_seen_chapter']==1 for x in rows)
        text=render_roster_block(rows,2)
        assert '后来者' not in text and '提及不等于出场或知情' in text
        pack=ContextPack(global_chapter_idx=2)
        pack.character_cards=[CharacterCard(name=n) for n in ['后来者','沈青','陈川']]
        monkeypatch.setattr('app.config.settings.CONTEXT_PACK_MAX_CHARACTER_CARDS',1)
        await ContextPackBuilder(db=db)._cap_character_cards(pack,str(r.project))
        assert [c.name for c in pack.character_cards]==['陈川']
        assert await load_historical_roster(db,r.project,before_global_idx=0)==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['edit','draft','delete','duplicate','move','reindex','unknown'])
async def test_author_changes_visible_without_recompute(roster_db,mutation):
    r=roster_db
    async with r.sessions() as db:
        ch=await db.get(Chapter,r.chapter)
        if mutation=='edit':ch.content_text='窗外没有人。'
        elif mutation=='draft':ch.status='draft'
        elif mutation=='delete':await db.execute(delete(Chapter).where(Chapter.id==r.chapter))
        elif mutation=='duplicate':db.add(Chapter(volume_id=ch.volume_id,chapter_idx=4,global_idx=1,title='重号草稿'))
        elif mutation=='move':
            p=Project(title='别的书');db.add(p);await db.flush()
            v=Volume(project_id=p.id,volume_idx=1,title='卷');db.add(v);await db.flush();ch.volume_id=v.id
        elif mutation=='reindex':ch.global_idx=9
        else:ch.global_idx=None
        await db.commit()
        assert await load_historical_roster(db,r.project,before_global_idx=2)==[]

@pytest.mark.asyncio
async def test_alias_changes_and_no_autoflush(roster_db):
    r=roster_db
    async with r.sessions() as db:
        await db.execute(update(Character).where(Character.name=='陈川').values(profile_json={}))
        await db.commit()
        ch=await db.get(Chapter,r.chapter);ch.content_text='尚未提交的作者编辑。'
        db.flush=AsyncMock(side_effect=AssertionError('read must not flush'))
        rows=await load_historical_roster(db,r.project,before_global_idx=2)
        assert {x['character_name']:x['appearance_count'] for x in rows}=={'沈青':1,'陈川':1}
        db.flush.assert_not_awaited()

@pytest.mark.asyncio
async def test_five_million_characters_streamed_with_historical_boundary(roster_db):
    import time
    from sqlalchemy import insert
    r=roster_db
    async with r.sessions() as db:
        chapter=await db.get(Chapter,r.chapter);vid=chapter.volume_id
        await db.execute(delete(Chapter))
        body='沈青'+'风'*2498
        # 2000 chapters, 5 million characters; 100 tracked names including
        # an alias. The later half must not influence this historical read.
        db.add_all([Character(project_id=r.project,name=f'配角{idx:03d}') for idx in range(97)])
        for start in range(1,2001,200):
            await db.execute(insert(Chapter),[dict(volume_id=vid,chapter_idx=i,global_idx=i,
                title=f'第{i}章',status='completed',content_text=body) for i in range(start,start+200)])
        await db.commit()
        started=time.monotonic()
        rows=await load_historical_roster(db,r.project,before_global_idx=1001)
        elapsed=time.monotonic()-started
        assert rows==[{'character_name':'沈青','first_seen_chapter':1,
                      'last_seen_chapter':1000,'appearance_count':1000}]
        print(f'5000000-char book / 2500000-char historical read: {elapsed:.3f}s')


@pytest.mark.asyncio
async def test_related_recall_uses_earlier_mention_despite_future_cached_last_seen(roster_db):
    from app.models.project import Foreshadow, CharacterState
    from app.services.related_chapters import find_related_chapters
    from app.services.summary_provenance import summary_source_hash
    r=roster_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Foreshadow.__table__.create(c))
        await conn.run_sync(lambda c: CharacterState.__table__.create(c))
    async with r.sessions() as db:
        ch=await db.get(Chapter,r.chapter)
        ch.summary='沈青提起陈川，陈川还没有回来。'
        ch.summary_source_hash=summary_source_hash(ch.title,ch.content_text,ch.summary)
        db.add(CharacterAppearance(project_id=r.project,character_name='陈川',
                                   last_seen_chapter=99,appearance_count=999))
        await db.commit()
        rows=await find_related_chapters(db,r.project,None,2,{'summary':'沈青和陈川继续交谈'},
                                        min_total_chapters=0,min_signals=1,
                                        foreshadow_references=[])
        assert rows and rows[0]['chapter']==1
        assert any('上次提及' in reason for reason in rows[0]['reasons'])
