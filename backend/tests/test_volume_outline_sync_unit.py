from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.services.volume_outline_sync import sync_untouched_stubs
from app.services.outline_order import REF, stub_payload


def setup():
    volume = SimpleNamespace(id=uuid4(), volume_idx=1)
    old = {'volume_id': str(volume.id), 'volume_idx': 1, 'chapter_summaries': [
        {'chapter_idx': i, 'summary': f'原摘要{i}'} for i in range(1, 4)]}
    new = deepcopy(old)
    for item in new['chapter_summaries']: item['summary'] = f"新摘要{item['chapter_idx']}"
    chapters = [SimpleNamespace(id=uuid4(), volume_id=volume.id, chapter_idx=i, outline_json=deepcopy(item), content_text='',
        status='draft', title=f'作者标题{i}', summary='事实摘要', target_word_count=1800)
        for i, item in enumerate(old['chapter_summaries'], 1)]
    db = SimpleNamespace(scalars=AsyncMock(side_effect=[SimpleNamespace(all=lambda: [volume]), SimpleNamespace(all=lambda: chapters)]))
    return old, new, chapters, db


@pytest.mark.asyncio
async def test_raw_text_only_save_never_replaces_expanded_chapter_plans():
    old, new, chapters, db = setup()
    new = {**old, 'raw_text': '作者只修改了卷纲文字'}
    chapters[0].outline_json = {'scenes': ['作者详细安排']}
    result = await sync_untouched_stubs(db, uuid4(), old, new)
    assert result.synced == result.preserved == 0
    assert chapters[0].outline_json == {'scenes': ['作者详细安排']}
    db.scalars.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('protect', ['edited', 'prose', 'running', 'removed', 'no_provenance'])
async def test_only_untouched_stubs_sync_and_explicit_indices_survive_reordering(protect):
    old, new, chapters, db = setup()
    if protect == 'edited': chapters[0].outline_json = {'scenes': ['完整场景']}
    if protect == 'prose': chapters[0].content_text = '作者已写正文。'
    if protect == 'running': chapters[0].status = 'generating'
    if protect == 'removed': new['chapter_summaries'].pop(0)
    if protect == 'no_provenance': old['chapter_summaries'].pop(0)
    first = deepcopy(chapters[0].__dict__)
    new['chapter_summaries'].reverse()
    result = await sync_untouched_stubs(db, uuid4(), old, new)
    assert result.synced == 2 and result.preserved == 1
    assert chapters[0].__dict__ == first
    assert stub_payload(chapters[1].outline_json) == {'chapter_idx': 2, 'summary': '新摘要2'}
    assert stub_payload(chapters[2].outline_json) == {'chapter_idx': 3, 'summary': '新摘要3'}
    assert chapters[1].outline_json[REF] == str(chapters[1].id)
    assert chapters[2].outline_json[REF] == str(chapters[2].id)
    for ch in chapters:
        assert ch.title.startswith('作者标题') and ch.summary == '事实摘要' and ch.target_word_count == 1800
    for call in db.scalars.call_args_list:
        assert call.args[0]._for_update_arg is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['duplicate', 'mixed', 'invalid', 'move_volume', 'move_index', 'duplicate_chapter'])
async def test_invalid_mapping_fails_before_mutation(bad):
    old, new, chapters, db = setup()
    before = deepcopy([ch.__dict__ for ch in chapters])
    if bad == 'duplicate': new['chapter_summaries'][1]['chapter_idx'] = 1
    if bad == 'mixed': del new['chapter_summaries'][1]['chapter_idx']
    if bad == 'invalid': new['chapter_summaries'][1]['chapter_idx'] = True
    if bad == 'move_volume': new['volume_id'] = str(uuid4())
    if bad == 'move_index': new['volume_idx'] = 2
    if bad == 'duplicate_chapter':
        chapters[1].chapter_idx = 1
        before = deepcopy([ch.__dict__ for ch in chapters])
    with pytest.raises(ValueError): await sync_untouched_stubs(db, uuid4(), old, new)
    assert [ch.__dict__ for ch in chapters] == before


@pytest.mark.asyncio
async def test_missing_chapters_are_reported_without_creating_or_deleting_rows():
    old, new, chapters, db = setup()
    chapters.pop()
    result = await sync_untouched_stubs(db, uuid4(), old, new)
    assert result.synced == 2 and result.unmaterialized == 1


@pytest.mark.asyncio
async def test_changed_unnumbered_legacy_summaries_cannot_guess_identity_from_list_order():
    old, new, chapters, db = setup()
    for data in (old, new):
        for item in data['chapter_summaries']: del item['chapter_idx']
    for ch in chapters: del ch.outline_json['chapter_idx']
    original = deepcopy([ch.outline_json for ch in chapters])
    with pytest.raises(ValueError, match='明确章号'):
        await sync_untouched_stubs(db, uuid4(), old, new)
    assert [ch.outline_json for ch in chapters] == original


@pytest.mark.asyncio
async def test_unavailable_volume_owner_cannot_fall_back_to_a_reused_number():
    old, new, chapters, db = setup()
    old['volume_id'] = new['volume_id'] = ''
    original = deepcopy([chapter.__dict__ for chapter in chapters])
    with pytest.raises(ValueError, match='不能按旧卷号'):
        await sync_untouched_stubs(db, uuid4(), old, new)
    db.scalars.assert_not_awaited()
    assert [chapter.__dict__ for chapter in chapters] == original
