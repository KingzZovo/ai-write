"""Source completeness and bounded reads on author books over five million characters."""
import asyncio
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.decompile import BeatSheetCard, ReferenceBookSlice
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Outline, Project, ReferenceBook, StyleProfile, Volume
from app.services.generation_task_sources import source_digest
from app.services.outline_from_reference import _load_beat_sketch
from tests.test_generation_task_execution import ORIGINAL, snapshot, task_book
from tests.test_generation_task_recovery import abandon, interrupted_task, recover


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['explicit_style', 'settings_style', 'structure_book', 'reference_book', 'missing_reference'])
async def test_selected_style_and_reference_source_changes_are_observed(interrupted_task, source):
    r = interrupted_task
    book_id, style_id = uuid4(), uuid4()
    for model in (ReferenceBookSlice, BeatSheetCard): model.__table__.create(r.engine)
    with r.engine.begin() as db:
        if source != 'missing_reference':
            db.execute(sa.insert(ReferenceBook).values(id=book_id, title='参考书', source='upload_txt', metadata_json={'plot_structure': {'arc': '寻亲'}}))
        db.execute(sa.insert(StyleProfile).values(id=style_id, name='朴素叙述', config_json={'tone': '克制'}))
        if source == 'settings_style':
            db.execute(sa.update(Project).where(Project.id == r.pid).values(settings_json={'style_reference': {'profile_id': str(style_id)}}))
    params = {'explicit_style': {'style_id': str(style_id)}, 'settings_style': {},
        'structure_book': {'structure_book_id': str(book_id)},
        'reference_book': {'reference_book_id': str(book_id)},
        'missing_reference': {'reference_book_id': str(book_id)}}[source]
    task_type = 'outline_from_reference' if 'reference' in source else 'chapter'
    identity = await abandon(r, task_type, **params)
    with r.engine.begin() as db:
        if source in {'explicit_style', 'settings_style'}:
            db.execute(sa.update(StyleProfile).where(StyleProfile.id == style_id).values(config_json={'tone': '幽默'}))
        elif source == 'missing_reference':
            db.execute(sa.insert(ReferenceBook).values(id=book_id, title='新出现的参考书', source='upload_txt'))
        else:
            db.execute(sa.update(ReferenceBook).where(ReferenceBook.id == book_id).values(metadata_json={'plot_structure': {'arc': '还债'}}))
    await recover(identity)
    assert snapshot(r, identity)['recovery_history_json'][-1]['outcome'] == 'source_changed'
    assert len(r.interrupted) == 1


@pytest.mark.asyncio
async def test_source_scan_covers_volumes_beyond_its_first_two_pages(interrupted_task):
    r = interrupted_task
    last_id = UUID(int=(1 << 128) - 1)
    with r.engine.begin() as db:
        for index in range(129):
            db.execute(sa.insert(Volume).values(id=last_id if index == 128 else uuid4(), project_id=r.pid,
                volume_idx=index + 2, title=f'第{index + 2}卷', summary='原卷简介。'))
    statements = []
    def capture(_c, _u, sql, params, _ctx, _m): statements.append((sql, params))
    sa.event.listen(r.engine, 'before_cursor_execute', capture)
    try:
        identity = await abandon(r, 'outline_book')
        with r.engine.begin() as db:
            db.execute(sa.update(Volume).where(Volume.id == last_id).values(summary='末卷改为兄弟和解。'))
        await recover(identity)
    finally:
        sa.event.remove(r.engine, 'before_cursor_execute', capture)
    pages = [(sql, params) for sql, params in statements if sql.startswith('SELECT volumes.') and 'ORDER BY volumes.id' in sql]
    assert len(pages) == 6 and all('LIMIT' in sql and 64 in params for sql, params in pages)
    assert snapshot(r, identity)['recovery_history_json'][-1]['outcome'] == 'source_changed'
    assert len(r.interrupted) == 1


@pytest.mark.asyncio
async def test_target_volume_outline_and_competing_book_outline_both_protect_recovery(interrupted_task):
    r = interrupted_task
    volume_outline = uuid4()
    with r.engine.begin() as db:
        db.execute(sa.insert(Outline).values(id=volume_outline, project_id=r.pid, level='volume',
            content_json={'volume_id': str(r.vid), 'volume_idx': 1, 'raw_text': '本卷先还钱，后追查。'}))
    identity = await abandon(r)
    with r.engine.begin() as db:
        db.execute(sa.update(Outline).where(Outline.id == volume_outline).values(version=2,
            content_json={'volume_id': str(r.vid), 'volume_idx': 1, 'raw_text': '本卷先追查收据，不交钱。'}))
    await recover(identity)
    assert snapshot(r, identity)['recovery_history_json'][-1]['outcome'] == 'source_changed'


@pytest.mark.asyncio
async def test_recovery_of_a_five_million_character_book_reads_only_its_target_body(interrupted_task):
    r = interrupted_task
    prose = '甲' * 5200
    with r.engine.begin() as db:
        db.execute(sa.insert(Chapter), [{'id': uuid4(), 'volume_id': r.vid, 'chapter_idx': index + 2,
            'global_idx': index + 2, 'title': f'旧章{index}', 'content_text': prose,
            'word_count': len(prose), 'status': 'completed'} for index in range(1004)])
    assert len(prose) * 1004 + len(ORIGINAL) > 5_000_000
    reads = []
    def capture(_c, _u, sql, params, _ctx, _m):
        if sql.startswith('SELECT') and 'chapters.content_text' in sql: reads.append((sql, params))
    sa.event.listen(r.engine, 'before_cursor_execute', capture)
    try:
        # submit() reads the author target once; claim and recovery each
        # reread that same target and never load any of the 1004 old bodies.
        identity = await abandon(r)
        with pytest.raises(asyncio.CancelledError): await recover(identity)
    finally:
        sa.event.remove(r.engine, 'before_cursor_execute', capture)
    assert len(reads) == 3
    assert all('chapters.id = ?' in sql and r.cid.hex in params for sql, params in reads)
    state = snapshot(r, identity)
    assert state['attempt_count'] == 2 and len(str(state['execution_source_json'])) < 180


@pytest.mark.asyncio
@pytest.mark.parametrize('changed_beat', [{'summary': '兄弟决定分家'}, ['damaged', 'beat']])
async def test_reference_sketch_never_loads_raw_passages_and_last_beat_changes_are_detected(interrupted_task, changed_beat):
    r = interrupted_task
    for model in (ReferenceBookSlice, BeatSheetCard): model.__table__.create(r.engine)
    book_id, card_id = uuid4(), uuid4()
    with r.engine.begin() as db:
        db.execute(sa.insert(ReferenceBook).values(id=book_id, title='长篇参考', source='upload_txt', total_words=5_226_000))
        slices, cards = [], []
        for index in range(1005):
            slice_id = uuid4()
            slices.append({'id': slice_id, 'book_id': book_id, 'chapter_idx': index + 1,
                'sequence_id': index, 'raw_text': '乙' * 5200})
            cards.append({'id': card_id if index == 1004 else uuid4(), 'book_id': book_id, 'slice_id': slice_id,
                'beat_json': {'scene_type': 'home', 'summary': f'第{index + 1}章先见家人', 'outcome': '核对债务'}})
        db.execute(sa.insert(ReferenceBookSlice), slices)
        db.execute(sa.insert(BeatSheetCard), cards)
    statements = []
    def capture(_c, _u, sql, _p, _ctx, _m): statements.append(sql)
    sa.event.listen(r.engine, 'before_cursor_execute', capture)
    try:
        async with r.sessions() as db: sketch = await _load_beat_sketch(str(book_id), db)
        identity = await abandon(r, 'outline_from_reference', reference_book_id=str(book_id))
        with r.engine.begin() as db:
            db.execute(sa.update(BeatSheetCard).where(BeatSheetCard.id == card_id).values(beat_json=changed_beat))
        await recover(identity)
    finally:
        sa.event.remove(r.engine, 'before_cursor_execute', capture)
    assert len(sketch.splitlines()) == 400
    assert sketch.startswith('[ch1/0]') and '[ch1005/1004]' in sketch
    assert all('reference_book_slices.raw_text' not in sql and 'text_chunks.content' not in sql for sql in statements)
    assert snapshot(r, identity)['recovery_history_json'][-1]['outcome'] == 'source_changed'


def test_source_hash_is_stable_but_rejects_non_json_numeric_values():
    assert source_digest({'b': ['结局'], 'a': 1}) == source_digest({'a': 1, 'b': ['结局']})
    with pytest.raises(ValueError): source_digest({'bad': float('nan')})
