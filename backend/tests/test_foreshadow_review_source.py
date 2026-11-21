import hashlib
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from app.services.foreshadow_review import review_scoped_chapter
from app.services.context_errors import RequiredContextUnavailable
from app.services.hook_manager import HookManager

PROJECT_ID, VOLUME_ID = str(uuid4()), str(uuid4())
STAMP = datetime(2026, 10, 8, tzinfo=timezone.utc)


def source():
    return (uuid4(), 913, '原稿中的寄信人。', STAMP, 'completed')


def db_for(*phases):
    return NS(execute=AsyncMock(side_effect=[NS(all=lambda rows=rows: rows) for rows in phases]),
              flush=AsyncMock(), commit=AsyncMock())


@pytest.mark.asyncio
async def test_review_binds_to_exact_persisted_chapter_and_rereads_scalar_columns():
    row = source()
    db = db_for([row], [row])
    candidate = NS(evidence='寄信人')
    manager = NS(check_resolution=AsyncMock(return_value=[candidate]))
    result = await review_scoped_chapter(db=db, manager=manager, project_id=PROJECT_ID, volume_id=VOLUME_ID,
                                        chapter_idx=3, expected_text=row[2])
    assert result.chapter_id == str(row[0]) and result.global_idx == 913
    assert result.source_sha256 == hashlib.sha256(row[2].encode()).hexdigest()
    assert result.source_updated_at == STAMP and result.source_status == 'completed'
    assert result.suggestions == (candidate,)
    manager.check_resolution.assert_awaited_once_with(PROJECT_ID, row[2], chapter_idx=913)
    queries = [call.args[0] for call in db.execute.call_args_list]
    for query in queries:
        compiled = query.compile(dialect=postgresql.dialect())
        assert 'chapters.content_text' in str(compiled) and 'volumes.project_id =' in str(compiled)
        assert {UUID(PROJECT_ID), UUID(VOLUME_ID), 3} <= set(compiled.params.values())
        assert query.get_execution_options()['autoflush'] is False
    assert row[0] in queries[1].compile(dialect=postgresql.dialect()).params.values()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'wrong_text', 'bad_index'])
async def test_invalid_dispatched_source_does_not_call_model(kind):
    row = source()
    rows = [row]
    if kind == 'missing': rows = []
    if kind == 'duplicate': rows = [row, row]
    if kind == 'bad_index': rows = [(row[0], 0, *row[2:])]
    db = db_for(rows)
    manager = NS(check_resolution=AsyncMock())
    with pytest.raises(RequiredContextUnavailable):
        await review_scoped_chapter(db=db, manager=manager, project_id=PROJECT_ID, volume_id=VOLUME_ID, chapter_idx=3,
                                    expected_text='旧稿' if kind == 'wrong_text' else row[2])
    manager.check_resolution.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['deleted_or_moved', 'replaced', 'text', 'index', 'version', 'status'])
async def test_source_changes_during_inference_discard_suggestions(kind):
    row = source()
    changed = list(row)
    index_value = {'replaced': (0, uuid4()), 'text': (2, '改稿'), 'index': (1, 914),
                   'version': (3, STAMP + timedelta(seconds=1)), 'status': (4, 'draft')}
    if kind in index_value:
        index, value = index_value[kind]
        changed[index] = value
    db = db_for([row], [] if kind == 'deleted_or_moved' else [tuple(changed)])
    manager = NS(check_resolution=AsyncMock(return_value=[NS(evidence='寄信人')]))
    with pytest.raises(RequiredContextUnavailable, match='source_changed'):
        await review_scoped_chapter(db=db, manager=manager, project_id=PROJECT_ID, volume_id=VOLUME_ID,
                                    chapter_idx=3, expected_text=row[2])
    manager.check_resolution.assert_awaited_once()
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_hook_delegates_volume_and_exact_dispatched_source(monkeypatch):
    from app.services import foreshadow_review_jobs
    helper = AsyncMock()
    monkeypatch.setattr(foreshadow_review_jobs, 'enqueue_saved_draft_review', helper)
    await HookManager()._enqueue_foreshadow_review(PROJECT_ID, VOLUME_ID, 3, '正文')
    helper.assert_awaited_once_with(PROJECT_ID, VOLUME_ID, 3, '正文')
