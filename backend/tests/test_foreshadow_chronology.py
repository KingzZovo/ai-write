import json
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, insert, select, update, delete
from sqlalchemy.orm import Session
from sqlalchemy.dialects import postgresql

from app.models.project import (Project, Volume, Chapter, Outline, Foreshadow, ForeshadowSourceLink,
    ForeshadowDiscoveryRun as Run, ForeshadowDiscovery as Candidate)
from app.services.entity_sources import digest
from app.services.foreshadow_discovery import source_snapshot
from app.services.foreshadow_lifecycle import (
    chapter_global_idx, load_active_foreshadows_for_context, format_active_foreshadows_for_prompt,
)
from app.services.foreshadow_plans import read_foreshadow_plans, plan_entries
from app.services.foreshadow_sources import inspect_foreshadow_sources
from app.services.memory_chronology import resolve_history_boundary


@pytest.fixture
def book():
    engine = create_engine('sqlite://')
    for model in (Project, Volume, Chapter, Outline, Foreshadow, Run, Candidate, ForeshadowSourceLink):
        model.__table__.create(engine)
    with Session(engine) as session:
        pid, foreign = uuid4(), uuid4()
        volumes, chapters = [uuid4() for _ in range(3)], [uuid4() for _ in range(4)]
        session.execute(insert(Project), [{'id': pid, 'title': '测试'}, {'id': foreign, 'title': '另一本'}])
        session.execute(insert(Volume), [dict(id=vid, project_id=pid, title=f'卷{i}', volume_idx=i)
                                        for i, vid in enumerate(volumes, 1)])
        positions = [(volumes[0], 1, 11), (volumes[1], 1, 51), (volumes[1], 201, 301), (volumes[2], 1, 401)]
        for cid, (vid, local, gidx) in zip(chapters, positions):
            session.execute(insert(Chapter).values(id=cid, volume_id=vid, chapter_idx=local, global_idx=gidx,
                title='信', status='completed', content_text='陈伯把寄信的存根放在桌上。', outline_json={}))
        session.commit()
        queries = []
        async def execute(statement):
            queries.append(statement)
            return session.execute(statement)
        r = NS(engine=engine, session=session, db=NS(execute=execute), pid=pid, foreign=foreign,
               volumes=volumes, chapters=chapters, queries=queries)
        yield r
    engine.dispose()


def add_clue(r, chapter, description='寄信的存根', *, legacy=False):
    fid = uuid4()
    position = r.session.execute(select(Chapter).where(Chapter.id == chapter)).scalar_one()
    r.session.execute(insert(Foreshadow).values(id=fid, project_id=r.pid, type='mystery',
        description=description, planted_chapter=0 if legacy else position.global_idx,
        source='lifecycle', status='planted', resolve_conditions_json=['说明存根的用途']))
    if not legacy:
        snapshot = source_snapshot(NS(project_id=r.pid, chapter_id=chapter, global_idx=position.global_idx,
            content=position.content_text, receipt={'volume_id': position.volume_id, 'chapter_idx': position.chapter_idx}))
        run_id = r.session.execute(select(Run.id).where(Run.chapter_id == chapter)).scalar_one_or_none()
        if run_id is None:
            run_id = uuid4()
            r.session.execute(insert(Run).values(id=run_id, project_id=r.pid, chapter_id=chapter,
                source_sha256=digest(snapshot), source_snapshot=snapshot, candidate_count=1))
        evidence = '寄信的存根'
        start = position.content_text.index(evidence)
        r.session.execute(insert(Candidate).values(id=uuid4(), run_id=run_id, foreshadow_id=fid,
            description=description, type='mystery', evidence=evidence, evidence_start=start,
            evidence_end=start + len(evidence), reason='留存物件，待核对后文用途', status='accepted'))
    r.session.commit()
    return fid


def volume_outline(r, planted, *, vid=None, confirmed=0, version=1, **metadata):
    vid = vid or r.volumes[1]
    oid = uuid4()
    r.session.execute(insert(Outline).values(id=oid, project_id=r.pid, level='volume',
        is_confirmed=confirmed, version=version, content_json={
            'volume_id': str(vid), 'volume_idx': 2, 'foreshadows': {'planted': planted}, **metadata}))
    r.session.commit()
    return oid


def set_chapter_plan(r, cid, entries):
    r.session.execute(update(Chapter).where(Chapter.id == cid).values(outline_json={'foreshadows_planted': entries}))
    r.session.commit()


@pytest.mark.asyncio
async def test_persisted_positions_replace_counting_and_reject_unknown_scope(book):
    r = book
    assert await chapter_global_idx(r.db, r.pid, 2, 1) == 51
    assert await resolve_history_boundary(r.db, str(r.pid), str(r.volumes[1]), 1) == 51
    assert await resolve_history_boundary(r.db, str(r.foreign), str(r.volumes[1]), 1) == 0
    with pytest.raises(ValueError): await chapter_global_idx(r.db, r.foreign, 2, 1)
    with pytest.raises(ValueError): await load_active_foreshadows_for_context(r.db, r.pid)
    assert all(q.get_execution_options().get('autoflush') is False for q in r.queries)


@pytest.mark.asyncio
async def test_only_current_prior_prose_enters_context_not_current_future_or_unknown(book):
    r = book
    prior = add_clue(r, r.chapters[0], '前章线索')
    add_clue(r, r.chapters[1], '当前章线索')
    add_clue(r, r.chapters[3], '后卷线索')
    add_clue(r, r.chapters[0], '旧编号零', legacy=True)
    r.queries.clear()
    items = await load_active_foreshadows_for_context(r.db, r.pid, 51, limit=None)
    assert [row['id'] for row in items] == [prior]
    text = format_active_foreshadows_for_prompt(items)
    assert '第1卷第1章（全书位置11）' in text and '寄信的存根' in text
    assert all(word not in text for word in ('后卷线索', '当前章线索', '旧编号零', '间隔'))
    body_queries = [q for q in r.queries if 'chapters.content_text' in str(q)]
    assert len(body_queries) == 1
    params = body_queries[0].compile(dialect=postgresql.dialect()).params
    assert [r.chapters[0]] in params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['prose', 'description', 'type', 'planted_position', 'volume_order', 'delete', 'duplicate_position'])
async def test_stale_or_reordered_clues_do_not_enter_prior_context(book, change):
    r = book
    fid = add_clue(r, r.chapters[0])
    if change == 'prose': r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(content_text='作者删去了存根。'))
    elif change == 'description': r.session.execute(update(Foreshadow).where(Foreshadow.id == fid).values(description='另一件事'))
    elif change == 'type': r.session.execute(update(Foreshadow).where(Foreshadow.id == fid).values(type='plot'))
    elif change == 'planted_position': r.session.execute(update(Foreshadow).where(Foreshadow.id == fid).values(planted_chapter=2))
    elif change == 'volume_order':
        r.session.execute(update(Volume).where(Volume.id == r.volumes[0]).values(volume_idx=3))
        r.session.execute(update(Volume).where(Volume.id == r.volumes[2]).values(volume_idx=1))
    elif change == 'delete': r.session.execute(delete(Chapter).where(Chapter.id == r.chapters[0]))
    elif change == 'duplicate_position': r.session.execute(update(Chapter).where(Chapter.id == r.chapters[2]).values(global_idx=11))
    r.session.commit()
    assert await load_active_foreshadows_for_context(r.db, r.pid, 51) == []
    assert r.session.execute(select(Foreshadow.id)).scalar_one() == fid


@pytest.mark.asyncio
async def test_duplicate_target_cannot_authorize_any_history(book):
    r = book
    r.session.execute(update(Chapter).where(Chapter.id == r.chapters[2]).values(global_idx=51))
    r.session.commit()
    with pytest.raises(ValueError): await load_active_foreshadows_for_context(r.db, r.pid, 51)
    assert await resolve_history_boundary(r.db, str(r.pid), str(r.volumes[1]), 1) == 0


@pytest.mark.asyncio
async def test_volume_plans_do_not_invent_a_first_chapter_and_large_local_indices_are_exact(book):
    r = book
    volume_outline(r, [{'description': '未安排到某章的线索'}, {'description': '二百零一章', 'planted_chapter': 201}])
    result = await read_foreshadow_plans(r.db, r.pid)
    unscheduled, scheduled = result['plans']
    assert unscheduled['planted_chapter'] is None and unscheduled['chapter_id'] is None
    assert '未指定章节' in unscheduled['source_label']
    assert scheduled['planted_chapter'] == 301 and scheduled['chapter_id'] == str(r.chapters[2])
    items = await load_active_foreshadows_for_context(r.db, r.pid, 51, limit=None)
    assert [p['description'] for p in items] == ['未安排到某章的线索']
    assert r.session.execute(select(Foreshadow.id)).all() == []


@pytest.mark.asyncio
async def test_current_chapter_plan_follows_edits_and_preserves_full_text_without_fact_writes(book):
    r = book
    description, hint = '完整计划' * 250, '回应条件' * 220
    set_chapter_plan(r, r.chapters[1], [{'description': description, 'resolve_conditions': hint}])
    first = (await read_foreshadow_plans(r.db, r.pid))['plans'][0]
    assert first['description'] == description and first['hint'] == hint and first['planted_chapter'] == 51
    first_prompt = format_active_foreshadows_for_prompt([first])
    assert description in first_prompt and hint in first_prompt
    set_chapter_plan(r, r.chapters[1], ['作者改过的计划'])
    second = (await read_foreshadow_plans(r.db, r.pid))['plans'][0]
    assert second['description'] == '作者改过的计划' and second['id'] != first['id']
    set_chapter_plan(r, r.chapters[1], [])
    assert (await read_foreshadow_plans(r.db, r.pid))['plans'] == []
    assert r.session.execute(select(Foreshadow.id)).all() == []


@pytest.mark.asyncio
async def test_selected_outline_version_can_remove_old_plans_and_stable_ids_do_not_fall_back(book):
    r = book
    old = volume_outline(r, ['旧稿'], confirmed=1)
    volume_outline(r, ['未确认新稿'], version=2)
    assert [p['description'] for p in (await read_foreshadow_plans(r.db, r.pid))['plans']] == ['旧稿']
    volume_outline(r, [], confirmed=1, version=3)
    assert (await read_foreshadow_plans(r.db, r.pid))['plans'] == []
    r.session.execute(delete(Outline))
    r.session.commit()
    volume_outline(r, ['外部卷'], volume_id=str(uuid4()))
    assert (await read_foreshadow_plans(r.db, r.pid))['plans'] == []


@pytest.mark.parametrize('raw', [False, {}, '文字', [None], ['有效', 1],
    [{'description': '计划', 'planted_chapter': True}], [{'description': '计划', 'planted_chapter': 1.5}],
    [{'description': '计划', 'planted_chapter': 0}], [{'description': '计划', 'planted_chapter': '1'}]])
def test_invalid_plan_arrays_are_not_guessed_or_partially_read(raw):
    with pytest.raises(ValueError): plan_entries(raw)


@pytest.mark.asyncio
async def test_conflicting_chapter_index_reports_source_instead_of_changing_position(book):
    r = book
    set_chapter_plan(r, r.chapters[1], ['正常条目', {'description': '误写全书号', 'planted_chapter': 51}])
    result = await read_foreshadow_plans(r.db, r.pid)
    assert result == {'plans': [], 'unavailable_sources': 1}
    with pytest.raises(ValueError, match='foreshadow_plan_sources_unavailable'):
        await load_active_foreshadows_for_context(r.db, r.pid, 51)


@pytest.mark.asyncio
async def test_future_malformed_plan_does_not_block_current_chapter(book):
    r = book
    set_chapter_plan(r, r.chapters[3], False)
    volume_outline(r, [], vid=r.volumes[2], foreshadows=False)
    assert await load_active_foreshadows_for_context(r.db, r.pid, 51) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['prose', 'plan'])
async def test_position_change_between_metadata_and_source_read_cannot_reuse_old_position(book, kind):
    r = book
    if kind == 'prose': add_clue(r, r.chapters[0])
    else: set_chapter_plan(r, r.chapters[0], ['旧位置的计划'])
    execute = r.db.execute
    async def change_then_execute(statement):
        target = 'chapters.content_text' if kind == 'prose' else "chapters.outline_json["
        if target in str(statement):
            r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(global_idx=999))
        return await execute(statement)
    r.db.execute = change_then_execute
    if kind == 'prose':
        assert await load_active_foreshadows_for_context(r.db, r.pid, 51) == []
    else:
        with pytest.raises(ValueError, match='foreshadow_plan_sources_unavailable'):
            await load_active_foreshadows_for_context(r.db, r.pid, 51)


@pytest.mark.asyncio
async def test_volume_generation_reads_only_earlier_volumes(book):
    r = book
    prior = add_clue(r, r.chapters[0], '前卷')
    add_clue(r, r.chapters[1], '本卷')
    add_clue(r, r.chapters[3], '后卷')
    volume_outline(r, ['本卷计划'])
    items = await load_active_foreshadows_for_context(r.db, r.pid, target_volume_idx=2)
    assert [p['id'] for p in items] == [prior]


@pytest.mark.asyncio
async def test_source_metadata_and_paginated_plans_are_scoped_real_api_responses(book):
    from app.api.foreshadows import router
    from app.db.session import get_db
    r = book
    known = add_clue(r, r.chapters[0], '正文登记')
    legacy = add_clue(r, r.chapters[0], '旧号', legacy=True)
    set_chapter_plan(r, r.chapters[1], ['计划甲', '计划乙', '计划丙'])
    app = FastAPI()
    app.include_router(router)
    async def test_db():
        return r.db
    app.dependency_overrides[get_db] = test_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get(f'/api/projects/{r.pid}/foreshadows')
        assert response.status_code == 200, response.text
        rows = {UUID(row['id']): row for row in response.json()['foreshadows']}
        assert rows[known]['source_state'] == 'current' and rows[known]['source_evidence'] == '寄信的存根'
        assert rows[legacy]['source_state'] == 'unknown' and response.json()['debt'] is None
        page = await client.get(f'/api/projects/{r.pid}/foreshadows/plans?limit=2')
        assert page.status_code == 200, page.text
        assert page.json()['total'] == 3 and page.json()['has_more']
        rest = await client.get(f'/api/projects/{r.pid}/foreshadows/plans?limit=2&offset=2')
        assert not rest.json()['has_more'] and len(rest.json()['plans']) == 1
        foreign = await client.get(f'/api/projects/{r.foreign}/foreshadows/plans')
        assert foreign.json()['plans'] == []
    assert len(r.session.execute(select(Foreshadow.id)).all()) == 2


@pytest.mark.asyncio
async def test_generation_hook_uses_source_checked_reader(book):
    from app.services.foreshadow_manager import ForeshadowManager
    r = book
    add_clue(r, r.chapters[0], '有来源的登记')
    add_clue(r, r.chapters[3], '后文才出现')
    text = await ForeshadowManager(r.db).get_prompts_for_generation(r.pid, 51)
    assert '有来源的登记' in text and '后文才出现' not in text


@pytest.mark.asyncio
async def test_related_chapter_recall_does_not_reintroduce_legacy_or_future_foreshadow_numbers(book):
    from app.services.related_chapters import find_related_chapters
    r = book
    add_clue(r, r.chapters[0], '存根原件')
    add_clue(r, r.chapters[0], '旧数字', legacy=True)
    add_clue(r, r.chapters[3], '将来凭据')
    related = await find_related_chapters(r.db, r.pid, r.volumes[1], 51,
        {'summary': '存根原件、旧数字和将来凭据'}, min_total_chapters=0, min_signals=1, historical_roster=[])
    assert [row['chapter'] for row in related] == [11]
    assert '登记原文' in related[0]['reasons'][0]


@pytest.mark.asyncio
async def test_edit_impact_uses_source_identity_not_local_number_or_chinese_whitespace(book):
    from app.services.incremental_sync import IncrementalSyncService
    r = book
    fid = add_clue(r, r.chapters[1], '寄信人的存根')
    add_clue(r, r.chapters[0], '更早线索')
    add_clue(r, r.chapters[1], '旧编号', legacy=True)
    old = '陈伯把寄信的存根放在桌上。'
    new = '陈伯没有留下存根，转身出了门。'
    r.session.execute(update(Chapter).where(Chapter.id == r.chapters[1]).values(content_text=new))
    r.session.commit()
    async def get(model, key): return r.session.get(model, key)
    r.db.get = get
    impacts = await IncrementalSyncService(r.db)._check_foreshadow_impact(
        r.session.get(Chapter, r.chapters[1]), old, new)
    assert [row['foreshadow_id'] for row in impacts] == [str(fid)]
    assert impacts[0]['source_state'] == 'changed'
    assert 'old_overlap' not in impacts[0] and '尚未判断剧情含义' in impacts[0]['reason']
    assert r.session.execute(select(Foreshadow.status).where(Foreshadow.id == fid)).scalar_one() == 'planted'


def test_volume_prompt_keeps_all_plans_and_does_not_promote_them_to_prose():
    from app.services.context_pack import ContextPack
    entries = [{'description': f'第{i}条安排', 'resolve_conditions': ['条件一', '条件二', '第三个条件很重要']}
               for i in range(12)]
    text = ContextPack(volume_outline={'foreshadows': {'planted': entries, 'resolved': ['回答来信的问题']}})._render_volume_outline_block()
    assert '第11条安排' in text and '第三个条件很重要' in text
    assert '尚待正文落实' in text and '不代表已经兑现' in text and '已埋伏笔' not in text
