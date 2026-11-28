"""Exercise author-save receipts against SQLite and the real HTTP router."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
import sqlalchemy as sa

from app.api import chapter_manuscripts
from app.db.session import get_db
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, Project
from tests.test_version_application import BEFORE, RESTORED, book, current


@pytest.fixture
def manuscript(book):
    async def database():
        try:
            yield book.db
            await book.db.commit()
        except BaseException:
            await book.db.rollback()
            raise
    app = FastAPI()
    app.include_router(chapter_manuscripts.router, prefix='/api/projects/{project_id}/chapters')
    app.dependency_overrides[get_db] = database
    book.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test')
    book.path = f'/api/projects/{book.pid}/chapters/{book.cid}/content'
    return book


async def save(r, **overrides):
    async with r.client() as client:
        return await client.put(r.path, json={'content_text': RESTORED, 'expected_content_text': BEFORE, **overrides})


@pytest.mark.asyncio
async def test_read_returns_the_full_saved_body_and_actual_count(manuscript):
    manuscript.session.get(Chapter, manuscript.cid).word_count = 1
    manuscript.session.commit()
    async with manuscript.client() as client:
        response = await client.get(manuscript.path)
    assert response.status_code == 200
    assert response.json() == {'chapter_id': str(manuscript.cid), 'project_id': str(manuscript.pid),
                              'title': '交付', 'content_text': BEFORE, 'word_count': len(BEFORE), 'status': 'completed'}


@pytest.mark.asyncio
@pytest.mark.parametrize('expected', ['missing', None])
async def test_save_requires_full_original_text(manuscript, expected):
    body = {'content_text': RESTORED}
    if expected != 'missing': body['expected_content_text'] = expected
    async with manuscript.client() as client:
        response = await client.put(manuscript.path, json=body)
    assert response.status_code == 428
    assert current(manuscript)[0].content_text == BEFORE
    manuscript.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True])
async def test_stale_save_cannot_replace_another_connections_edit(manuscript, force):
    cached = manuscript.session.get(Chapter, manuscript.cid)
    latest = '作者补上了交钱的动作。' + BEFORE
    with manuscript.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == manuscript.cid).values(content_text=latest))
    assert cached.content_text == BEFORE
    response = await save(manuscript, force=force)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'chapter_edit_conflict'
    chapter, versions = current(manuscript)
    assert chapter.content_text == latest and len(versions) == 2
    manuscript.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_success_requires_commit_and_returns_exact_saved_manuscript(manuscript):
    response = await save(manuscript)
    assert response.status_code == 200
    receipt = response.json()
    assert receipt['status'] == 'saved' and receipt['changed'] is True
    assert receipt['manuscript']['content_text'] == RESTORED
    assert receipt['manuscript']['word_count'] == len(RESTORED)
    assert receipt['manuscript']['chapter_id'] == str(manuscript.cid)
    assert receipt['backup_version_id'] is None
    chapter, versions = current(manuscript)
    assert chapter.content_text == RESTORED and len(versions) == 2
    manuscript.dispatch.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_unconfirmed_save_retains_atomic_outcome_and_sends_no_notification(manuscript, mode):
    manuscript.fail_commit = mode == 'before'
    manuscript.lose_ack = mode == 'after'
    response = await save(manuscript)
    assert response.status_code == 503 and 'private' not in response.text
    assert current(manuscript)[0].content_text == (BEFORE if mode == 'before' else RESTORED)
    manuscript.dispatch.assert_not_called()
    if mode == 'after':
        assert (await save(manuscript)).status_code == 409
        async with manuscript.client() as client:
            assert (await client.get(manuscript.path)).json()['content_text'] == RESTORED


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['', '先去看腿。'])
async def test_large_reduction_needs_explicit_force_and_preserves_full_backup(manuscript, replacement):
    response = await save(manuscript, content_text=replacement)
    assert response.status_code == 400
    assert current(manuscript)[0].content_text == BEFORE
    response = await save(manuscript, content_text=replacement, force=True, status='draft')
    assert response.status_code == 200
    chapter, versions = current(manuscript)
    assert chapter.content_text == replacement and chapter.status == 'draft'
    backup = next(v for v in versions if v.source == 'author_edit_backup')
    assert backup.content_text == BEFORE and backup.word_count == len(BEFORE)
    assert response.json()['backup_version_id'] == str(backup.id)


@pytest.mark.asyncio
async def test_same_body_repairs_historical_count_without_backup_or_dispatch(manuscript):
    manuscript.session.get(Chapter, manuscript.cid).word_count = 1
    manuscript.session.commit()
    response = await save(manuscript, content_text=BEFORE, force=True)
    assert response.status_code == 200 and response.json()['changed'] is False
    chapter, versions = current(manuscript)
    assert chapter.word_count == len(BEFORE) and len(versions) == 2
    assert response.json()['backup_version_id'] is None
    manuscript.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['busy', 'pipeline', 'trash', 'foreign', 'missing'])
async def test_unavailable_or_busy_manuscript_is_not_changed(manuscript, condition):
    if condition == 'busy': manuscript.writer_lock.return_value = False
    elif condition == 'pipeline':
        manuscript.session.add(PipelineRun(project_id=manuscript.pid, state='queued')); manuscript.session.commit()
    elif condition == 'trash':
        manuscript.session.get(Project, manuscript.pid).deleted_at = datetime.now(timezone.utc); manuscript.session.commit()
    elif condition == 'foreign': manuscript.path = manuscript.path.replace(str(manuscript.pid), str(uuid4()))
    elif condition == 'missing': manuscript.path = manuscript.path.replace(str(manuscript.cid), str(uuid4()))
    response = await save(manuscript)
    assert response.status_code == (409 if condition in {'busy', 'pipeline'} else 404)
    assert current(manuscript)[0].content_text == BEFORE
    manuscript.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', ['body', 'backup'])
async def test_database_failure_rolls_back_explicit_reduction_with_backup(manuscript, failed):
    sql = ("CREATE TRIGGER reject_write BEFORE UPDATE OF content_text ON chapters BEGIN SELECT RAISE(ABORT, 'body failed'); END"
           if failed == 'body' else "CREATE TRIGGER reject_write BEFORE INSERT ON chapter_versions WHEN NEW.source='author_edit_backup' BEGIN SELECT RAISE(ABORT, 'backup failed'); END")
    with manuscript.engine.begin() as connection: connection.exec_driver_sql(sql)
    response = await save(manuscript, content_text='', force=True)
    assert response.status_code == 503
    chapter, versions = current(manuscript)
    assert chapter.content_text == BEFORE and len(versions) == 2
    manuscript.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_author_may_save_a_review_draft_but_cannot_claim_model_completion(manuscript):
    response = await save(manuscript, status='needs_review')
    assert response.status_code == 200
    assert current(manuscript)[0].status == 'needs_review'
    response = await save(manuscript, expected_content_text=RESTORED, status='completed')
    assert response.status_code == 422
    assert current(manuscript)[0].status == 'needs_review'


@pytest.mark.asyncio
async def test_author_save_strips_meta_chapter_reference_before_storing(manuscript):
    """2026-11-28:meta 泄漏必须在共享漏斗里剥掉,不能靠各调用点记得调。

    实测残留:卷2-29「他想起第12章那天早上」、卷3-15「第二卷末,旧操场…」。
    角色不知道自己处在第几章,这类引用一旦落库就是读者可见的硬伤。
    """
    from app.services.prose_sanitizer import sanitize_prose

    leaky = BEFORE + '他想起第12章那天早上,名字换成了他的。第二卷末,老槐树底下。'
    expected, hits = sanitize_prose(leaky)
    assert hits, '净化器本身必须能命中这两处引用'

    response = await save(manuscript, content_text=leaky)

    assert response.status_code == 200
    assert response.json()['manuscript']['content_text'] == expected
    assert '第12章' not in expected and '第二卷' not in expected
    chapter, _versions = current(manuscript)
    assert chapter.content_text == expected
    assert chapter.word_count == len(expected)


@pytest.mark.asyncio
async def test_author_save_keeps_a_clean_manuscript_byte_identical(manuscript):
    """净化不得静默改写本来就干净的作者稿(否则每次保存都产出版本差异)。"""
    response = await save(manuscript, content_text=RESTORED)
    assert response.status_code == 200
    assert response.json()['manuscript']['content_text'] == RESTORED
    chapter, _versions = current(manuscript)
    assert chapter.content_text == RESTORED
    assert chapter.word_count == len(RESTORED)


@pytest.mark.asyncio
async def test_content_change_enqueues_cognition_for_the_new_body(manuscript, monkeypatch):
    """2026-11-28:改正文必须为新正文重入队认知提取。

    认知记录按 source_hash(含 content_hash)绑定,正文一改旧记录即失效。若不在
    写入路径重入队,这一章就没有当前正文的记录,而台账对 completed 章是严格的 →
    它成为其后**所有**章的硬阻塞。实测:重写并采纳 卷2-5 之后,全书重写驱动全部
    卡在 character_cognition_not_ready。
    """
    from unittest.mock import AsyncMock
    import app.services.cognition_extraction as extraction

    enqueue = AsyncMock()
    monkeypatch.setattr(extraction, 'ensure_chapter_cognition', enqueue)

    response = await save(manuscript)

    assert response.status_code == 200
    enqueue.assert_awaited_once()
    args = enqueue.await_args.args
    assert str(args[1]) == str(manuscript.pid) and str(args[2]) == str(manuscript.cid)


@pytest.mark.asyncio
async def test_unchanged_save_does_not_enqueue_cognition(manuscript, monkeypatch):
    from unittest.mock import AsyncMock
    import app.services.cognition_extraction as extraction

    enqueue = AsyncMock()
    monkeypatch.setattr(extraction, 'ensure_chapter_cognition', enqueue)

    response = await save(manuscript, content_text=BEFORE, force=True)

    assert response.status_code == 200 and response.json()['changed'] is False
    enqueue.assert_not_awaited()
