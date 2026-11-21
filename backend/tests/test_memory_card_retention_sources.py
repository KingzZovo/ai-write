"""Real PG checks: stale metadata never pins or misclassifies excerpt cards."""
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import select
from app.models.project import Chapter, CharacterAppearance, CharacterMemoryCard
from app.services.entity_sources import resolve_entity_source
from app.tasks.entity_tasks import _upsert_memory_cards
from app.services.context_pack import ContextPack, ContextPackBuilder, CharacterCard
from tests.test_historical_roster import roster_db
from tests.test_pipeline_control import runtime_db

@pytest_asyncio.fixture
async def cards_db(roster_db, monkeypatch):
    r=roster_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: CharacterMemoryCard.__table__.create(c))
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    return r

@pytest.mark.asyncio
async def test_first_label_uses_live_previous_mentions_not_stale_roster(cards_db):
    r=cards_db
    async with r.sessions() as db:
        ch=await db.scalar(select(Chapter).where(Chapter.global_idx==2))
        ch.content_text='陈川走到门边。'
        db.add(CharacterAppearance(project_id=r.project,character_name='陈川',first_seen_chapter=99,last_seen_chapter=99))
        await db.commit()
        src=await resolve_entity_source(db,r.project,2,ch.id)
    for _ in range(2):
        await _upsert_memory_cards(project_id=str(r.project),global_idx=2,chapter_text=src.content,
                                  source_chapter_id=src.chapter_id,source_hash=src.source_hash)
    async with r.sessions() as db:
        rows=(await db.scalars(select(CharacterMemoryCard))).all()
        assert len(rows)==1 and rows[0].card_type=='key_moment'
        assert rows[0].excerpt==src.content

@pytest.mark.asyncio
async def test_stale_cards_and_duplicate_first_labels_do_not_displace_valid_recent_cards(cards_db,monkeypatch):
    r=cards_db
    monkeypatch.setattr('app.config.settings.MEMORY_CARDS_PER_CHARACTER',2)
    async with r.sessions() as db:
        first=await db.get(Chapter,r.chapter)
        for idx in [2,3]:
            ch=await db.scalar(select(Chapter).where(Chapter.global_idx==idx))
            ch.content_text=f'沈青在第{idx}天走到门边。'
        ch=Chapter(volume_id=first.volume_id,chapter_idx=4,global_idx=4,status='completed',
                   title='新章',content_text='沈青把门关上。')
        db.add(ch);await db.commit()
        current=await resolve_entity_source(db,r.project,4,ch.id)
        for idx in [1,2,3]:
            src=await resolve_entity_source(db,r.project,idx)
            db.add(CharacterMemoryCard(project_id=r.project,character_name='沈青',global_idx=idx,
                card_type='first_appearance',excerpt=src.content,source_chapter_id=src.chapter_id,
                source_hash='stale' if idx==1 else src.source_hash))
        await db.commit()
    result=await _upsert_memory_cards(project_id=str(r.project),global_idx=4,chapter_text=current.content,
                                    source_chapter_id=current.chapter_id,source_hash=current.source_hash)
    assert result['cards_evicted']==2
    async with r.sessions() as db:
        rows=(await db.scalars(select(CharacterMemoryCard).order_by(CharacterMemoryCard.global_idx))).all()
        assert [(x.global_idx,x.card_type) for x in rows]==[(2,'first_appearance'),(4,'key_moment')]
        pack=ContextPack();pack.character_cards=[CharacterCard(name='沈青')]
        await ContextPackBuilder(db=db)._build_returning_drilldown(pack,str(r.project),30)
        assert '[CH-2·早期片段]' in pack.returning_character_drilldown
        assert '[CH-4·片段]' in pack.returning_character_drilldown
        assert '初登场' not in pack.returning_character_drilldown

@pytest.mark.asyncio
async def test_reader_keeps_recent_verified_cards_with_legacy_first_labels(cards_db):
    r=cards_db
    async with r.sessions() as db:
        for idx in [1,2,3]:
            ch=await db.scalar(select(Chapter).where(Chapter.global_idx==idx));ch.content_text=f'沈青走过第{idx}扇门。'
        await db.commit()
        for idx in [1,2,3]:
            src=await resolve_entity_source(db,r.project,idx)
            db.add(CharacterMemoryCard(project_id=r.project,character_name='沈青',global_idx=idx,
                card_type='first_appearance',excerpt=src.content,source_chapter_id=src.chapter_id,source_hash=src.source_hash))
        await db.commit()
        pack=ContextPack();pack.character_cards=[CharacterCard(name='沈青')]
        await ContextPackBuilder(db=db)._build_returning_drilldown(pack,str(r.project),30)
        assert all(f'[CH-{idx}·早期片段]' in pack.returning_character_drilldown for idx in [1,2,3])
