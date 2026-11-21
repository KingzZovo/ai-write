"""Cognition must reach actual arc attempts, with complete original quotes."""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.cognition import ChapterCognition as Record, CognitionRevision as Revision
from app.models.project import Chapter
from app.services.entity_sources import digest, resolve_entity_source
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit
from tests.support.body_hashes import install_sqlite_body_hashes
from tests.support.planning_cognition import seed_cognition


@pytest.fixture
def cognition_book(planning_book):
    r = planning_book
    for model in (Record, Revision): model.__table__.create(r.engine, checkfirst=True)
    install_sqlite_body_hashes(r.engine)
    return r


async def cognition_material(r, *, ready=True):
    lines = ['林禾还不知道信有没有送到。', '陈川以为船主已经看过信。', '林禾怀疑船主另有打算。',
             '信其实仍在陈川的内袋，林禾没有看见。', '陈川读完回条，才知道寄信费用尚未付清。']
    changes = [dict(character='林禾', still_unknown='信有没有送到', evidence=lines[0]),
        dict(character='陈川', believes='船主已经看过信', evidence=lines[1]),
        dict(character='林禾', suspects='船主另有打算', evidence=lines[2]),
        dict(character='__reader__', learns='信仍在陈川内袋', evidence=lines[3]),
        dict(character='陈川', learns='寄信费用尚未付清', evidence=lines[4])]
    with r.engine.begin() as db:
        db.execute(update(Chapter).where(Chapter.id == r.cid).values(content_text='\n'.join(lines), status='completed'))
    return await seed_cognition(r, r.cid, changes) if ready else None


@pytest.mark.asyncio
async def test_all_actual_arc_stages_receive_dated_cognition_and_complete_evidence(cognition_book):
    from app.services.character_cognition import load_ledger
    r = cognition_book; data = await cognition_material(r)
    async with r.sessions() as db:
        ledger = await load_ledger(db, r.pid, before_global_idx=2)
    assert ledger['陈川']['history'][-1]['evidence'] == data.changes[-1]['evidence']
    body = await request_body(r, target=11)
    assert (await submit(r, body)).status_code == 202
    await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed' and len(r.calls) == 4
    for _, user, _ in r.calls:
        assert all(change['evidence'] in user for change in data.changes)
        assert '当时未知记录' in user and '当时确信（可能误信）' in user and '当时怀疑' in user
    assert state['source_json']['version'] == 5
    assert state['source_json']['cognition_context']['snapshot_hash']
    assert state['journal_json']['version'] == 2


@pytest.mark.asyncio
async def test_a_completed_chapter_without_cognition_cannot_be_an_empty_planning_ledger(cognition_book):
    r = cognition_book; await cognition_material(r, ready=False)
    async with r.client() as client:
        response = await client.get(path(r, '/source'))
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'arc_planning_cognition_material_unavailable'
    assert not r.calls


async def author_decision(r, data, changes):
    from app.services.cognition_edits import save_cognition_decision
    from app.services.cognition_reader import record_version
    async with r.sessions() as db:
        record = await db.get(Record, data.record_id)
        result = await save_cognition_decision(db, r.pid, r.cid, uuid4(), data.source.source_hash,
            record_version(record, data.source), mode='author', changes=changes, actor='离线作者校订')
        await db.commit()
    return result


@pytest.mark.asyncio
async def test_explicit_empty_decisions_keep_full_coverage_and_the_original_body(cognition_book):
    r = cognition_book; await cognition_material(r, ready=False)
    data = await seed_cognition(r, r.cid, [])
    body = await request_body(r, target=4); await submit(r, body); await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed'
    source = state['source_json']['cognition_context']
    assert source['ledger'] == {} and source['snapshot']['positions'] == {str(r.cid): 1}
    assert source['snapshot']['sources'] == {str(r.cid): data.source.source_hash}
    assert len(source['snapshot']['decisions']) == len(source['snapshot']['proofs']) == 1
    with Session(r.engine) as db: assert db.get(Chapter, r.cid).content_text == data.source.content


@pytest.mark.asyncio
@pytest.mark.parametrize('empty', [False, True])
async def test_current_author_correction_enters_the_plan_without_overwriting_model_records(cognition_book, empty):
    r = cognition_book; data = await cognition_material(r)
    corrected = [] if empty else [dict(character='陈川', believes='当时只是以为船主看过信，尚未核实', evidence=data.changes[1]['evidence'])]
    await author_decision(r, data, corrected)
    body = await request_body(r, target=4); await submit(r, body); await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed'
    ledger = state['source_json']['cognition_context']['ledger']
    assert (ledger == {}) == empty
    if not empty:
        assert ledger['陈川']['history'][0]['source_mode'] == 'author'
        assert all('当时只是以为船主看过信，尚未核实' in user and '（作者校订）' in user for _, user, _ in r.calls)
    with Session(r.engine) as db: assert db.get(Record, data.record_id).changes_json == data.changes


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['author', 'auto', 'empty_author'])
async def test_changed_decision_after_preview_refuses_the_stale_request_before_any_call(cognition_book, change):
    r = cognition_book; data = await cognition_material(r)
    body = await request_body(r, target=4)
    if change == 'auto':
        with r.engine.begin() as db:
            db.execute(update(Record).where(Record.id == data.record_id).values(changes_json=[], payload_hash=digest([data.source.source_hash, []])))
    else:
        await author_decision(r, data, [] if change == 'empty_author' else data.changes)
    response = await submit(r, body)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'arc_planning_source_changed'
    assert snapshot(r, body['request_id']) is None and not r.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt', ['missing', 'pending', 'failed', 'payload', 'quote', 'author_pointer', 'blank_body'])
async def test_unready_or_damaged_cognition_never_becomes_an_available_empty_context(cognition_book, corrupt):
    r = cognition_book; data = await cognition_material(r)
    with r.engine.begin() as db:
        if corrupt == 'missing': db.execute(delete(Record))
        elif corrupt in {'pending', 'failed'}: db.execute(update(Record).values(status=corrupt))
        elif corrupt == 'payload': db.execute(update(Record).values(payload_hash='f' * 64))
        elif corrupt == 'author_pointer': db.execute(update(Record).values(source_mode='author', author_revision_id=uuid4()))
        elif corrupt == 'blank_body': db.execute(update(Chapter).values(content_text=' \n'))
        else:
            changes = [dict(data.changes[0], evidence='这段引文从未出现在保存稿里。')]
            db.execute(update(Record).values(changes_json=changes, payload_hash=digest([data.source.source_hash, changes])))
    async with r.client() as client: response = await client.get(path(r, '/source'))
    assert response.status_code == 503 and response.json()['detail']['code'] == 'arc_planning_cognition_material_unavailable'
    assert not r.calls


@pytest.mark.asyncio
async def test_originals_and_each_actual_input_are_reviewable_without_bloating_progress(cognition_book):
    r = cognition_book; data = await cognition_material(r)
    body = await request_body(r, target=4); await submit(r, body); await run(r, body['request_id'])
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
        progress = (await client.get(path(r, f'/{body["request_id"]}'))).json()
        page = (await client.get(path(r))).json()
    assert detail['integrity_valid'] and all(change['evidence'] in detail['cognition_material_text'] for change in data.changes)
    inputs = [attempt['input'] for stage in detail['journal']['stages'] for attempt in stage['attempts']]
    assert inputs == [user for _, user, _ in r.calls]
    assert all('cognition_material_text' not in row and 'journal' not in row and 'source' not in row
               for row in [progress, *page['items']])


@pytest.mark.asyncio
async def test_reading_cognition_does_not_flush_or_drop_an_unsaved_manuscript(cognition_book):
    from app.services.arc_planning_sources import read_planning_source
    from unittest.mock import Mock
    r = cognition_book; data = await cognition_material(r)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.cid)
        chapter.content_text = '作者尚未保存的当前修改。'
        chapter.summary = '作者尚未保存的摘要。'
        db.session.flush = Mock(side_effect=AssertionError('read must not flush'))
        source = await read_planning_source(db, r.pid)
        assert source['cognition_context']['snapshot']['sources'][str(r.cid)] == data.source.source_hash
        assert chapter.content_text == '作者尚未保存的当前修改。' and chapter.summary == '作者尚未保存的摘要。'
        db.session.flush.assert_not_called()
