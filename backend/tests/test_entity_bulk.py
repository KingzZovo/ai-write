"""Real PG batch size, upsert semantics and whole-transaction rollback."""
from uuid import uuid4
import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from app.models import Base
from app.models.project import Character, CharacterState, Location, Foreshadow
from app.services.entity_bulk import upsert_projection_rows
from tests.test_pipeline_control import runtime_db

@pytest_asyncio.fixture
async def batch_db(runtime_db):
    r=runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync:Base.metadata.create_all(sync,tables=[Character.__table__,CharacterState.__table__,Location.__table__,Foreshadow.__table__]))
    async with r.sessions() as db:
        c=Character(project_id=r.project,name='江枫');db.add(c);await db.commit();r.character=c.id
    return r


def states(r,count):
    return [dict(id=uuid4(),project_id=r.project,character_id=r.character,chapter_start=i+1,
                 chapter_end=None,status_json={'状态':f'变化{i}', '其它':{'甲':1,'乙':2}},source_chapter_id=r.chapter,
                 source_hash='a'*64,source_mode='chapter_delta') for i in range(count)]

@pytest.mark.asyncio
async def test_5000_state_rows_fit_driver_and_conflict_updates_preserve_sources(batch_db):
    r=batch_db;rows=states(r,5000)
    async with r.sessions() as db:
        assert await upsert_projection_rows(db,CharacterState,rows,constraint='uq_character_states_key',
            update_columns=('status_json','source_hash','source_mode','chapter_end'))==5000
        async with r.sessions() as reader:
            assert await reader.scalar(select(func.count()).select_from(CharacterState))==0
        await db.commit()
        assert await upsert_projection_rows(db,CharacterState,rows,constraint='uq_character_states_key',
            update_columns=('status_json','source_hash','source_mode','chapter_end'))==0
        await db.commit()
        equivalent=dict(rows[-1],status_json={'其它':{'乙':2,'甲':1},'状态':'变化4999'})
        assert await upsert_projection_rows(db,CharacterState,[equivalent],constraint='uq_character_states_key',
            update_columns=('status_json','source_hash','source_mode','chapter_end'))==0
        changed=dict(rows[-1],status_json={'状态':'更新'},source_hash='b'*64,chapter_end=5001)
        await upsert_projection_rows(db,CharacterState,[changed],constraint='uq_character_states_key',
            update_columns=('status_json','source_hash','source_mode','chapter_end'))
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(CharacterState))==5000
        row=await db.scalar(select(CharacterState).where(CharacterState.chapter_start==5000))
        assert row.status_json=={'状态':'更新'} and row.source_hash=='b'*64 and row.chapter_end==5001

@pytest.mark.asyncio
async def test_late_batch_failure_rolls_back_earlier_batch(batch_db):
    r=batch_db;rows=states(r,600);rows[-1]['character_id']=uuid4()
    async with r.sessions() as db:
        with pytest.raises(IntegrityError):
            await upsert_projection_rows(db,CharacterState,rows,constraint='uq_character_states_key')
        await db.rollback()
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CharacterState))==0

@pytest.mark.asyncio
async def test_do_nothing_deduplicates_across_batch_boundaries(batch_db):
    r=batch_db;rows=[dict(project_id=r.project,name=f'地点{i}') for i in range(1000)]
    async with r.sessions() as db:
        assert await upsert_projection_rows(db,Location,rows+rows[:10],constraint='uq_locations_project_name')==1000
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(Location))==1000

@pytest.mark.asyncio
async def test_primary_key_upsert_keeps_foreshadow_contract(batch_db):
    r=batch_db
    rows=[dict(id=uuid4(),project_id=r.project,type='线索',description='初始伏笔',planted_chapter=i+1,source='neo4j') for i in range(601)]
    async with r.sessions() as db:
        await upsert_projection_rows(db,Foreshadow,rows,update_columns=('description','source'))
        await db.commit()
        changed=dict(rows[-1],description='修订伏笔')
        await upsert_projection_rows(db,Foreshadow,[changed],update_columns=('description','source'))
        await db.commit()
        assert (await db.get(Foreshadow,rows[-1]['id'])).description=='修订伏笔'
        assert await db.scalar(select(func.count()).select_from(Foreshadow))==601
