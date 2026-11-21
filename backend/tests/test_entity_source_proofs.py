"""Source-proof reuse must reduce body IO without reusing obsolete facts."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.exc import SQLAlchemyError

from app.models.project import Chapter, Project, Volume
from app.services import entity_sources, entity_source_validation as validation
from app.services.entity_sources import filter_current_entity_rows
from app.services.source_proof_cache import EvidenceProofCache
from tests.test_cognition_validation import hashed_db
from tests.test_cognition_workflow_offline import cognition_db, state_db


@pytest.fixture(autouse=True)
def isolated_proofs(monkeypatch):
    cache = EvidenceProofCache()
    monkeypatch.setattr(validation, 'entity_evidence_proofs', cache)
    return cache


def candidate(source, **values):
    return dict(source_chapter_id=source.chapter_id, source_hash=source.source_hash,
                global_idx=source.global_idx, **values)


@pytest.mark.asyncio
async def test_verified_state_and_quote_reuse_the_same_body_proof(hashed_db):
    r = hashed_db
    rows = [candidate(r.source, state={'左手': '尚未恢复'}), candidate(r.source, excerpt=r.source.content)]
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, rows, before_global_idx=901) == rows
    assert r.body_reads == [dict(rows=1, chars=len(r.source.content))]
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, rows, before_global_idx=901) == rows
    assert r.body_reads == [], 'the same verified source was read in full again'


@pytest.mark.asyncio
async def test_a_new_quote_requires_its_own_verbatim_check(hashed_db):
    r = hashed_db
    original = candidate(r.source, excerpt=r.source.content)
    await filter_current_entity_rows(r.db, r.pid, [original], before_global_idx=901)
    changed = candidate(r.source, excerpt='这段话从未出现在当前保存稿里。')
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, [changed], before_global_idx=901) == []
    assert r.body_reads == [dict(rows=1, chars=len(r.source.content))]
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, [original], before_global_idx=901) == [original]
    assert r.body_reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'title', 'chapter_idx', 'global_idx', 'volume_idx', 'move',
                                   'project', 'draft', 'delete', 'duplicate_global'])
async def test_previous_proof_cannot_authorize_a_changed_current_source(hashed_db, change):
    r = hashed_db
    original = candidate(r.source, excerpt=r.source.content)
    await filter_current_entity_rows(r.db, r.pid, [original], before_global_idx=901)
    cid = UUID(r.source.chapter_id)
    values = {'body': {'content_text': '陈川现在仍未收到回执。'}, 'title': {'title': '作者的新标题'},
        'chapter_idx': {'chapter_idx': 2}, 'global_idx': {'global_idx': 901}, 'draft': {'status': 'draft'}}.get(change)
    if values:
        r.session.execute(update(Chapter).where(Chapter.id == cid).values(**values))
    elif change == 'delete': r.session.execute(delete(Chapter).where(Chapter.id == cid))
    elif change == 'volume_idx': r.session.execute(update(Volume).where(Volume.id == r.vid).values(volume_idx=3))
    elif change == 'project':
        foreign = uuid4()
        r.session.execute(insert(Project).values(id=foreign, title='另一部书'))
        r.session.execute(update(Volume).where(Volume.id == r.vid).values(project_id=foreign))
    elif change == 'move':
        vid = uuid4()
        r.session.execute(insert(Volume).values(id=vid, project_id=r.pid, volume_idx=3, title='移动目标卷'))
        r.session.execute(update(Chapter).where(Chapter.id == cid).values(volume_id=vid))
    else:
        r.session.execute(insert(Chapter).values(id=uuid4(), volume_id=r.vid, title='重复全书位置',
            chapter_idx=2, global_idx=900, content_text='另一章。', status='completed'))
    r.session.commit()
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, [original], before_global_idx=901) == []
    assert not r.body_reads


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['', None, ' \t\n', '\u3000\u2003\u0085'])
async def test_an_empty_body_receipt_is_not_a_proof_even_without_a_quote(hashed_db, body):
    r = hashed_db
    r.session.execute(update(Chapter).values(content_text=body)); r.session.commit()
    header = (await entity_sources.resolve_entity_source_headers(r.db, r.pid, [r.source.chapter_id]))[r.source.chapter_id]
    row = candidate(header, state={'未知': None})
    for _ in range(2):
        assert await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, '', 'invalid-hash'])
async def test_missing_stored_hash_still_reads_the_full_body_after_a_prior_proof(hashed_db, invalid):
    r = hashed_db
    row = candidate(r.source, excerpt=r.source.content)
    await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901)
    # Emulate unmigrated legacy metadata only in this disposable SQLite DB.
    r.session.execute(text('DROP TRIGGER saved_hash_update'))
    r.session.execute(update(Chapter).values(content_hash=invalid)); r.session.commit()
    for _ in range(2):
        r.body_reads.clear()
        assert await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901) == [row]
        assert r.body_reads == [dict(rows=1, chars=len(r.source.content))]
    r.session.execute(update(Chapter).values(content_text='这份改稿没有旧原句。')); r.session.commit()
    assert await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901) == []


@pytest.mark.asyncio
async def test_cached_proof_never_replays_old_entity_payloads(hashed_db):
    r = hashed_db
    first = candidate(r.source, status={'登记字段': '第一次提取的解释'})
    await filter_current_entity_rows(r.db, r.pid, [first], before_global_idx=901)
    current = candidate(r.source, status={'登记字段': '当前提取的解释', '明确空值': None})
    r.body_reads.clear()
    result = await filter_current_entity_rows(r.db, r.pid, [current, current], before_global_idx=901)
    assert result == [current, current] and result[0] is current and not r.body_reads
    # Only source/quote verification is reused; semantic interpretation still
    # belongs to the source record and to the writer/editor reviewing it.


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, '', ' \n', False, {}, []])
async def test_invalid_quote_types_do_not_reuse_a_no_quote_proof(hashed_db, bad):
    r = hashed_db
    plain = candidate(r.source, status={})
    await filter_current_entity_rows(r.db, r.pid, [plain], before_global_idx=901)
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, [candidate(r.source, excerpt=bad)], before_global_idx=901) == []
    assert not r.body_reads


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [None, 0, -1, True, '901', 900])
async def test_invalid_or_current_boundary_never_reads_a_body(hashed_db, boundary):
    r = hashed_db
    row = candidate(r.source, excerpt=r.source.content)
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=boundary) == []
    assert not r.body_reads


@pytest.mark.asyncio
async def test_future_and_foreign_source_ids_do_not_trigger_legacy_body_fallback(hashed_db):
    r = hashed_db
    future = uuid4()
    r.session.execute(insert(Chapter).values(id=future, volume_id=r.vid, chapter_idx=2, global_idx=901,
        title='未来章', content_text='未来章全文不应读取。', status='completed'))
    r.session.execute(text('DROP TRIGGER saved_hash_update'))
    r.session.execute(update(Chapter).where(Chapter.id == future).values(content_hash=None)); r.session.commit()
    r.body_reads.clear()
    forged_prior_position = dict(source_chapter_id=str(future), source_hash='0' * 64, global_idx=899)
    assert await filter_current_entity_rows(r.db, r.pid, [forged_prior_position], before_global_idx=901) == []
    assert await filter_current_entity_rows(r.db, uuid4(), [candidate(r.source)], before_global_idx=901) == []
    assert not r.body_reads


@pytest.mark.asyncio
async def test_edit_between_header_and_body_read_cannot_create_a_stale_proof(hashed_db, monkeypatch, isolated_proofs):
    r = hashed_db
    original = entity_sources.resolve_entity_sources
    async def edit_then_read(db, pid, ids, **kwargs):
        if ids:
            r.session.execute(update(Chapter).values(content_text='读到指纹后作者修改了正文。')); r.session.commit()
        return await original(db, pid, ids, **kwargs)
    monkeypatch.setattr(entity_sources, 'resolve_entity_sources', edit_then_read)
    row = candidate(r.source, excerpt=r.source.content)
    assert await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901) == []
    assert not isolated_proofs._entries


@pytest.mark.asyncio
async def test_hot_read_does_not_flush_or_discard_unsaved_author_work(hashed_db, monkeypatch):
    r = hashed_db
    row = candidate(r.source, excerpt=r.source.content)
    await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901)
    manuscript = r.session.get(Chapter, UUID(r.source.chapter_id))
    manuscript.content_text = '作者在本会话中尚未保存的正文。'
    manuscript.summary = '尚未保存的摘要。'
    flush = Mock(side_effect=AssertionError('read must not flush'))
    monkeypatch.setattr(r.session, 'flush', flush)
    assert await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901) == [row]
    assert manuscript.content_text == '作者在本会话中尚未保存的正文。' and manuscript.summary == '尚未保存的摘要。'
    flush.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('warm', [False, True])
async def test_database_failures_propagate_instead_of_returning_an_empty_context(hashed_db, warm):
    r = hashed_db
    row = candidate(r.source, excerpt=r.source.content)
    if warm: await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901)
    r.db.execute = AsyncMock(side_effect=SQLAlchemyError('temporary source read failure'))
    with pytest.raises(SQLAlchemyError):
        await filter_current_entity_rows(r.db, r.pid, [row], before_global_idx=901)


@pytest.mark.asyncio
async def test_evicted_proofs_are_revalidated_and_cache_contains_no_prose(hashed_db, monkeypatch):
    r = hashed_db
    cache = EvidenceProofCache(max_entries=1)
    monkeypatch.setattr(validation, 'entity_evidence_proofs', cache)
    full = candidate(r.source, excerpt=r.source.content)
    short = candidate(r.source, excerpt=r.changes[0]['evidence'])
    await filter_current_entity_rows(r.db, r.pid, [full], before_global_idx=901)
    await filter_current_entity_rows(r.db, r.pid, [short], before_global_idx=901)
    r.body_reads.clear()
    assert await filter_current_entity_rows(r.db, r.pid, [full], before_global_idx=901) == [full]
    assert r.body_reads == [dict(rows=1, chars=len(r.source.content))]
    assert len(cache._entries) == 1
    assert all(len(key) == 64 and set(key) <= set('0123456789abcdef') and value is None
               for key, value in cache._entries.items())
