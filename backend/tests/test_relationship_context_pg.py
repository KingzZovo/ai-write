"""Real source receipts, Neo4j event intervals and generation card assembly."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.models.project import Chapter, Character, CharacterState
from app.services.chapter_entity_extraction import FIELDS
from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.entity_context import verified_world_snapshot
from app.services import entity_projection as ep
from tests.test_entity_invalidation import clean_env, change
from tests.test_entity_projection import env, run, source
from tests.test_pipeline_control import runtime_db


def relations(body, *, changed=False):
    payload = {key: [] for key in FIELDS}
    field = 'changed_relationships' if changed else 'new_relationships'
    payload[field] = [dict(char1='陈川', char2='沈青', type=kind, evidence=body)
                      for kind in (['敌对'] if changed else ['同事', '朋友'])]
    return payload


@pytest.mark.asyncio
async def test_relationship_only_extraction_reaches_actual_fact_builder(clean_env, monkeypatch):
    e = clean_env
    body = '陈川与沈青既是同事，也是朋友。'
    await change(e, content_text=body)
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=relations(body)))
    await run(e)
    async def driver():
        yield e.driver
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    async with e.r.sessions() as db:
        assert await db.scalar(select(CharacterState)) is None
        assert set((await db.execute(select(Character.name))).scalars()) >= {'陈川', '沈青'}
        pack = ContextPack(global_chapter_idx=2, history_before_global_idx=2)
        await ContextPackBuilder(db=db)._build_facts(pack, e.r.project, 2, 2)
        prompt = pack.to_system_prompt()
        assert '同事' in prompt and '朋友' in prompt and '陈川 → 沈青' in prompt
        assert len(pack.relationship_records) == 2


@pytest.mark.asyncio
async def test_real_closed_intervals_are_recomputed_when_the_closing_source_is_edited(clean_env, monkeypatch):
    e = clean_env
    body = '陈川与沈青既是同事，也是朋友。'
    await change(e, content_text=body)
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=relations(body)))
    await run(e)
    async with e.r.sessions() as db:
        original = await db.get(Chapter, e.r.chapter)
        later = Chapter(volume_id=original.volume_id, chapter_idx=2, global_idx=2,
                        title='反目', status='completed', content_text='陈川与沈青反目。')
        db.add(later)
        await db.commit()
        later_id = later.id
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=relations('陈川与沈青反目。', changed=True)))
    await run(e, await source(e.r, later_id))
    async with e.r.sessions() as db:
        before = await verified_world_snapshot(db, e.driver, e.r.project, 3)
        assert [rel.rel_type for rel in before.relationships] == ['敌对']
        await db.execute(update(Chapter).where(Chapter.id == later_id).values(content_text='作者取消了反目的情节。'))
        await db.commit()
        # No graph invalidation worker has run. Old graph edges are still closed.
        after = await verified_world_snapshot(db, e.driver, e.r.project, 3)
        assert {rel.rel_type for rel in after.relationships} == {'同事', '朋友'}
        assert all(rel.chapter_start == 1 and rel.source_chapter_id == str(e.r.chapter) for rel in after.relationships)


@pytest.mark.asyncio
async def test_real_graph_relationship_read_preserves_full_type_and_current_chapter_boundary(clean_env, monkeypatch):
    e = clean_env
    body = '陈川与沈青暂时合作，仍各自提防。'
    await change(e, content_text=body)
    payload = {key: [] for key in FIELDS}
    description = '暂时合作（仍各自提防，尚未互相信任）'
    payload['new_relationships'] = [dict(char1='陈川', char2='沈青', type=description, evidence=body)]
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=payload))
    await run(e)
    async with e.r.sessions() as db:
        assert not (await verified_world_snapshot(db, e.driver, e.r.project, 1)).relationships
        rel = (await verified_world_snapshot(db, e.driver, e.r.project, 2)).relationships[0]
        assert rel.rel_type == description and rel.event_kind == 'new' and rel.source_hash
