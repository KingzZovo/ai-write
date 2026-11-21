"""Volume-outline adoption with real SQLite transactions and ASGI requests.

Project advisory/materialization locks are explicit substitutes. No model,
broker, PostgreSQL or graph service is contacted by these tests.
"""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.api import outlines, volume_outlines, volumes
from app.db.session import get_db
from app.models.generation_task import GenerationTask
from app.models.outline_write import VolumeOutlineWrite
from app.models.project import Chapter, Outline, Project, Volume
from app.services import volume_outline_writes as writes
from app.services.outline_authority import outline_for_generation
from tests.test_version_application import book


@pytest.fixture
def outline_book(book):
    r = book
    for model in (Outline, GenerationTask, VolumeOutlineWrite): model.__table__.create(r.engine)
    chapter = r.session.get(Chapter, r.cid)
    r.volume_id = chapter.volume_id
    current = Outline(id=uuid4(), project_id=r.pid, level='volume', version=2, is_confirmed=1,
        content_json={'volume_id': str(r.volume_id), 'volume_idx': 1, 'raw_text': '原卷纲：先交钱，再带弟弟看腿。',
                      'chapter_summaries': [{'chapter_idx': 1, 'summary': '交两千元。'}]})
    alternative = Outline(id=uuid4(), project_id=r.pid, level='volume', version=4, is_confirmed=0,
        content_json={'volume_id': str(r.volume_id), 'volume_idx': 1, 'raw_text': '未采用的另一份卷纲。'})
    r.session.add_all([current, alternative]); r.session.commit()
    r.outline_id, r.alternative_id = current.id, alternative.id
    r.base = f'/api/projects/{r.pid}/volume-outlines'
    async def database():
        try:
            yield r.db
            await r.db.commit()
        except BaseException:
            await r.db.rollback()
            raise
    app = FastAPI()
    app.include_router(volume_outlines.router)
    app.include_router(outlines.router)
    app.include_router(volumes.router)
    async def scalars(statement): return r.session.scalars(statement)
    async def delete(row): r.session.delete(row)
    r.db.scalars, r.db.delete = scalars, delete
    app.dependency_overrides[get_db] = database
    r.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test')
    return r


async def source(r):
    async with r.client() as client:
        response = await client.get(f'{r.base}/{r.volume_id}')
    assert response.status_code == 200, response.text
    return response.json()


async def request(r, **changes):
    return {'request_id': str(uuid4()), 'expected_source': await source(r),
            'text': '新卷纲：当面交足两千元，收好五百元，天亮后再去求医。', **changes}


async def save(r, body):
    async with r.client() as client:
        return await client.put(f'{r.base}/{r.volume_id}', json=body)


def history(r):
    r.session.expire_all()
    return list(r.session.scalars(sa.select(VolumeOutlineWrite)))


def candidate(r, **changes):
    volume = r.session.get(Volume, r.volume_id)
    target = writes.volume_submission_source(volume)
    text = '候选卷纲：兄弟先把钱数清，再讨论进城求医。'
    task = GenerationTask(id=uuid4(), project_id=r.pid, task_type='outline_volume', status='completed',
        result_text=text, params_json={'volume_idx': volume.volume_idx, 'submission': {'volume_source': target},
        'volume_candidate': {'version': 1, 'target': target, 'text_hash': writes.text_digest(text)}})
    for key, value in changes.items(): setattr(task, key, value)
    r.session.add(task); r.session.commit()
    return task


@pytest.mark.asyncio
async def test_reads_the_confirmed_outline_and_all_competing_sources(outline_book):
    value = await source(outline_book)
    assert value['outline']['id'] == str(outline_book.outline_id)
    assert {item['id'] for item in value['selection']} == {str(outline_book.outline_id), str(outline_book.alternative_id)}
    assert value['volume_id'] == str(outline_book.volume_id) and value['volume_idx'] == 1
    assert not history(outline_book)


@pytest.mark.asyncio
async def test_save_records_full_history_and_retains_prose_and_chapter_plans(outline_book):
    r = outline_book
    before_chapters = [(row.id, row.content_text, deepcopy(row.outline_json)) for row in r.session.scalars(sa.select(Chapter))]
    body = await request(r)
    response = await save(r, body)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['status'] == 'saved' and receipt['request_id'] == body['request_id']
    assert receipt['changed'] is True and receipt['preserved_chapters'] == 2
    assert receipt['source'] == await source(r)
    assert receipt['source']['outline']['version'] == 5
    assert receipt['source']['outline']['content_json']['raw_text'] == body['text']
    row = history(r)[0]
    assert {key: row.before_json[key] for key in body['expected_source']} == body['expected_source']
    assert row.before_json['competing_outlines'][0]['id'] == str(r.alternative_id)
    assert row.before_json['competing_outlines'][0]['content_json']['raw_text'] == '未采用的另一份卷纲。'
    assert row.result_json == receipt
    assert [(ch.id, ch.content_text, ch.outline_json) for ch in r.session.scalars(sa.select(Chapter))] == before_chapters
    effective = outline_for_generation(receipt['source']['outline']['content_json'])
    assert effective['raw_text'] == body['text'] and 'chapter_summaries' not in effective


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['request_id', 'expected_source'])
async def test_missing_save_precondition_does_not_change_the_outline(outline_book, missing):
    body = await request(outline_book)
    del body[missing]
    assert (await save(outline_book, body)).status_code == 428
    assert not history(outline_book)
    assert outline_book.session.get(Outline, outline_book.outline_id).version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['body', 'alternative', 'confirmation', 'volume_title', 'volume_index'])
async def test_other_connection_edits_invalidate_the_complete_source(outline_book, field):
    r = outline_book
    body = await request(r)
    with Session(r.engine) as other:
        if field == 'volume_title': other.get(Volume, r.volume_id).title = '作者后来改的卷名'
        elif field == 'volume_index': other.get(Volume, r.volume_id).volume_idx = 2
        else:
            row = other.get(Outline, r.alternative_id if field == 'alternative' else r.outline_id)
            if field == 'confirmation': row.is_confirmed = 0
            else: row.content_json = {**row.content_json, 'raw_text': '另一窗口保存的完整卷纲。'}
        other.commit()
    response = await save(r, body)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'volume_outline_conflict'
    assert not history(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_lost_commit_ack_retains_the_same_request_and_full_original(outline_book, mode):
    r = outline_book
    body = await request(r)
    r.fail_commit, r.lose_ack = mode == 'before', mode == 'after'
    response = await save(r, body)
    assert response.status_code == 503 and 'private' not in response.text
    assert response.json()['detail']['request_id'] == body['request_id']
    assert len(history(r)) == (0 if mode == 'before' else 1)
    response = await save(r, body)
    assert response.status_code == 200 and len(history(r)) == 1
    assert {key: history(r)[0].before_json[key] for key in body['expected_source']} == body['expected_source']
    assert r.session.get(Outline, r.outline_id).version == 5


@pytest.mark.asyncio
async def test_retry_reads_the_original_receipt_after_later_edits_and_volume_deletion(outline_book):
    r = outline_book
    body = await request(r)
    receipt = (await save(r, body)).json()
    later = await request(r, text='作者第二次另作安排，保留这份最新稿。')
    assert (await save(r, later)).status_code == 200
    assert (await save(r, body)).json() == receipt
    assert (await source(r))['outline']['content_json']['raw_text'] == later['text']
    r.session.execute(sa.delete(Volume).where(Volume.id == r.volume_id)); r.session.commit()
    assert (await save(r, body)).json() == receipt
    async with r.client() as client:
        details = await client.get(f'{r.base}/{r.volume_id}/history/{body["request_id"]}')
    assert details.status_code == 200 and details.json()['receipt'] == receipt
    assert len(history(r)) == 2


@pytest.mark.asyncio
async def test_request_id_cannot_be_reused_for_different_text(outline_book):
    r = outline_book
    body = await request(r)
    assert (await save(r, body)).status_code == 200
    response = await save(r, {**body, 'text': '另一份改稿。'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'volume_outline_request_conflict'
    assert len(history(r)) == 1


@pytest.mark.asyncio
async def test_first_volume_outline_has_a_complete_absence_precondition(outline_book):
    r = outline_book
    r.session.execute(sa.delete(Outline)); r.session.commit()
    body = await request(r)
    assert body['expected_source']['outline'] is None
    response = await save(r, body)
    assert response.status_code == 200, response.text
    assert response.json()['source']['outline']['version'] == 1
    assert len(list(r.session.scalars(sa.select(Outline)))) == 1
    assert (await save(r, body)).json() == response.json()


@pytest.mark.asyncio
async def test_same_saved_text_returns_a_receipt_without_revising_the_outline(outline_book):
    r = outline_book
    body = await request(r)
    body['text'] = body['expected_source']['outline']['content_json']['raw_text']
    response = await save(r, body)
    assert response.status_code == 200 and response.json()['changed'] is False
    assert response.json()['source'] == body['expected_source']
    assert len(history(r)) == 1 and r.session.get(Outline, r.outline_id).version == 2


@pytest.mark.asyncio
async def test_candidate_preview_uses_its_stable_volume_and_author_can_edit_before_adoption(outline_book):
    r = outline_book
    task = candidate(r)
    async with r.client() as client:
        preview = await client.get(f'{r.base}/candidates/{task.id}')
    assert preview.status_code == 200, preview.text
    view = preview.json()
    assert view['source']['volume_id'] == str(r.volume_id)
    body = await request(r, expected_source=view['source'], candidate=view['candidate'],
                         text=view['candidate']['text'] + '作者补充：求医的钱单独收好。')
    response = await save(r, body)
    assert response.status_code == 200 and response.json()['candidate_task_id'] == str(task.id)
    assert response.json()['source']['outline']['content_json']['raw_text'] == body['text']
    assert r.session.get(GenerationTask, task.id).result_text == view['candidate']['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['running', 'cancelled', 'legacy', 'changed_text', 'moved', 'params_changed', 'wrong_project', 'wrong_type'])
async def test_incomplete_stale_or_ambiguous_candidates_are_not_formally_adopted(outline_book, change):
    r = outline_book
    task = candidate(r)
    body = await request(r, candidate={'task_id': str(task.id), 'text': task.result_text})
    if change in {'running', 'cancelled'}: task.status = change
    elif change == 'legacy': task.params_json = {'volume_idx': 1}
    elif change == 'changed_text': task.result_text += '另一份候选的末尾。'
    elif change == 'params_changed': task.params_json = {**task.params_json, 'volume_idx': 2}
    elif change == 'moved': r.session.get(Volume, r.volume_id).volume_idx = 2
    elif change == 'wrong_project': task.project_id = uuid4()
    elif change == 'wrong_type': task.task_type = 'chapter'
    r.session.commit()
    response = await save(r, body)
    assert response.status_code in {404, 409} and not history(r)
    assert r.session.get(Outline, r.outline_id).version == 2


@pytest.mark.asyncio
async def test_full_candidate_comparison_rejects_a_different_preview(outline_book):
    r = outline_book
    task = candidate(r)
    body = await request(r, candidate={'task_id': str(task.id), 'text': task.result_text + '不属于该任务的一句。'})
    assert (await save(r, body)).status_code == 409 and not history(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['trash', 'other_project', 'missing_volume'])
async def test_new_writes_check_live_project_and_volume_scope(outline_book, scope):
    r = outline_book
    body = await request(r)
    if scope == 'trash':
        r.session.get(Project, r.pid).deleted_at = datetime.now(timezone.utc)
        r.session.commit()
    elif scope == 'other_project':
        r.base = f'/api/projects/{uuid4()}/volume-outlines'
    else: r.volume_id = uuid4()
    assert (await save(r, body)).status_code == 404 and not history(r)


@pytest.mark.asyncio
async def test_history_cascades_only_on_permanent_project_deletion(outline_book):
    r = outline_book
    assert (await save(r, await request(r))).status_code == 200
    r.session.execute(sa.delete(Project).where(Project.id == r.pid)); r.session.commit()
    assert not history(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('number', [0, 1])
async def test_invalid_or_duplicate_volume_numbers_are_not_guessed(outline_book, number):
    r = outline_book
    if number == 0:
        r.session.get(Volume, r.volume_id).volume_idx = 0
    else:
        r.session.add(Volume(project_id=r.pid, title='另一卷', volume_idx=1))
    r.session.commit()
    async with r.client() as client:
        response = await client.get(f'{r.base}/{r.volume_id}')
    assert response.status_code in {409, 422} and not history(r)


@pytest.mark.asyncio
async def test_boolean_candidate_version_is_not_a_verified_receipt(outline_book):
    r = outline_book
    task = candidate(r)
    task.params_json = {**task.params_json, 'volume_candidate': {**task.params_json['volume_candidate'], 'version': True}}
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, 'params_json')
    r.session.commit()
    async with r.client() as client:
        response = await client.get(f'{r.base}/candidates/{task.id}')
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_adoption_records_every_superseded_confirmation_even_for_same_text(outline_book):
    r = outline_book
    r.session.get(Outline, r.outline_id).version = 6
    r.session.get(Outline, r.alternative_id).is_confirmed = 1
    r.session.commit()
    body = await request(r)
    body['text'] = body['expected_source']['outline']['content_json']['raw_text']
    response = await save(r, body)
    assert response.status_code == 200 and response.json()['changed'] is True
    saved = history(r)[0]
    assert saved.before_json['competing_outlines'][0]['is_confirmed'] == 1
    assert r.session.get(Outline, r.alternative_id).is_confirmed == 0


@pytest.mark.asyncio
async def test_history_pages_ties_and_retains_cursor_scope(outline_book):
    r = outline_book
    for index in range(5):
        assert (await save(r, await request(r, text=f'第{index}次完整修订。'))).status_code == 200
    stamp = datetime(2026, 10, 9, tzinfo=timezone.utc)
    r.session.execute(sa.update(VolumeOutlineWrite).values(created_at=stamp)); r.session.commit()
    ids = sorted([str(row.id) for row in history(r)], reverse=True)
    async with r.client() as client:
        page = (await client.get(f'{r.base}/{r.volume_id}/history?limit=2')).json()
        assert [row['request_id'] for row in page['items']] == ids[:2] and page['next_cursor'] == ids[1]
        assert (await save(r, await request(r, text='翻页期间新增的最新稿。'))).status_code == 200
        next_page = (await client.get(f'{r.base}/{r.volume_id}/history?limit=2&cursor={page["next_cursor"]}')).json()
        assert [row['request_id'] for row in next_page['items']] == ids[2:4]
        last = (await client.get(f'{r.base}/{r.volume_id}/history?limit=2&cursor={next_page["next_cursor"]}')).json()
        assert [row['request_id'] for row in last['items']] == ids[4:] and last['next_cursor'] is None
        foreign = await client.get(f'{r.base}/{uuid4()}/history?cursor={ids[0]}')
        assert foreign.status_code == 422


@pytest.mark.asyncio
async def test_structured_edits_share_receipts_and_only_sync_proven_untouched_stubs(outline_book):
    r = outline_book
    chapters = list(r.session.scalars(sa.select(Chapter).order_by(Chapter.chapter_idx)))
    old = {'volume_id': str(r.volume_id), 'volume_idx': 1,
           'chapter_summaries': [{'chapter_idx': ch.chapter_idx, 'summary': f'旧摘要{ch.chapter_idx}'} for ch in chapters]}
    r.session.get(Outline, r.outline_id).content_json = deepcopy(old)
    chapters[0].outline_json = deepcopy(old['chapter_summaries'][0])
    chapters[0].status, chapters[0].content_text = 'draft', ''
    chapters[1].outline_json = {'scene_beats': ['作者独立细纲：先开门，再扶人坐下。']}
    authored_plan = deepcopy(chapters[1].outline_json)
    r.session.commit()
    new = deepcopy(old)
    for item in new['chapter_summaries']: item['summary'] = '新安排：核对两千元的收据。'
    body = await request(r, content_json=new)
    del body['text']
    response = await save(r, body)
    assert response.status_code == 200, response.text
    assert response.json()['submitted_json'] == new and response.json()['synced_chapters'] == 1
    assert response.json()['preserved_chapters'] == 2
    assert r.session.get(Chapter, chapters[0].id).outline_json['summary'] == new['chapter_summaries'][0]['summary']
    assert r.session.get(Chapter, chapters[1].id).outline_json == authored_plan
    assert r.session.get(Chapter, chapters[1].id).content_text
    assert (await save(r, body)).json() == response.json() and len(history(r)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'content_json': {}}, {'content_json': {'volume_idx': 1}}, {'content_json': {'volume_idx': 2}},
    {'text': '混用', 'content_json': {'summary': '结构稿'}}, {'content_json': {'summary': '结构稿'}, 'candidate': {'task_id': str(uuid4()), 'text': '候选'}}])
async def test_incomplete_or_rebound_structure_input_does_not_change_saved_outline(outline_book, payload):
    r = outline_book
    body = await request(r)
    del body['text']
    body.update(payload)
    response = await save(r, body)
    assert response.status_code == 422 and not history(r)
    assert r.session.get(Outline, r.outline_id).version == 2


@pytest.mark.asyncio
async def test_author_revision_does_not_reuse_a_model_verdict_about_the_old_outline(outline_book):
    r = outline_book
    row = r.session.get(Outline, r.outline_id)
    row.content_json = {**row.content_json, '_quality_status': 'degraded_structural_draft', '_consistency_report': {'passed': False}}
    r.session.commit()
    response = await save(r, await request(r))
    assert response.status_code == 200
    content = response.json()['source']['outline']['content_json']
    assert '_quality_status' not in content and '_consistency_report' not in content
    assert history(r)[0].before_json['outline']['content_json']['_quality_status'] == 'degraded_structural_draft'
