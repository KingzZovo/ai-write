"""Rescans retain complete lists and reuse only exact, already decided evidence."""
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update, delete, text

from app.models.project import (Chapter, Project, Foreshadow, ForeshadowReviewJob as Job,
    ForeshadowDiscoveryRun as Run, ForeshadowDiscovery as Candidate,
    ForeshadowDiscoveryRunItem as Item, ForeshadowReviewRequest as Receipt)
from app.services.foreshadow_discovery import cached_discovery, DiscoveryIncomplete, detect_clues
from app.services.foreshadow_discovery_actions import accept_discovery, dismiss_discovery, list_discoveries
from app.services.foreshadow_discovery_reader import list_discovery_runs, DiscoveryRunNotFound
from app.services.foreshadow_review_jobs import create_review_job, retry_review_job, ReviewSourceNotReady
from app.services.foreshadow_review_requests import ReviewRequestConflict
from app.services import foreshadow_review_worker as worker
from tests.test_foreshadow_discovery import discovery, persist, stored_id, candidate_json
from tests.test_manual_foreshadow_review import manual
from tests.test_foreshadow_review_worker import runtime
from tests.test_foreshadow_review_store import database


@pytest.fixture
def rescanning(discovery, manual):
    # Both fixtures share runtime. The second only substitutes the PG sequence
    # and source resolver; storage, lists and author decisions use real SQL.
    return discovery


async def request(r, request_id=None, *, rescan=True, content=None):
    with r.session.begin():
        return await create_review_job(r.db, r.pid, r.cid, r.source.content if content is None else content,
            request_id=request_id or uuid4(), rescan_discovery=rescan)


@pytest.mark.asyncio
async def test_new_scan_has_its_own_list_and_reason_without_resetting_original_decision(rescanning):
    r = rescanning; cid = await stored_id(r)
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid, actor='原作者')
    original = r.session.execute(select(Candidate.handled_at, Candidate.reason)).one(); r.session.commit()
    second = replace(r.batch, scan_revision=101, candidates=(replace(r.batch.candidates[0], reason='新的解释，尚未单独确认'),))
    assert await persist(r, second) == 1
    latest = await cached_discovery(r.db, r.source)
    assert latest.scan_revision == 101 and latest.source_sha256 == r.batch.source_sha256
    run_id = r.session.execute(select(Run.id).where(Run.scan_revision == 101)).scalar_one()
    row = (await list_discoveries(r.db, r.pid, None, run_id=run_id))['discoveries'][0]
    assert row['id'] == cid and row['status'] == 'dismissed' and row['handled_by'] == '原作者'
    assert row['handled_at'] == original.handled_at and row['reason'] == original.reason
    assert row['scan_reason'] == second.candidates[0].reason and row['reused_in_scan'] is True
    assert len(r.session.execute(select(Candidate.id)).all()) == 1
    assert len(r.session.execute(select(Item.run_id)).all()) == 2
    r.session.commit()
    # A retried attempt cannot merge a different non-deterministic response.
    assert await persist(r, replace(second, candidates=())) == 1
    assert (await cached_discovery(r.db, r.source, scan_revision=0)).cached_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [dict(description='存根是否真的寄出过信？'), dict(type='plot'),
    dict(evidence='把寄信的存根', evidence_start=2, evidence_end=8)])
async def test_changed_description_type_or_quote_needs_new_author_decision(rescanning, changes):
    r = rescanning; cid = await stored_id(r)
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid, actor='作者')
    if 'evidence' in changes:
        start = r.source.content.index(changes['evidence'])
        changes = dict(changes, evidence_start=start, evidence_end=start + len(changes['evidence']))
    await persist(r, replace(r.batch, scan_revision=100, candidates=(replace(r.batch.candidates[0], **changes),)))
    rows = r.session.execute(select(Candidate.id, Candidate.status, Candidate.handled_by)).all()
    assert {(row.id == cid, row.status, row.handled_by) for row in rows} == {
        (True, 'dismissed', '作者'), (False, 'pending', None)}


@pytest.mark.asyncio
async def test_empty_rescan_is_latest_success_and_preserves_old_candidates(rescanning):
    r = rescanning; cid = await stored_id(r)
    await persist(r, replace(r.batch, scan_revision=100, candidates=()))
    assert (await cached_discovery(r.db, r.source)).cached_count == 0
    assert (await cached_discovery(r.db, r.source, scan_revision=0)).cached_count == 1
    history = (await list_discovery_runs(r.db, r.pid, chapter_id=r.cid))['runs']
    assert [row['candidate_count'] for row in history] == [0, 1]
    assert all(row['source_state'] == 'current' and row['result_state'] == 'complete' for row in history)
    assert await list_discoveries(r.db, r.pid, None, run_id=history[0]['id']) == dict(discoveries=[], has_more=False)
    assert (await list_discoveries(r.db, r.pid))['discoveries'][0]['id'] == cid
    with pytest.raises(DiscoveryRunNotFound): await list_discoveries(r.db, uuid4(), None, run_id=history[0]['id'])
    assert (await list_discovery_runs(r.db, uuid4()))['runs'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('removed', [False, True])
async def test_rescan_never_restores_deleted_registration_or_overwrites_later_author_edits(rescanning, removed):
    r = rescanning; cid = await stored_id(r)
    with r.session.begin(): await accept_discovery(r.db, r.pid, cid, actor='原作者')
    r.session.execute(delete(Foreshadow).where(Foreshadow.id == cid) if removed else
        update(Foreshadow).where(Foreshadow.id == cid).values(description='作者后续修改'))
    r.session.commit()
    await persist(r, replace(r.batch, scan_revision=102))
    with r.session.begin(): assert await accept_discovery(r.db, r.pid, cid, actor='重试者') == cid
    record = r.session.execute(select(Foreshadow.description).where(Foreshadow.id == cid)).scalar_one_or_none()
    assert record == (None if removed else '作者后续修改')
    assert r.session.execute(select(Candidate.handled_by)).scalar_one() == '原作者'
    assert len(r.session.execute(select(Candidate.id)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing', 'position', 'reason', 'origin'])
async def test_broken_scan_membership_is_an_error_not_an_empty_cached_success(rescanning, corruption):
    r = rescanning; await stored_id(r)
    if corruption == 'missing': r.session.execute(delete(Item))
    if corruption == 'position': r.session.execute(update(Item).values(position=10))
    if corruption == 'reason': r.session.execute(update(Item).values(reason=''))
    if corruption == 'origin':
        other = replace(r.batch, scan_revision=10, candidates=())
        r.session.commit(); await persist(r, other)
        new_run = r.session.execute(select(Run.id).where(Run.scan_revision == 10)).scalar_one()
        r.session.execute(update(Candidate).values(run_id=new_run))
        r.session.execute(update(Run).where(Run.id == new_run).values(source_sha256='0' * 64))
    r.session.commit()
    with pytest.raises(DiscoveryIncomplete): await cached_discovery(r.db, r.source, scan_revision=0)
    history = (await list_discovery_runs(r.db, r.pid))['runs']
    broken = next(row for row in history if row['scan_revision'] == 0)
    assert broken['result_state'] == 'unknown' and broken['candidate_count'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', [True, 1.0, -1, 9223372036854775808])
async def test_invalid_scan_revision_cannot_create_or_lookup_a_scan(rescanning, revision):
    r = rescanning
    with pytest.raises(DiscoveryIncomplete): await persist(r, replace(r.batch, scan_revision=revision))
    with pytest.raises(DiscoveryIncomplete): await cached_discovery(r.db, r.source, scan_revision=revision)


@pytest.mark.asyncio
async def test_rescan_calls_model_once_and_ordinary_job_reuses_that_exact_scan(rescanning):
    r = rescanning; await persist(r)
    fresh = await request(r)
    assert (await worker.run_review_job(fresh['id'], sessions=r.sessions))['status'] == 'completed'
    r.detector.assert_awaited_once()
    run_id = r.session.execute(select(Job.discovery_run_id)).scalar_one()
    assert r.session.execute(select(Run.scan_revision).where(Run.id == run_id)).scalar_one() == 1001
    r.session.commit()
    ordinary = await request(r, rescan=False)
    assert (await worker.run_review_job(ordinary['id'], sessions=r.sessions))['status'] == 'completed'
    r.detector.assert_awaited_once()
    assert set(r.session.execute(select(Job.discovery_run_id)).scalars()) == {run_id}
    assert len(r.session.execute(select(Run.id)).all()) == 2


@pytest.mark.asyncio
async def test_active_rescan_coalesces_requests_and_active_ordinary_job_does_not_claim_rescan(rescanning):
    r = rescanning
    ordinary = await request(r, rescan=False)
    with pytest.raises(ReviewSourceNotReady, match='普通核查'): await request(r)
    assert len(r.session.execute(select(Receipt.id)).all()) == 1
    r.session.execute(update(Job).values(status='completed')); r.session.commit()
    one, two = await request(r), await request(r)
    assert one['id'] == two['id'] != ordinary['id'] and one['request_id'] != two['request_id']
    assert one['rescan_discovery'] and two['rescan_discovery']
    assert len(r.sequence_calls) == 2
    receipts = r.session.execute(select(Receipt.job_id)).scalars().all()
    assert receipts.count(UUID(one['id'])) == 2


@pytest.mark.asyncio
async def test_response_loss_replays_completed_request_before_checking_changed_prose(rescanning):
    r = rescanning; request_id = uuid4()
    original = await request(r, request_id)
    await worker.run_review_job(original['id'], sessions=r.sessions)
    r.resolver.reset_mock(); r.resolver.return_value = None
    replay = await request(r, request_id)
    assert replay == dict(original, status='completed')
    r.resolver.assert_not_awaited()
    assert len(r.sequence_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'mode', 'chapter', 'project'])
async def test_request_uuid_cannot_be_rebound_to_another_intent(rescanning, change):
    r = rescanning; request_id = uuid4(); await request(r, request_id)
    with pytest.raises(ReviewRequestConflict):
        with r.session.begin():
            await create_review_job(r.db, uuid4() if change == 'project' else r.pid,
                uuid4() if change == 'chapter' else r.cid, '变更正文' if change == 'body' else r.source.content,
                request_id=request_id, rescan_discovery=change != 'mode')
    assert len(r.sequence_calls) == 1


@pytest.mark.asyncio
async def test_deleted_chapter_keeps_request_and_scan_history_without_restarting_job(rescanning):
    r = rescanning
    r.session.execute(text('PRAGMA foreign_keys=ON')); r.session.commit()
    request_id = uuid4(); original = await request(r, request_id)
    await worker.run_review_job(original['id'], sessions=r.sessions)
    r.session.execute(delete(Chapter)); r.session.commit()
    replay = await request(r, request_id)
    assert replay == dict(original, status='unavailable')
    assert r.session.execute(select(Job.id)).all() == []
    assert len(r.session.execute(select(Receipt.id)).all()) == 1
    assert (await list_discovery_runs(r.db, r.pid))['runs'][0]['source_state'] == 'unavailable'
    r.session.execute(delete(Project)); r.session.commit()
    for model in (Receipt, Run, Candidate, Item): assert r.session.execute(select(model)).all() == []


@pytest.mark.asyncio
async def test_request_and_job_rollback_together(rescanning):
    r = rescanning; request_id = uuid4()
    with pytest.raises(RuntimeError):
        with r.session.begin():
            await create_review_job(r.db, r.pid, r.cid, r.source.content, request_id=request_id, rescan_discovery=True)
            raise RuntimeError('commit failed')
    assert r.session.execute(select(Receipt)).all() == [] and r.session.execute(select(Job)).all() == []
    r.session.commit()
    assert (await request(r, request_id))['status'] == 'pending'


@pytest.mark.asyncio
async def test_soft_deleted_project_only_allows_reading_the_original_request_outcome(rescanning):
    from app.models.project import _utcnow
    r = rescanning; request_id = uuid4(); original = await request(r, request_id)
    r.session.execute(update(Project).values(deleted_at=_utcnow())); r.session.commit()
    assert await request(r, request_id) == original
    with pytest.raises(ReviewSourceNotReady, match='作品'): await request(r)
    assert len(r.session.execute(select(Receipt.id)).all()) == 1


@pytest.mark.asyncio
async def test_failed_or_cancelled_rescan_keeps_old_success_and_retries_same_scan(rescanning):
    r = rescanning; await persist(r)
    first = await request(r)
    r.detector.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await worker.run_review_job(first['id'], sessions=r.sessions)
    assert (await cached_discovery(r.db, r.source)).scan_revision == 0
    assert len(r.session.execute(select(Run.id)).all()) == 1
    r.session.commit()
    with r.session.begin(): await retry_review_job(r.db, r.pid, UUID(first['id']))
    r.detector.side_effect = None
    r.detector.return_value = replace(r.batch, candidates=())
    assert (await worker.run_review_job(first['id'], sessions=r.sessions))['discovered'] == 0
    assert (await cached_discovery(r.db, r.source)).scan_revision == 1001
    assert len(r.sequence_calls) == 1


@pytest.mark.asyncio
async def test_failed_final_write_rolls_back_new_scan_members_but_preserves_old_scan(rescanning):
    r = rescanning; await persist(r); first = await request(r)
    @asynccontextmanager
    async def sessions():
        async with r.sessions() as db:
            execute = db.execute
            async def fail(stmt):
                if str(stmt).startswith('UPDATE foreshadow_review_jobs') and stmt.compile().params.get('status') == 'completed':
                    raise RuntimeError('final save failed')
                return await execute(stmt)
            yield NS(execute=fail, commit=db.commit)
    with pytest.raises(RuntimeError): await worker.run_review_job(first['id'], sessions=sessions)
    assert r.session.execute(select(Run.scan_revision)).scalars().all() == [0]
    assert len(r.session.execute(select(Item)).all()) == 1
    assert r.session.execute(select(Job.status, Job.discovery_run_id)).one() == ('failed', None)


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_first', [False, True])
async def test_api_never_claims_unconfirmed_commit_and_retries_fixed_request(rescanning, commit_first):
    from app.api import foreshadows as api
    r = rescanning; fail = [True]
    async def commit():
        if not fail[0] or commit_first: r.session.commit()
        if fail[0]: raise RuntimeError('private database details')
    adapter = NS(execute=r.db.execute, commit=AsyncMock(side_effect=commit))
    async def provide():
        try: yield adapter
        finally: r.session.rollback()
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide
    body = dict(chapter_id=str(r.cid), expected_content=r.source.content, request_id=str(uuid4()), rescan_discovery=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        url = f'/api/projects/{r.pid}/foreshadows/review-jobs'
        response = await client.post(url, json=body)
        assert response.status_code == 503 and 'private' not in response.text
        fail[0] = False
        response = await client.post(url, json=body)
        assert response.status_code == 200 and response.json()['request_id'] == body['request_id']
        assert response.json()['rescan_discovery'] is True
        for invalid in ({**body, 'request_id': None}, {**body, 'rescan_discovery': 'true'}, {**body, 'request_id': 'invalid'}):
            assert (await client.post(url, json=invalid)).status_code == 422
    assert len(r.session.execute(select(Job.id)).all()) == 1
    assert len(r.sequence_calls) == (1 if commit_first else 2)


@pytest.mark.asyncio
async def test_history_and_all_status_run_filter_have_project_scoped_api(rescanning):
    from app.api import foreshadows as api
    r = rescanning; cid = await stored_id(r)
    with r.session.begin(): await dismiss_discovery(r.db, r.pid, cid, actor='作者')
    await persist(r, replace(r.batch, scan_revision=55))
    async def provide(): return r.db
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        root = f'/api/projects/{r.pid}/foreshadows'
        history = (await client.get(root + '/discovery-runs?limit=1')).json()
        assert history['has_more'] is True and len(history['runs']) == 1
        run_id = history['runs'][0]['id']
        listed = (await client.get(root + f'/discoveries?status=all&run_id={run_id}')).json()['discoveries']
        assert len(listed) == 1 and listed[0]['id'] == str(cid) and listed[0]['status'] == 'dismissed'
        assert listed[0]['reused_in_scan'] is True
        assert (await client.get(root + f'/discoveries?run_id={uuid4()}')).status_code == 404
        for path in ('/discovery-runs?limit=0', '/discovery-runs?chapter_id=bad', '/discoveries?run_id=bad'):
            assert (await client.get(root + path)).status_code == 422


@pytest.mark.asyncio
async def test_scan_model_label_comes_from_call_result_and_is_not_invented(rescanning, monkeypatch):
    r = rescanning
    call = AsyncMock(return_value=NS(text=json.dumps([candidate_json()]), finish_reason='stop', model='gemini-3.1-pro'))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', call)
    batch = await detect_clues(r.source)
    assert batch.model_name == 'gemini-3.1-pro'
    await persist(r, replace(batch, scan_revision=100))
    assert (await list_discovery_runs(r.db, r.pid))['runs'][0]['model_name'] == 'gemini-3.1-pro'
