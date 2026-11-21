"""Real managed Neo4j transactions and PostgreSQL Project locks; offline skips."""
import asyncio

import pytest
from sqlalchemy import update

from app.models.project import Outline, WorldRule
from app.services.outline_settings_source import read_settings_source, require_current_settings_source
from app.services.outline_settings_graph import write_settings_graph
from app.services.world_rule_material import WorldRuleSourceError, world_rule_material, world_rule_version
from app.services.world_rule_sources import read_world_rule_source
from app.services.world_rule_writes import write_world_rule
from tests.test_pipeline_control import runtime_db
from tests.test_entity_projection import env

pytestmark = pytest.mark.external_services


async def current(e):
    async with e.r.sessions() as db:
        return await read_world_rule_source(db, e.r.project, driver=e.driver)


async def write(e, operation, **kwargs):
    async with e.r.sessions() as db:
        result = await write_world_rule(db, e.driver, e.r.project, operation, **kwargs)
        await db.commit()
        return result


@pytest.mark.asyncio
async def test_real_graph_create_edit_merge_and_delete_preserve_confirmed_identity(env):
    e = env
    first = await write(e, 'create', category='航运', text='汛期封航。')
    duplicate = await write(e, 'create', category='航运', text='汛期封航。')
    assert duplicate['id'] == first['id']
    row = (await current(e))['rules'][0]
    assert row['id'] == first['id'] and row['origin'] == 'author'
    await write(e, 'update', category='航运', text='汛期封航。', new_category='洪水',
                new_text='水位回落且测量完成后才准通航。', expected_version=world_rule_version(row))
    changed = (await current(e))['rules'][0]
    assert changed['id'] == first['id'] and changed['rule_text'] == '水位回落且测量完成后才准通航。'
    target = await write(e, 'create', category='渡口', text='夜间不开船。')
    result = await write(e, 'update', category=changed['category'], text=changed['rule_text'],
                        new_category='渡口', new_text='夜间不开船。', expected_version=world_rule_version(changed))
    assert result['deduplicated'] is True
    rows = (await current(e))['rules']
    assert len(rows) == 1 and rows[0]['id'] == target['id']
    result = await write(e, 'delete', category='渡口', text='夜间不开船。', expected_version=world_rule_version(rows[0]))
    assert result['deleted'] == 1 and (await current(e))['rules'] == []


@pytest.mark.asyncio
async def test_real_extraction_basis_and_author_adoption_retain_original_provenance(env):
    e = env
    async with e.r.sessions() as db:
        book = Outline(project_id=e.r.project, level='book', is_confirmed=1,
                       content_json={'raw_text': '铜铃损坏后不能复原。'})
        db.add(book); await db.commit()
        source = await read_settings_source(db, e.r.project, book.id)
        await db.rollback()
        source = await require_current_settings_source(db, source)
        await write_settings_graph(e.driver, source, {'characters': [], 'relationships': [],
            'world_rules': [{'category': '能力限制', 'rule_text': '铜铃损坏后不能复原。'}]}, {})
        await db.commit()
    initial = await current(e)
    row = initial['rules'][0]
    assert row['source_outline_material_hash'] == source.material_hash
    assert world_rule_material(initial, e.r.project)[0]['source_status'] == 'current_outline'
    async with e.r.sessions() as db:
        await db.execute(update(Outline).where(Outline.id == source.outline_id).values(
            content_json={'raw_text': '作者修改了铜铃来历，旧提取需要核对。'}))
        await db.commit()
    with pytest.raises(WorldRuleSourceError):
        world_rule_material(await current(e), e.r.project)
    await write(e, 'update', category=row['category'], text=row['rule_text'],
                new_category=row['category'], new_text=row['rule_text'], expected_version=world_rule_version(row))
    adopted = await current(e)
    assert world_rule_material(adopted, e.r.project)[0]['source_status'] == 'author'
    assert adopted['rules'][0]['source_outline_material_hash'] == source.material_hash
    assert adopted['rules'][0]['source_outline_id'] == str(source.outline_id)


@pytest.mark.asyncio
async def test_project_lock_allows_only_one_edit_with_the_same_original_version(env):
    e = env
    await write(e, 'create', category='航运', text='汛期封航。')
    row = (await current(e))['rules'][0]
    async def edit(text):
        return await write(e, 'update', category=row['category'], text=row['rule_text'],
            new_category=row['category'], new_text=text, expected_version=world_rule_version(row))
    results = await asyncio.wait_for(asyncio.gather(edit('水位回落再通航。'), edit('堤坝检修后再通航。'),
                                                  return_exceptions=True), 10)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, WorldRuleSourceError) and result.status in (404, 409) for result in results) == 1
    assert len((await current(e))['rules']) == 1


@pytest.mark.asyncio
async def test_rule_write_project_lock_does_not_deadlock_separate_projection_fk_check(env):
    e = env
    async with e.r.engine.begin() as conn:
        await conn.run_sync(lambda db: WorldRule.__table__.create(db, checkfirst=True))
    async with e.r.sessions() as owner:
        await write_world_rule(owner, e.driver, e.r.project, 'create', category='渡口', text='封航。')
        async def project():
            async with e.r.sessions() as db:
                db.add(WorldRule(project_id=e.r.project, category='渡口', rule_text='封航。'))
                await db.commit()
        await asyncio.wait_for(project(), 5)
        await owner.commit()
