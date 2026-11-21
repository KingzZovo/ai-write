"""Author-selected source identities, revisions, HTTP commits and exact prose."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import delete, inspect, insert, select, text, update
from sqlalchemy.dialects import postgresql

from app.api.foreshadows import router
from app.db.session import get_db
from app.models.project import Chapter, Volume, Project, Foreshadow, ForeshadowSourceLink as Link
from app.services import foreshadow_source_links as service
from app.services.foreshadow_source_link_contract import SourceLinkConflict, SourceLinkNotFound, make_source_link_command
from app.services.foreshadow_sources import inspect_foreshadow_sources
from app.services.foreshadow_lifecycle import load_active_foreshadows_for_context
from tests.test_foreshadow_chronology import book, add_clue
from tests.test_foreshadow_review_retention import migration


@pytest.fixture
def linked(book, monkeypatch):
    r = book
    r.fid = add_clue(r, r.chapters[0], legacy=True)
    r.mutex = AsyncMock()
    monkeypatch.setattr(service, 'require_materialization_lock', r.mutex)
    r.fail_commit = r.lose_ack = False
    async def commit():
        if r.fail_commit:
            r.fail_commit = False
            raise RuntimeError('private SQL details')
        r.session.commit()
        if r.lose_ack:
            r.lose_ack = False
            raise RuntimeError('private commit acknowledgement details')
    async def rollback():
        r.session.rollback()
    r.db.commit, r.db.rollback = commit, rollback
    r.app = FastAPI()
    r.app.include_router(router)
    async def database():
        yield r.db
    r.app.dependency_overrides[get_db] = database
    @r.app.middleware('http')
    async def actor(request, call_next):
        request.state.auth_username = 'writer-account'
        return await call_next(request)
    return r


async def context(r, chapter=None):
    return await service.read_source_context(r.db, r.pid, r.fid, chapter or r.chapters[0])


def command(r, ctx, *, operation='link', request_id=None, **changes):
    payload = dict(expected_link_id=str(ctx['latest_link']['id']) if ctx['latest_link'] else None, note='作者核对了存根与线索的关系。')
    if operation == 'link':
        payload.update(chapter_id=ctx['preview']['chapter_id'],
            source_version=ctx['preview']['source_version'], evidence='寄信的存根')
    payload.update(changes)
    return make_source_link_command(request_id or uuid4(), r.pid, r.fid,
        ctx['record_version'], operation, payload)


async def apply(r, cmd, actor='writer'):
    try:
        result = await service.apply_source_link(r.db, cmd, actor=actor)
        await r.db.commit()
        return result
    except BaseException:
        await r.db.rollback()
        raise


def records(r):
    return list(r.session.execute(select(*Foreshadow.__table__.columns).where(Foreshadow.id == r.fid)).mappings())


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['lifecycle', 'neo4j', None])
async def test_author_links_old_or_manual_records_without_rewriting_them(linked, owner):
    r = linked
    r.session.execute(update(Foreshadow).where(Foreshadow.id == r.fid).values(source=owner, type='暗伏笔'))
    body = '他们沿河回了村。' * 700 + '\r\n😀陈伯把寄信的存根放在桌上。'
    r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(content_text=body))
    r.session.commit()
    before = dict(records(r)[0])
    ctx = await context(r)
    assert ctx['preview']['content'] == body
    result = await apply(r, command(r, ctx))
    assert result['is_latest'] and result['revision'] == 1
    assert dict(records(r)[0]) == before and before['planted_chapter'] == 0
    source = (await inspect_foreshadow_sources(r.db, r.pid, records(r)))[str(r.fid)]
    assert source['source_state'] == 'current' and source['source_origin'] == 'author'
    assert source['source_global_idx'] == 11 and source['source_chapter_id'] == r.chapters[0]
    stored = r.session.execute(select(*Link.__table__.columns)).mappings().one()
    assert stored['evidence_start'] > 4000
    assert body[stored['evidence_start']:stored['evidence_end']] == '寄信的存根'
    items = await load_active_foreshadows_for_context(r.db, r.pid, 51)
    assert len(items) == 1 and items[0]['planted_chapter'] == 11
    assert await load_active_foreshadows_for_context(r.db, r.pid, 11) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'description', 'type', 'raw_index', 'chapter_order', 'volume_order', 'delete'])
async def test_changed_source_or_record_is_never_reused(linked, change):
    r = linked
    await apply(r, command(r, await context(r)))
    if change == 'body': r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(content_text='陈伯把寄信的存根放在桌上。作者续了一句。'))
    elif change == 'description': r.session.execute(update(Foreshadow).where(Foreshadow.id == r.fid).values(description='新线索'))
    elif change == 'type': r.session.execute(update(Foreshadow).where(Foreshadow.id == r.fid).values(type='plot'))
    elif change == 'raw_index': r.session.execute(update(Foreshadow).where(Foreshadow.id == r.fid).values(planted_chapter=51))
    elif change == 'chapter_order': r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(global_idx=12))
    elif change == 'volume_order': r.session.execute(update(Volume).where(Volume.id == r.volumes[0]).values(volume_idx=4))
    elif change == 'delete': r.session.execute(delete(Chapter).where(Chapter.id == r.chapters[0]))
    r.session.commit()
    source = (await inspect_foreshadow_sources(r.db, r.pid, records(r)))[str(r.fid)]
    assert source['source_state'] in {'changed', 'unavailable'}
    assert await load_active_foreshadows_for_context(r.db, r.pid, 51) == []


@pytest.mark.asyncio
async def test_status_and_title_changes_preserve_the_same_cited_prose(linked):
    r = linked
    await apply(r, command(r, await context(r)))
    r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(title='改题', status='needs_review'))
    r.session.execute(update(Foreshadow).where(Foreshadow.id == r.fid).values(status='ready', resolve_conditions_json=['新回应安排']))
    r.session.commit()
    assert (await inspect_foreshadow_sources(r.db, r.pid, records(r)))[str(r.fid)]['source_state'] == 'current'


@pytest.mark.asyncio
async def test_multiline_quote_preserves_original_newlines_and_unicode_offsets(linked):
    r = linked
    quote = '😀寄信的存根\r\n留在桌上。'
    body = '门响了。\r\n' + quote + '\r\n陈伯没有抬头。'
    r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(content_text=body))
    r.session.commit()
    await apply(r, command(r, await context(r), evidence=quote))
    row = r.session.execute(select(*Link.__table__.columns)).mappings().one()
    assert row['evidence'] == quote
    assert body[row['evidence_start']:row['evidence_end']] == quote
    assert (await inspect_foreshadow_sources(r.db, r.pid, records(r)))[str(r.fid)]['source_state'] == 'current'


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', [
    {'record_sha256': '0' * 64}, {'source_sha256': '0' * 64},
    {'evidence': '正文没有的原句'}, {'evidence_start': 0},
])
async def test_corrupt_association_cannot_authorize_prose_context(linked, tamper):
    r = linked
    await apply(r, command(r, await context(r)))
    r.session.execute(update(Link).values(**tamper)); r.session.commit()
    assert (await inspect_foreshadow_sources(r.db, r.pid, records(r)))[str(r.fid)]['source_state'] == 'unknown'
    assert await load_active_foreshadows_for_context(r.db, r.pid, 51) == []


@pytest.mark.asyncio
async def test_latest_unlink_or_stale_link_never_revives_model_candidate_evidence(linked):
    r = linked
    r.fid = add_clue(r, r.chapters[0])
    assert (await inspect_foreshadow_sources(r.db, r.pid, records(r)))[str(r.fid)]['source_state'] == 'current'
    first = await apply(r, command(r, await context(r), operation='unlink'))
    assert await load_active_foreshadows_for_context(r.db, r.pid, 51) == []
    second_cmd = command(r, await context(r, r.chapters[1]))
    second = await apply(r, second_cmd)
    r.session.execute(update(Chapter).where(Chapter.id == r.chapters[1]).values(content_text='已删去存根。'))
    r.session.commit()
    assert await load_active_foreshadows_for_context(r.db, r.pid, 301) == []
    history = await service.read_source_context(r.db, r.pid, r.fid)
    assert [row['id'] for row in history['history']] == [second['id'], first['id']]
    assert history['history'][0]['previous_id'] == first['id']
    assert (await apply(r, second_cmd, actor='retry'))['revision'] == 2


@pytest.mark.asyncio
async def test_old_retry_cannot_replace_a_newer_author_decision(linked):
    r = linked
    old = command(r, await context(r))
    await apply(r, old, actor='first-author')
    latest = await apply(r, command(r, await context(r), operation='unlink'), actor='second-author')
    retry = await apply(r, old, actor='third-author')
    assert not retry['is_latest'] and latest['revision'] == 2
    rows = (await service.read_source_context(r.db, r.pid, r.fid))['history']
    assert len(rows) == 2 and rows[1]['created_by'] == 'first-author'
    assert await load_active_foreshadows_for_context(r.db, r.pid, 51) == []


@pytest.mark.asyncio
async def test_two_intents_from_same_baseline_conflict_and_reused_id_is_rejected(linked):
    r = linked
    ctx = await context(r)
    first, second = command(r, ctx), command(r, ctx, note='另一项作者决定')
    await apply(r, first)
    with pytest.raises(SourceLinkConflict, match='link_changed'): await apply(r, second)
    with pytest.raises(SourceLinkConflict, match='request_id_reused'):
        await apply(r, command(r, ctx, request_id=first.id, note='不能用旧编号换内容'))
    assert len((await service.read_source_context(r.db, r.pid, r.fid))['history']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['body', 'record', 'duplicate_position'])
async def test_save_rechecks_the_full_preview_before_writing(linked, changed):
    r = linked
    cmd = command(r, await context(r))
    if changed == 'body': r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(content_text='陈伯把寄信的存根放在桌上。后文已改。'))
    elif changed == 'record': r.session.execute(update(Foreshadow).where(Foreshadow.id == r.fid).values(description='另一件事'))
    else: r.session.execute(update(Chapter).where(Chapter.id == r.chapters[1]).values(global_idx=11))
    r.session.commit()
    with pytest.raises(SourceLinkConflict): await apply(r, cmd)
    assert r.session.execute(select(Link.id)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['正文不存在', '陈伯'])
async def test_missing_or_repeated_quotes_cannot_create_an_association(linked, evidence):
    r = linked
    r.session.execute(update(Chapter).where(Chapter.id == r.chapters[0]).values(content_text='陈伯把寄信的存根放在桌上。陈伯又看了一眼。'))
    r.session.commit()
    with pytest.raises(SourceLinkConflict, match='quote_'):
        await apply(r, command(r, await context(r), evidence=evidence))
    assert r.session.execute(select(Link.id)).all() == []


@pytest.mark.asyncio
async def test_project_boundaries_and_lightweight_chapter_paging(linked):
    r = linked
    vid, cid = uuid4(), uuid4()
    r.session.execute(insert(Volume).values(id=vid, project_id=r.foreign, volume_idx=1, title='另一部作品'))
    r.session.execute(insert(Chapter).values(id=cid, volume_id=vid, chapter_idx=1, global_idx=1,
        title='保密章名', status='completed', content_text='不能跨作品使用的正文'))
    r.session.commit()
    with pytest.raises(SourceLinkNotFound): await service.read_source_context(r.db, r.foreign, r.fid)
    with pytest.raises(SourceLinkConflict, match='chapter_unavailable'): await context(r, cid)
    r.queries.clear()
    page = await service.read_source_chapters(r.db, r.pid, '', 2, 0)
    assert [row['id'] for row in page['chapters']] == r.chapters[:2] and page['has_more']
    assert all('content_text' not in str(query) for query in r.queries)
    assert (await service.read_source_chapters(r.db, r.pid, '保密章名'))['chapters'] == []
    assert [row['id'] for row in (await service.read_source_chapters(r.db, r.pid, '201'))['chapters']] == [r.chapters[2]]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['fail_commit', 'lose_ack'])
async def test_http_commit_uncertainty_preserves_request_identity_and_actor(linked, failure):
    r = linked
    cmd = command(r, await context(r))
    setattr(r, failure, True)
    headers = {'Idempotency-Key': str(cmd.id), 'If-Match': cmd.expected_version}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=r.app), base_url='http://test') as client:
        url = f'/api/projects/{r.pid}/foreshadows/{r.fid}/source'
        failed = await client.post(url, json=cmd.payload, headers=headers)
        assert failed.status_code == 503 and 'private' not in failed.text
        retried = await client.post(url, json=cmd.payload, headers=headers)
        assert retried.status_code == 200 and retried.json()['id'] == str(cmd.id)
        assert retried.json()['is_latest']
        row = r.session.execute(select(*Link.__table__.columns)).mappings().one()
        assert row['created_by'] == 'writer-account' and row['revision'] == 1
        rejected = await client.post(url, json={**cmd.payload, 'created_by': 'forged'}, headers=headers)
        assert rejected.status_code == 422
        history = await client.get(url)
        assert history.status_code == 200 and 'command_sha256' not in history.json()['latest_link']


@pytest.mark.asyncio
async def test_http_preview_is_scoped_and_writes_lock_sources_before_clue(linked):
    r = linked
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=r.app), base_url='http://test') as client:
        url = f'/api/projects/{r.pid}/foreshadows/{r.fid}/source'
        preview = await client.get(url, params={'chapter_id': str(r.chapters[0])})
        assert preview.status_code == 200 and preview.json()['preview']['content'].endswith('桌上。')
        assert (await client.get(f'/api/projects/{r.foreign}/foreshadows/{r.fid}/source')).status_code == 404
        assert (await client.get(f'/api/projects/{r.pid}/foreshadows/source-chapters?limit=1')).json()['has_more']
    await apply(r, command(r, await context(r)))
    sql = [str(query.compile(dialect=postgresql.dialect())) for query in r.queries]
    assert any('FOR SHARE' in query and 'projects.deleted_at IS NULL' in query for query in sql)
    chapter_lock = next(i for i, query in enumerate(sql) if 'FROM chapters JOIN volumes' in query and 'FOR UPDATE' in query)
    clue_lock = next(i for i, query in enumerate(sql) if 'FROM foreshadows' in query and 'FOR UPDATE' in query)
    assert chapter_lock < clue_lock
    r.mutex.assert_awaited_once_with(r.db, r.pid)


@pytest.mark.asyncio
@pytest.mark.parametrize('removed', [Chapter, Volume, Foreshadow])
async def test_source_deletion_preserves_links_but_project_purge_removes_them(linked, removed):
    r = linked
    r.session.execute(text('PRAGMA foreign_keys=ON')); r.session.commit()
    cmd = command(r, await context(r))
    await apply(r, cmd)
    before = dict(r.session.execute(select(*Link.__table__.columns)).mappings().one())
    r.session.execute(delete(removed)); r.session.commit()
    assert dict(r.session.execute(select(*Link.__table__.columns)).mappings().one()) == before
    history = await service.read_source_context(r.db, r.pid, r.fid)
    assert history['history'][0]['id'] == before['id']
    assert history['record_present'] is (removed is not Foreshadow)
    if removed is Foreshadow:
        assert history['record_version'] is None
    assert (await apply(r, cmd))['id'] == cmd.id  # No source is recreated.
    assert r.session.execute(select(removed.id)).all() == []
    r.session.execute(delete(Project)); r.session.commit()
    assert r.session.execute(select(Link.id)).all() == []


@pytest.mark.asyncio
async def test_migration_matches_model_and_refuses_discarding_author_history(linked):
    r = linked
    Link.__table__.drop(r.engine)
    step = migration('a1001943_foreshadow_source_links')
    assert step.down_revision == 'a1001942'
    with r.engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            step.upgrade()
            assert {col['name'] for col in inspect(connection).get_columns(Link.__tablename__)} == set(Link.__table__.columns.keys())
            assert {fk['referred_table'] for fk in inspect(connection).get_foreign_keys(Link.__tablename__)} == {'projects'}
    await apply(r, command(r, await context(r)))
    with r.engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match='Preserve author source links'): step.downgrade()
