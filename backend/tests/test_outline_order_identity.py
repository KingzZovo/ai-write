"""Real SQLite statements and API moves; no model or external services."""
from copy import deepcopy
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update

from app.models.project import Chapter, Outline, Volume
from app.services.chapter_outline_expander import _gather_context, _format_user_prompt
from app.services.chapter_outline_save import snapshot_chapter_outline, save_chapter_outline
from app.services.foreshadow_plans import read_foreshadow_plans
from app.services.outline_authority import TEXT_REVISION, outline_for_generation, record_text_edit
from app.services.outline_order import REF, UNAVAILABLE, chapter_stub, maintain_outline_order, stub_payload
from app.services.outline_readiness import build_outline_readiness_report, has_meaningful_outline_content
from app.services.summary_provenance import summary_source_hash
from app.services.volume_outline_sync import sync_untouched_stubs
from tests.test_chapter_order_maintenance import book, move, saved


def add_plan(r, *, legacy_volume=False, text_revision=False):
    summaries = []
    for cid, idx, title, text in zip(r.ids[:3], [4, 9, 20], ['还信', '过桥', '回家'],
                                    ['周原把信递还给宋岚。', '宋岚过桥去见父亲。', '周原回家取雨伞。']):
        stub = {'chapter_idx': idx, 'title': title, 'summary': text, 'key_events': [text]}
        if idx == 4:
            stub['foreshadows_planted'] = [{'description': '信封上少了一枚邮票', 'planted_chapter': idx}]
        summaries.append(stub)
        r.session.execute(update(Chapter).where(Chapter.id == cid).values(outline_json=deepcopy(stub)))
    content = {'volume_idx': 2, 'title': '归途', 'core_conflict': '按约把信送回',
        'raw_text': '作者原文：第四章还信，第九章过桥。', 'chapter_summaries': summaries,
        'foreshadows': {'planted': [{'description': '信纸上的水印', 'planted_chapter': 4}]}}
    if not legacy_volume:
        content['volume_id'] = str(r.volumes[0])
    if text_revision:
        content = record_text_edit(content, {**content, 'raw_text': '作者新安排：本卷不再出现信件。'})
        chapter = r.session.get(Chapter, r.ids[0])
        chapter.outline_json = record_text_edit(chapter.outline_json, {**chapter.outline_json,
            'raw_text': '作者新细纲：宋岚独自过桥，本章没有信件。'})
    oid = uuid4()
    r.session.execute(insert(Outline).values(id=oid, project_id=r.pid, parent_id=r.book_id,
        level='volume', is_confirmed=1, content_json=content))
    r.session.commit()
    return oid, deepcopy(content)


def stored_plan(r, oid):
    return r.session.execute(select(Outline.content_json).where(Outline.id == oid)).scalar_one()


@pytest.mark.asyncio
async def test_swap_preserves_story_identity_nested_plans_and_author_strings(book):
    r = book; oid, original = add_plan(r)
    before = saved(r)
    async with r.client() as client:
        url, body = move(r)
        response = await client.post(url, json=body)
        assert response.status_code == 200, response.text
    content = stored_plan(r, oid)
    assert content['raw_text'] == original['raw_text']
    assert [item[REF] for item in content['chapter_summaries']] == [str(r.ids[1]), str(r.ids[0]), str(r.ids[2])]
    chapter = r.session.get(Chapter, r.ids[0]); r.session.refresh(chapter)
    assert chapter.chapter_idx == chapter.outline_json['chapter_idx'] == 9
    stub = chapter_stub(content, chapter)
    assert stub['title'] == '还信' and stub['chapter_idx'] == 9
    assert stub['foreshadows_planted'][0]['planted_chapter'] == 9
    assert content['foreshadows']['planted'][0]['planted_chapter'] == 9
    plans = (await read_foreshadow_plans(r.db, r.pid))['plans']
    assert len(plans) == 2
    assert all(p['chapter_id'] == str(r.ids[0]) and p['chapter_idx'] == 9 and p['planted_chapter'] == 2 for p in plans)
    prompt = str(outline_for_generation(content))
    assert REF not in prompt and UNAVAILABLE not in prompt
    for cid in r.ids:
        after = saved(r)[str(cid)]
        for key in ('content_text', 'title', 'summary', 'status'):
            assert after[key] == before[str(cid)][key]
    assert await maintain_outline_order(r.db, r.pid) == 0


@pytest.mark.asyncio
async def test_old_stubs_can_be_identified_after_an_earlier_numeric_only_move(book):
    r = book; oid, _ = add_plan(r)
    # Reproduce the previous implementation's incomplete structural update.
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[0]).values(chapter_idx=9, global_idx=2))
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[1]).values(chapter_idx=4, global_idx=1))
    r.session.commit()
    await maintain_outline_order(r.db, r.pid)
    await r.db.commit()
    stubs = stored_plan(r, oid)['chapter_summaries']
    assert [(s['title'], s['chapter_idx']) for s in stubs[:2]] == [('过桥', 4), ('还信', 9)]


@pytest.mark.asyncio
async def test_missing_provenance_is_retained_without_borrowing_a_number(book):
    r = book; oid, _ = add_plan(r)
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[0]).values(outline_json={'summary': '作者重新安排为买票。'}))
    r.session.commit()
    async with r.client() as client:
        url, body = move(r)
        assert (await client.post(url, json=body)).status_code == 200
    content = stored_plan(r, oid)
    unknown = next(item for item in content['chapter_summaries'] if item['title'] == '还信')
    assert unknown[REF] is None and unknown[UNAVAILABLE]
    assert unknown['summary'] == '周原把信递还给宋岚。'
    assert '周原把信递还给宋岚。' not in str(outline_for_generation(content).get('chapter_summaries'))
    assert (await read_foreshadow_plans(r.db, r.pid))['unavailable_sources'] == 1
    assert chapter_stub(content, r.session.get(Chapter, r.ids[1]))['title'] == '过桥'


@pytest.mark.asyncio
async def test_deleted_chapter_reference_never_retargets_reused_number(book):
    r = book; oid, _ = add_plan(r)
    await maintain_outline_order(r.db, r.pid); await r.db.commit()
    async with r.client() as client:
        assert (await client.delete(f'/api/projects/{r.pid}/chapters/{r.ids[0]}')).status_code == 204
        created = await client.post(f'/api/projects/{r.pid}/chapters', json={
            'volume_id': str(r.volumes[0]), 'chapter_idx': 4, 'title': '新章节'})
        assert created.status_code == 201, created.text
    content = stored_plan(r, oid)
    item = next(s for s in content['chapter_summaries'] if s['title'] == '还信')
    assert item[REF] == str(r.ids[0]) and item[UNAVAILABLE]
    assert (await read_foreshadow_plans(r.db, r.pid))['plans'] == []
    assert chapter_stub(content, NS(id=created.json()['id'], chapter_idx=4)) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_volume_move_keeps_readiness_and_stable_owner(book, legacy):
    r = book; oid, original = add_plan(r, legacy_volume=legacy)
    async with r.client() as client:
        result = await client.put(f'/api/projects/{r.pid}/volumes/{r.volumes[0]}', json={'volume_idx': 7})
        assert result.status_code == 200, result.text
    content = stored_plan(r, oid)
    assert content['volume_id'] == str(r.volumes[0]) and content['volume_idx'] == 7
    assert content['raw_text'] == original['raw_text']
    report = await build_outline_readiness_report(r.db, project_id=r.pid, chapter_id=r.ids[0])
    assert report.layers['volume'].outline_id == str(oid) and report.ready
    assert all(p['volume_idx'] == 7 for p in (await read_foreshadow_plans(r.db, r.pid))['plans'])


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_both_order_and_outline_bindings(book):
    r = book; oid, original = add_plan(r)
    before = saved(r); r.fail_commit = True
    async with r.client() as client:
        url, body = move(r)
        response = await client.post(url, json=body)
        assert response.status_code == 503
    assert stored_plan(r, oid) == original and saved(r) == before


@pytest.mark.asyncio
async def test_author_text_revision_suppresses_old_plans_before_and_after_move(book):
    r = book; oid, original = add_plan(r, text_revision=True)
    assert (await read_foreshadow_plans(r.db, r.pid))['plans'] == []
    async with r.client() as client:
        url, body = move(r)
        assert (await client.post(url, json=body)).status_code == 200
    content = stored_plan(r, oid)
    assert content[TEXT_REVISION] == original[TEXT_REVISION] and content['raw_text'] == original['raw_text']
    assert (await read_foreshadow_plans(r.db, r.pid))['plans'] == []
    assert '信纸上的水印' not in str(outline_for_generation(content))


@pytest.mark.asyncio
async def test_stub_sync_after_move_updates_only_exact_unwritten_originals(book):
    r = book; oid, _ = add_plan(r)
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[0]).values(content_text=''))
    r.session.commit()
    async with r.client() as client:
        url, body = move(r)
        assert (await client.post(url, json=body)).status_code == 200
    old = deepcopy(stored_plan(r, oid)); new = deepcopy(old)
    for stub in new['chapter_summaries']:
        stub['summary'] += '作者补充一场雨。'
    result = await sync_untouched_stubs(r.db, r.pid, old, new)
    await r.db.flush()
    assert result.synced == 1 and result.preserved == 2
    for i, cid in enumerate(r.ids[:3]):
        row = r.session.get(Chapter, cid); r.session.refresh(row)
        assert ('作者补充一场雨。' in row.outline_json['summary']) == (i == 0)
    assert new['chapter_summaries'][1][REF] == str(r.ids[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('cross_volume', [False, True])
async def test_chapter_snapshot_uses_actual_predecessor_and_rechecks_its_prose(book, cross_volume):
    r = book; oid, _ = add_plan(r)
    chapter_id = r.ids[0] if cross_volume else r.ids[1]
    predecessor = r.ids[3] if cross_volume else r.ids[0]
    if cross_volume:
        r.session.execute(update(Volume).where(Volume.id == r.volumes[1]).values(volume_idx=1))
        # The last chapter in the preceding volume has a sparse label too.
        predecessor = r.ids[4]
    previous = r.session.get(Chapter, predecessor); r.session.refresh(previous)
    text, summary = '宋岚刚把伞递给周原。', '伞已交给周原。'
    r.session.execute(update(Chapter).where(Chapter.id == predecessor).values(
        content_text=text, status='completed', summary=summary,
        summary_source_hash=summary_source_hash(previous.title, text, summary)))
    r.session.commit()
    current = r.session.get(Chapter, chapter_id)
    source = await snapshot_chapter_outline(r.db, r.pid, oid, current.chapter_idx)
    assert source.previous_summary == summary
    r.session.execute(update(Chapter).where(Chapter.id == predecessor).values(content_text='周原没有接伞。'))
    r.session.commit()
    with pytest.raises(ValueError, match='生成期间'):
        await save_chapter_outline(r.db, source, '{"summary":"周原撑伞回家。","key_events":["回家"]}')


@pytest.mark.asyncio
async def test_expander_does_not_borrow_an_array_slot_or_revive_revised_structure(book, monkeypatch):
    r = book; oid, original = add_plan(r)
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[0]).values(chapter_idx=1, outline_json={}))
    r.session.execute(update(Outline).where(Outline.id == oid).values(content_json={**original,
        'chapter_summaries': [{'chapter_idx': 9, 'summary': '不能借给第一章的过桥剧情'}]}))
    r.session.commit()
    chapter = r.session.get(Chapter, r.ids[0]); r.session.refresh(chapter)
    context = await _gather_context(r.db, chapter)
    assert context['stub']['summary'] == ''
    assert '不能借给第一章' not in str(context['stub'])
    revised = record_text_edit(original, {**original, 'raw_text': '作者新卷纲：改写为买票。'})
    r.session.execute(update(Outline).where(Outline.id == oid).values(content_json=revised))
    r.session.commit()
    context = await _gather_context(r.db, chapter)
    prompt = _format_user_prompt(context, chapter)
    assert '作者新卷纲：改写为买票。' in prompt and '周原把信递还给宋岚。' not in prompt


def test_identity_metadata_cannot_make_an_empty_plan_ready():
    assert not has_meaningful_outline_content({'chapter_id': str(uuid4()), 'chapter_idx': 2,
        'volume_id': str(uuid4()), REF: str(uuid4())})
    assert not has_meaningful_outline_content({'title': '失效计划', UNAVAILABLE: True})


def test_unedited_raw_model_response_cannot_reintroduce_unavailable_plans():
    source = {'volume_id': str(uuid4()), 'volume_idx': 1, 'core_conflict': '按约送伞',
        'chapter_summaries': [{'chapter_idx': 1, 'summary': '不能确定归属的旧安排', REF: None, UNAVAILABLE: True}]}
    source['raw_text'] = json.dumps(source, ensure_ascii=False)
    before = deepcopy(source)
    projection = outline_for_generation(source)
    assert '不能确定归属的旧安排' not in str(projection) and 'raw_text' not in projection
    assert source == before
    revised = record_text_edit(source, {**source, 'raw_text': '作者决定把这段文字作为新安排。'})
    assert outline_for_generation(revised)['raw_text'] == '作者决定把这段文字作为新安排。'
    assert outline_for_generation({'chapter_id': 'c', 'chapter_idx': 1, 'raw_text': '只有文字的作者计划'})['raw_text'] == '只有文字的作者计划'
    assert outline_for_generation({'chapter_id': 'c', 'chapter_idx': 1, 'raw_text': '空结构占位仍须保留这段文字',
        'key_events': [], 'foreshadows_planted': [], 'state_changes': {}})['raw_text'] == '空结构占位仍须保留这段文字'


def test_breakdown_ignores_missing_invalid_and_duplicate_numbers_instead_of_array_slots():
    from app.services.outline_generator import extract_chapter_breakdown
    items = [{'summary': '没有编号'}, {'chapter_idx': 9, 'summary': '第九章'},
        {'chapter_idx': True, 'summary': '无效'}, {'chapter_idx': 2, 'summary': '重复甲'},
        {'chapter_idx': 2, 'summary': '重复乙'}, {'chapter_idx': 4, 'summary': '已失效', REF: None, UNAVAILABLE: True}]
    assert extract_chapter_breakdown(items) == {9: {'chapter_idx': 9, 'summary': '第九章'}}


@pytest.mark.parametrize('invalid', [True, 1.0, '1'])
def test_legacy_stub_cannot_treat_boolean_float_or_string_as_a_chapter_number(invalid):
    target = NS(id='target', chapter_idx=1)
    assert chapter_stub({'chapter_summaries': [{'chapter_idx': invalid, 'summary': '不可借给第一章'}]}, target) == {}
    assert chapter_stub({'chapter_summaries': [{'chapter_idx': 1, 'summary': '真正的第一章'}]}, target)['summary'] == '真正的第一章'


@pytest.mark.asyncio
async def test_readiness_uses_stable_volume_identity_and_newer_empty_source_suppresses_old(book):
    r = book; oid, content = add_plan(r)
    r.session.execute(update(Outline).where(Outline.id == oid).values(content_json={**content, 'volume_idx': 99}))
    r.session.commit()
    report = await build_outline_readiness_report(r.db, project_id=r.pid, chapter_id=r.ids[0])
    assert report.layers['volume'].outline_id == str(oid)
    newer = uuid4()
    r.session.execute(insert(Outline).values(id=newer, project_id=r.pid, level='volume', version=10,
        is_confirmed=1, content_json={'volume_id': str(r.volumes[0]), 'volume_idx': 2}))
    r.session.commit()
    report = await build_outline_readiness_report(r.db, project_id=r.pid, chapter_id=r.ids[0])
    assert not report.layers['volume'].ready


@pytest.mark.asyncio
async def test_author_can_schedule_unknown_plan_explicitly_but_an_unrelated_edit_cannot(book):
    from app.services.outline_order import prepare_author_chapter, prepare_author_volume
    r = book; oid, _ = add_plan(r)
    await maintain_outline_order(r.db, r.pid); await r.db.commit()
    volume = r.session.get(Volume, r.volumes[0])
    chapters = list(r.session.scalars(select(Chapter).where(Chapter.volume_id == volume.id)))
    old = deepcopy(stored_plan(r, oid))
    old['foreshadows']['planted'][0].update({REF: None, UNAVAILABLE: True})
    new = {**deepcopy(old), 'title': '只修改标题'}
    assert prepare_author_volume(old, new, volume, chapters)['foreshadows']['planted'][0][REF] is None
    new['foreshadows']['planted'][0]['planted_chapter'] = 9
    linked = prepare_author_volume(old, new, volume, chapters)['foreshadows']['planted'][0]
    assert linked[REF] == str(r.ids[1]) and not linked.get(UNAVAILABLE)
    # The JSON editor hides internal fields; exact unmodified entries still
    # retain their unavailable identity when an unrelated paragraph is edited.
    chapter = chapters[0]
    old_chapter = {'chapter_idx': chapter.chapter_idx, 'summary': '原细纲', 'foreshadows_planted': [
        {'description': '旧线索', 'planted_chapter': chapter.chapter_idx, REF: None, UNAVAILABLE: True}]}
    new_chapter = {'summary': '作者补充其他过程', 'foreshadows_planted': [
        {'description': '旧线索', 'planted_chapter': chapter.chapter_idx}]}
    result = prepare_author_chapter(old_chapter, new_chapter, chapter)
    assert result['foreshadows_planted'][0][REF] is None
    new_chapter['foreshadows_planted'][0]['description'] = '作者重新指定的线索'
    assert prepare_author_chapter(old_chapter, new_chapter, chapter)['foreshadows_planted'][0][REF] == str(chapter.id)


@pytest.mark.asyncio
async def test_metadata_refresh_cannot_move_order_version_backwards(book):
    r = book; add_plan(r)
    future = datetime(2100, 1, 1, tzinfo=timezone.utc)
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[0]).values(updated_at=future))
    r.session.commit()
    async with r.client() as client:
        url, body = move(r)
        assert (await client.post(url, json=body)).status_code == 200
    assert saved(r)[str(r.ids[0])]['updated_at'].replace(tzinfo=timezone.utc) > future


@pytest.mark.asyncio
async def test_new_author_volume_plan_binds_explicit_target_and_preserves_existing_chapter(book):
    from app.api.outlines import create_outline, OutlineCreate
    r = book; before = saved(r)
    result = await create_outline(str(r.pid), OutlineCreate(level='volume', content_json={
        'volume_id': str(r.volumes[0]), 'volume_idx': 2,
        'foreshadows': {'planted': [{'description': '作者指定的线索', 'planted_chapter': 20,
            REF: str(r.outsider)}]},
    }), r.db)
    await r.db.commit()
    entry = result.content_json['foreshadows']['planted'][0]
    assert entry[REF] == str(r.ids[2]) and not entry.get(UNAVAILABLE)
    assert saved(r) == before


@pytest.mark.asyncio
async def test_generated_volume_assigns_its_own_ids_instead_of_trusting_model_metadata(book):
    from app.services.volume_creation import snapshot_creation, materialize_volume
    from tests.support.volume_meta import valid_volume_meta
    r = book
    parsed = {**valid_volume_meta(2), 'volume_idx': 1, 'chapter_summaries': [
        {'chapter_idx': i, 'title': f'新章{i}', 'summary': '按约送伞。', 'key_events': ['抵达约定地点', '交伞'],
         REF: str(r.outsider), 'chapter_id': str(r.outsider)} for i in (1, 2)],
        'foreshadows': {'planted': [{'description': '伞柄上的字', 'planted_chapter': 2, REF: str(r.outsider)}], 'resolved': []}}
    snapshot = await snapshot_creation(r.db, r.pid, r.book_id, 1, 3)
    outline = await materialize_volume(r.db, snapshot, parsed)
    await r.db.commit()
    content = stored_plan(r, outline.id)
    ids = [item[REF] for item in content['chapter_summaries']]
    assert str(r.outsider) not in str(content)
    assert content['foreshadows']['planted'][0][REF] == ids[1]
    assert all(r.session.execute(select(Chapter.id).where(Chapter.id == UUID(cid))).scalar_one() for cid in ids)


@pytest.mark.asyncio
async def test_regeneration_selects_the_stable_volume_even_with_stale_numeric_metadata(book):
    from app.services.volume_regeneration import snapshot_volume, replace_volume
    from tests.support.volume_meta import valid_volume_meta
    r = book; own_id, original = add_plan(r)
    r.session.execute(update(Outline).where(Outline.id == own_id).values(content_json={**original, 'volume_idx': 99}))
    other_id = uuid4()
    other = {'volume_id': str(r.volumes[1]), 'volume_idx': 2, 'title': '另一卷的旧编号'}
    r.session.execute(insert(Outline).values(id=other_id, project_id=r.pid, parent_id=r.book_id,
        level='volume', content_json=other))
    r.session.commit()
    source = await snapshot_volume(r.db, r.pid, r.volumes[0])
    parsed = {**valid_volume_meta(2), 'volume_idx': 2, 'chapter_summaries': [
        {'chapter_idx': i, 'title': f'新章{i}', 'summary': '按约送伞。', 'key_events': ['抵达约定地点', '交伞']} for i in (1, 2)]}
    await replace_volume(r.db, source, parsed); await r.db.commit()
    assert r.session.execute(select(Outline.id).where(Outline.id == own_id)).first() is None
    kept = stored_plan(r, other_id)
    assert kept['volume_id'] == other['volume_id'] and kept['title'] == other['title']
    assert kept['volume_idx'] == 5


@pytest.mark.asyncio
async def test_unavailable_old_label_does_not_block_editing_or_reassociating_after_a_move(book):
    r = book; oid, _ = add_plan(r)
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[0]).values(outline_json={'summary': '作者独立细纲'}))
    r.session.execute(update(Chapter).where(Chapter.id == r.ids[1]).values(content_text=''))
    r.session.commit()
    async with r.client() as client:
        url, body = move(r)
        assert (await client.post(url, json=body)).status_code == 200
    old = stored_plan(r, oid)
    # Both the retained unknown old entry and the current known entry mention
    # label 4. The author can still edit the known one without losing history.
    assert sum(item['chapter_idx'] == 4 for item in old['chapter_summaries']) == 2
    new = deepcopy(old)
    for item in new['chapter_summaries']:
        if item.get(REF) == str(r.ids[1]):
            item['summary'] = '宋岚撑伞过桥去见父亲。'
        for field in (REF, UNAVAILABLE):
            item.pop(field, None)  # Same text shown by the JSON editor.
    result = await sync_untouched_stubs(r.db, r.pid, old, new)
    await r.db.flush()
    assert result.synced == 1 and result.unmaterialized == 0
    assert any(item.get(UNAVAILABLE) and item['title'] == '还信' for item in new['chapter_summaries'])
    # An explicit current label then associates the remaining legacy entry,
    # while the chapter's independent author plan remains untouched.
    old = deepcopy(new)
    for item in new['chapter_summaries']:
        if item['title'] == '还信':
            item['chapter_idx'] = 9
    result = await sync_untouched_stubs(r.db, r.pid, old, new)
    assert result.preserved == 1 and result.unmaterialized == 0
    assert next(item for item in new['chapter_summaries'] if item['title'] == '还信')[REF] == str(r.ids[0])
    own = r.session.get(Chapter, r.ids[0]); r.session.refresh(own)
    assert own.outline_json['summary'] == '作者独立细纲'
