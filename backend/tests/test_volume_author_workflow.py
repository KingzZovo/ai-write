"""Legacy volume writes and atomic moves through ASGI, with SQLite transactions."""
from copy import deepcopy
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.models.project import Chapter, Outline, Volume
from tests.test_volume_outline_writes import outline_book, request, save, source
from tests.test_version_application import book


async def legacy(r, action, body):
    url = f'/api/projects/{r.pid}/outlines/{r.outline_id}'
    async with r.client() as client:
        if action == 'confirm': return await client.post(url + '/confirm', json=body)
        return await client.request('PUT' if action == 'edit' else 'DELETE', url, json=body)


async def expectation(r):
    original = (await source(r))['outline']
    return dict(expected_content_json=original['content_json'], expected_version=original['version'],
                expected_is_confirmed=original['is_confirmed'])


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['edit', 'confirm', 'delete'])
@pytest.mark.parametrize('condition', ['missing', 'text_changed', 'confirmation_changed', 'version_changed'])
async def test_legacy_volume_writes_cannot_skip_the_author_snapshot(outline_book, action, condition):
    r = outline_book
    body = {} if condition == 'missing' else await expectation(r)
    if action == 'edit': body['content_json'] = {'volume_id': str(r.volume_id), 'volume_idx': 1, 'raw_text': '旧窗口修改'}
    row = r.session.get(Outline, r.outline_id)
    if condition == 'text_changed': row.content_json = {**row.content_json, 'raw_text': '作者后来保存的新卷纲'}
    elif condition == 'confirmation_changed': row.is_confirmed = 0
    elif condition == 'version_changed': row.version += 1
    r.session.commit()
    expected = deepcopy(row.content_json), row.version, row.is_confirmed
    response = await legacy(r, action, body)
    assert response.status_code == (428 if condition == 'missing' else 409), response.text
    r.session.expire_all()
    row = r.session.get(Outline, r.outline_id)
    assert (row.content_json, row.version, row.is_confirmed) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['edit', 'confirm', 'delete'])
@pytest.mark.parametrize('ack', ['ok', 'before', 'after'])
async def test_legacy_volume_write_returns_only_after_commit_confirmation(outline_book, action, ack):
    r = outline_book
    body = await expectation(r)
    if action == 'edit': body['content_json'] = {**body['expected_content_json'], 'raw_text': '本轮作者改稿'}
    r.fail_commit, r.lose_ack = ack == 'before', ack == 'after'
    response = await legacy(r, action, body)
    assert response.status_code == (503 if ack != 'ok' else 204 if action == 'delete' else 200), response.text
    assert 'private' not in response.text
    r.session.expire_all()
    row = r.session.get(Outline, r.outline_id)
    if action == 'delete': assert (row is None) == (ack != 'before')
    elif action == 'edit': assert row.content_json['raw_text'] == ('本轮作者改稿' if ack != 'before' else body['expected_content_json']['raw_text'])
    if ack == 'ok' and action != 'delete': assert response.json()['save_status'] == 'saved'


@pytest.mark.asyncio
async def test_legacy_volume_update_joins_the_project_writer_lock(outline_book):
    r = outline_book
    r.writer_lock.return_value = False
    response = await legacy(r, 'edit', {**await expectation(r), 'content_json': {'raw_text': '未获执行权的改稿'}})
    assert response.status_code == 409
    assert r.session.get(Outline, r.outline_id).version == 2


def next_volume(r):
    other = Volume(id=uuid4(), project_id=r.pid, volume_idx=4, title='进城')
    r.session.add(other); r.session.flush()
    chapter = Chapter(id=uuid4(), volume_id=other.id, chapter_idx=1, global_idx=3,
                      title='求医', content_text='作者在后一卷保存的正文。', outline_json={'summary': '问诊'})
    r.session.add(chapter); r.session.commit()
    return other, chapter


async def move(r, neighbor, **overrides):
    body = dict(neighbor_id=str(neighbor.id), expected_idx=1, expected_neighbor_idx=4, **overrides)
    async with r.client() as client:
        return await client.post(f'/api/projects/{r.pid}/volumes/{r.volume_id}/move', json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', ['ok', 'before', 'after'])
async def test_volume_move_swaps_once_with_global_order_and_keeps_history(outline_book, ack):
    r = outline_book
    other, last = next_volume(r)
    original = await request(r)
    saved = (await save(r, original)).json()
    prose = {row.id: row.content_text for row in r.session.scalars(sa.select(Chapter))}
    r.fail_commit, r.lose_ack = ack == 'before', ack == 'after'
    response = await move(r, other)
    assert response.status_code == (200 if ack == 'ok' else 503), response.text
    r.session.expire_all()
    changed = ack != 'before'
    assert r.session.get(Volume, r.volume_id).volume_idx == (4 if changed else 1)
    assert r.session.get(Volume, other.id).volume_idx == (1 if changed else 4)
    assert r.session.get(Chapter, last.id).global_idx == (1 if changed else 3)
    assert r.session.get(Chapter, r.cid).global_idx == (2 if changed else 1)
    assert {row.id: row.content_text for row in r.session.scalars(sa.select(Chapter))} == prose
    assert r.session.get(Outline, r.outline_id).content_json['volume_idx'] == (4 if changed else 1)
    assert (await save(r, original)).json() == saved
    if changed:
        assert (await move(r, other)).status_code == 409  # An uncertain retry cannot swap back.


@pytest.mark.asyncio
async def test_volume_move_rejects_nonadjacent_wrong_and_stale_neighbors(outline_book):
    r = outline_book
    other, _ = next_volume(r)
    middle = Volume(project_id=r.pid, volume_idx=2, title='中间卷')
    r.session.add(middle); r.session.commit()
    assert (await move(r, other)).status_code == 409
    async with r.client() as client:
        wrong = await client.post(f'/api/projects/{r.pid}/volumes/{r.volume_id}/move', json={
            'neighbor_id': str(uuid4()), 'expected_idx': 1, 'expected_neighbor_idx': 2})
        assert wrong.status_code == 404
        stale = await client.post(f'/api/projects/{r.pid}/volumes/{r.volume_id}/move', json={
            'neighbor_id': str(middle.id), 'expected_idx': 1, 'expected_neighbor_idx': 3})
        assert stale.status_code == 409
    assert r.session.get(Volume, r.volume_id).volume_idx == 1
