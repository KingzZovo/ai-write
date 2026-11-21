"""The compatibility chapter update must obey the confirmed manuscript contract."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
import sqlalchemy as sa

from app.api import chapters
from app.db.session import get_db
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, Outline, Project
from tests.test_version_application import BEFORE, RESTORED, book, current


@pytest.fixture
def legacy(book):
    Outline.__table__.create(book.engine)
    async def scalars(statement):
        return book.session.scalars(statement)
    book.db.scalars = scalars
    async def database():
        try:
            yield book.db
            await book.db.commit()
        except BaseException:
            await book.db.rollback()
            raise
    app = FastAPI()
    app.include_router(chapters.router)
    app.dependency_overrides[get_db] = database
    book.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test')
    book.path = f'/api/projects/{book.pid}/chapters/{book.cid}'
    return book


async def save(r, **overrides):
    async with r.client() as client:
        return await client.put(r.path, json={'content_text': RESTORED, 'expected_content_text': BEFORE, **overrides})


@pytest.mark.asyncio
@pytest.mark.parametrize('expected', ['missing', None])
async def test_legacy_body_requires_original_before_any_metadata_mutation(legacy, expected):
    body = {'content_text': RESTORED, 'title': '不能提前改名', 'force': True}
    if expected != 'missing': body['expected_content_text'] = expected
    async with legacy.client() as client:
        response = await client.put(legacy.path, json=body)
    assert response.status_code == 428
    chapter, versions = current(legacy)
    assert chapter.content_text == BEFORE and chapter.title == '交付' and len(versions) == 2
    legacy.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True])
async def test_stale_legacy_body_cannot_replace_a_later_saved_manuscript(legacy, force):
    cached = legacy.session.get(Chapter, legacy.cid)
    latest = '作者后来补写的动作。' + BEFORE
    with legacy.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == legacy.cid).values(content_text=latest))
    assert cached.content_text == BEFORE
    response = await save(legacy, force=force, title='不能提前改名')
    assert response.status_code == 409
    chapter, versions = current(legacy)
    assert chapter.content_text == latest and chapter.title == '交付' and len(versions) == 2
    legacy.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_success_returns_the_same_confirmed_receipt_as_the_content_endpoint(legacy):
    response = await save(legacy, title='交钱以后', status='needs_review')
    assert response.status_code == 200, response.text
    result = response.json()
    receipt = result['manuscript_receipt']
    assert receipt['status'] == 'saved' and receipt['changed'] is True and receipt['backup_version_id'] is None
    assert receipt['manuscript'] == dict(project_id=str(legacy.pid), chapter_id=str(legacy.cid),
        title='交钱以后', content_text=RESTORED, word_count=len(RESTORED), status='needs_review')
    assert result['content_text'] == RESTORED and result['title'] == '交钱以后'
    assert current(legacy)[0].content_text == RESTORED
    # The fixture's dispatch callback reads the committed body on another connection.
    legacy.dispatch.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_unknown_commit_does_not_report_success_or_send_extraction(legacy, mode):
    legacy.fail_commit = mode == 'before'
    legacy.lose_ack = mode == 'after'
    response = await save(legacy, title='组合提交标题', force=True)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'chapter_save_unconfirmed'
    chapter, versions = current(legacy)
    assert chapter.content_text == (BEFORE if mode == 'before' else RESTORED)
    assert chapter.title == ('交付' if mode == 'before' else '组合提交标题')
    assert len(versions) == (2 if mode == 'before' else 3)
    legacy.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['', '先去看腿。'])
async def test_explicit_legacy_reduction_preserves_the_complete_previous_body(legacy, replacement):
    rejected = await save(legacy, content_text=replacement)
    assert rejected.status_code == 400
    response = await save(legacy, content_text=replacement, force=True, title='新标题', status='draft')
    assert response.status_code == 200
    chapter, versions = current(legacy)
    backup = next(version for version in versions if version.source == 'author_edit_backup')
    assert backup.content_text == BEFORE and backup.word_count == len(BEFORE)
    assert backup.metadata_json == {'title': '交付', 'status': 'completed'}
    assert response.json()['manuscript_receipt']['backup_version_id'] == str(backup.id)
    assert chapter.content_text == replacement and chapter.title == '新标题'


@pytest.mark.asyncio
async def test_same_body_corrects_count_without_backup_or_extraction(legacy):
    legacy.session.get(Chapter, legacy.cid).word_count = 1
    legacy.session.commit()
    response = await save(legacy, content_text=BEFORE, force=True, title='只改标题')
    assert response.status_code == 200
    chapter, versions = current(legacy)
    assert chapter.word_count == len(BEFORE) and chapter.title == '只改标题' and len(versions) == 2
    assert response.json()['manuscript_receipt']['changed'] is False
    legacy.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_body_outline_summary_and_reorder_are_one_atomic_change(legacy):
    response = await save(legacy, title='先去看腿', summary='弟弟已经拿到了两千元。',
        outline_json={'summary': '先去诊所'}, expected_outline_json={}, chapter_idx=3, force=True)
    assert response.status_code == 200, response.text
    chapter, versions = current(legacy)
    assert (chapter.title, chapter.content_text, chapter.chapter_idx, chapter.global_idx) == ('先去看腿', RESTORED, 3, 2)
    assert chapter.outline_json['summary'] == '先去诊所'
    assert chapter.summary == '弟弟已经拿到了两千元。' and chapter.summary_source_hash
    assert response.json()['manuscript_receipt']['manuscript']['content_text'] == RESTORED
    assert len(versions) == 3
    assert legacy.dispatch.call_args.kwargs['chapter_idx'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['outline', 'position'])
async def test_metadata_conflict_rolls_back_the_body_and_staged_backup(legacy, conflict):
    extra = {'outline_json': {'summary': '新稿'}, 'expected_outline_json': {'summary': '过期稿'}} if conflict == 'outline' else {'chapter_idx': 2}
    response = await save(legacy, force=True, title='不能提前改名', **extra)
    assert response.status_code == 409
    chapter, versions = current(legacy)
    assert chapter.title == '交付' and chapter.content_text == BEFORE and chapter.chapter_idx == 1
    assert len(versions) == 2
    legacy.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['busy', 'pipeline', 'trash', 'foreign', 'missing'])
async def test_legacy_body_obeys_project_ownership_and_generation_locks(legacy, condition):
    if condition == 'busy': legacy.writer_lock.return_value = False
    elif condition == 'pipeline':
        legacy.session.add(PipelineRun(project_id=legacy.pid, state='queued')); legacy.session.commit()
    elif condition == 'trash':
        legacy.session.get(Project, legacy.pid).deleted_at = datetime.now(timezone.utc); legacy.session.commit()
    elif condition == 'foreign': legacy.path = legacy.path.replace(str(legacy.pid), str(uuid4()))
    elif condition == 'missing': legacy.path = legacy.path.replace(str(legacy.cid), str(uuid4()))
    response = await save(legacy)
    assert response.status_code == (409 if condition in {'busy', 'pipeline'} else 404)
    assert current(legacy)[0].content_text == BEFORE
    legacy.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_backup_failure_leaves_body_and_metadata_unchanged(legacy):
    with legacy.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_backup BEFORE INSERT ON chapter_versions WHEN NEW.source='author_edit_backup' BEGIN SELECT RAISE(ABORT, 'backup failed'); END")
    response = await save(legacy, force=True, title='不能单独改名', chapter_idx=3)
    assert response.status_code == 503
    chapter, versions = current(legacy)
    assert chapter.content_text == BEFORE and chapter.title == '交付' and chapter.chapter_idx == 1
    assert len(versions) == 2
    legacy.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_only_update_remains_available_without_a_body_receipt(legacy):
    async with legacy.client() as client:
        response = await client.put(legacy.path, json={'title': '作者改名'})
    assert response.status_code == 200
    assert response.json()['manuscript_receipt'] is None
    assert current(legacy)[0].content_text == BEFORE
    legacy.dispatch.assert_not_called()
