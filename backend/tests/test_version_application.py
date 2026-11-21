"""Real SQLite version/body transactions and ASGI contract checks.

Project advisory locking and the broker are explicitly replaced. Real PG
concurrency remains in the companion external-services file.
"""
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.api import versions
from app.db.session import get_db
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, ChapterVersion, Project, Volume
from app.services import chapter_order
from app.services.version_control import VersionControlService, VersionConflict, VersionUnavailable
from tests.test_generation_execution import LocalSession


BEFORE = '沈青把信封收回，重新数了一遍钱。' * 100
RESTORED = '弟弟把信封放进内袋，答应天亮就走。' * 100


@pytest.fixture
def book(monkeypatch, tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "versions.sqlite"}')
    @sa.event.listens_for(engine, 'connect')
    def foreign_keys(connection, _record): connection.execute('PRAGMA foreign_keys=ON')
    for model in (Project, Volume, Chapter, ChapterVersion, PipelineRun): model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        project = Project(id=uuid4(), title='版本恢复试书')
        volume = Volume(id=uuid4(), project_id=project.id, volume_idx=1, title='归来')
        chapters = [Chapter(id=uuid4(), volume_id=volume.id, chapter_idx=i+1, global_idx=i+1,
                            title='交付', content_text=BEFORE, word_count=len(BEFORE), status='completed') for i in range(2)]
        session.add(project); session.flush()
        session.add(volume); session.flush()
        session.add_all(chapters); session.flush()
        chosen = ChapterVersion(id=uuid4(), chapter_id=chapters[0].id, content_text=RESTORED, word_count=3, is_active=0)
        active = ChapterVersion(id=uuid4(), chapter_id=chapters[0].id, content_text=BEFORE, word_count=len(BEFORE), is_active=1)
        foreign = ChapterVersion(id=uuid4(), chapter_id=chapters[1].id, content_text='另一章的内容。', is_active=1)
        session.add_all([chosen, active, foreign]); session.commit()
        r = NS(engine=engine, session=session, db=LocalSession(session), pid=project.id, cid=chapters[0].id,
               vid=chosen.id, active=active.id, foreign=foreign.id, fail_commit=False, lose_ack=False)
        async def commit():
            if r.fail_commit:
                r.fail_commit = False
                raise RuntimeError('private commit error')
            session.commit()
            if r.lose_ack:
                r.lose_ack = False
                raise RuntimeError('private acknowledgement error')
        r.db.commit = commit
        r.writer_lock = AsyncMock(return_value=True)
        monkeypatch.setattr(chapter_order, 'try_command_lock', r.writer_lock)
        monkeypatch.setattr(chapter_order, 'require_materialization_lock', AsyncMock())
        def notify(**_kwargs):
            # A separate connection must see the committed body and backup
            # before any broker notification can be sent.
            with Session(engine) as reader:
                assert reader.get(Chapter, r.cid).content_text == session.get(Chapter, r.cid).content_text
        r.dispatch = Mock(side_effect=notify)
        monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction', r.dispatch)
        async def database():
            try:
                yield r.db
                await r.db.commit()
            except BaseException:
                await r.db.rollback()
                raise
        app = FastAPI()
        app.include_router(versions.router)
        app.dependency_overrides[get_db] = database
        r.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test')
        yield r
    engine.dispose()


def current(r):
    r.session.expire_all()
    return (r.session.get(Chapter, r.cid), list(r.session.scalars(sa.select(ChapterVersion)
            .where(ChapterVersion.chapter_id == r.cid).order_by(ChapterVersion.created_at))))


async def restore(r, **values):
    async with r.client() as client:
        return await client.post(f'/api/chapters/{r.cid}/versions/{r.vid}/rollback',
                                 json={'expected_content_text': BEFORE, **values})


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [None, {}, {'expected_content_text': None}])
async def test_restore_requires_the_saved_body_the_author_saw(book, body):
    async with book.client() as client:
        response = await client.post(f'/api/chapters/{book.cid}/versions/{book.vid}/rollback', json=body)
    assert response.status_code == 428
    chapter, rows = current(book)
    assert chapter.content_text == BEFORE and len(rows) == 2
    assert [row.id for row in rows if row.is_active] == [book.active]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True])
async def test_restore_reloads_another_connections_changes_even_with_force(book, force):
    cached = book.session.get(Chapter, book.cid)
    later = '作者后来又改了动作。' + BEFORE
    with book.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(content_text=later))
    assert cached.content_text == BEFORE
    response = await restore(book, force=force)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'chapter_edit_conflict'
    chapter, rows = current(book)
    assert chapter.content_text == later and len(rows) == 2
    assert [row.id for row in rows if row.is_active] == [book.active]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_restore_backs_up_the_complete_body_and_commits_before_notifying(book):
    response = await restore(book)
    assert response.status_code == 200, response.text
    chapter, rows = current(book)
    backups = [row for row in rows if row.source == 'version_restore_backup']
    assert chapter.content_text == RESTORED and chapter.word_count == len(RESTORED)
    assert chapter.title == '交付' and chapter.status == 'completed'
    assert len(backups) == 1 and backups[0].content_text == BEFORE
    assert backups[0].metadata_json['restored_version_id'] == str(book.vid)
    assert response.json()['backup_version_id'] == str(backups[0].id)
    assert response.json()['content_text'] == RESTORED and response.json()['word_count'] == len(RESTORED)
    assert [row.id for row in rows if row.is_active] == [book.vid]
    book.dispatch.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['', '先去看腿。'])
@pytest.mark.parametrize('force', [False, True])
async def test_restore_uses_shared_content_loss_protection(book, replacement, force):
    book.session.get(ChapterVersion, book.vid).content_text = replacement
    book.session.commit()
    response = await restore(book, force=force)
    assert response.status_code == (200 if force else 400)
    chapter, rows = current(book)
    assert chapter.content_text == (replacement if force else BEFORE)
    assert len(rows) == (3 if force else 2)
    assert book.dispatch.call_count == int(force)


@pytest.mark.asyncio
async def test_identical_body_only_changes_selection_without_backup_or_notification(book):
    book.session.get(ChapterVersion, book.vid).content_text = BEFORE
    book.session.get(Chapter, book.cid).word_count = 1
    book.session.commit()
    response = await restore(book)
    assert response.status_code == 200
    assert response.json()['changed'] is False and response.json()['backup_version_id'] is None
    chapter, rows = current(book)
    assert chapter.content_text == BEFORE and len(rows) == 2
    assert chapter.word_count == len(BEFORE) == response.json()['word_count']
    assert [row.id for row in rows if row.is_active] == [book.vid]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_write', ['backup', 'body', 'selection'])
async def test_actual_database_write_failure_rolls_back_body_backup_and_selection(book, failed_write):
    statements = {
        'backup': "CREATE TRIGGER reject_restore BEFORE INSERT ON chapter_versions WHEN NEW.source='version_restore_backup' BEGIN SELECT RAISE(ABORT, 'backup failed'); END",
        'body': "CREATE TRIGGER reject_restore BEFORE UPDATE OF content_text ON chapters BEGIN SELECT RAISE(ABORT, 'body failed'); END",
        'selection': "CREATE TRIGGER reject_restore BEFORE UPDATE OF is_active ON chapter_versions BEGIN SELECT RAISE(ABORT, 'selection failed'); END",
    }
    with book.engine.begin() as connection: connection.exec_driver_sql(statements[failed_write])
    response = await restore(book)
    assert response.status_code == 503
    chapter, rows = current(book)
    assert chapter.content_text == BEFORE and len(rows) == 2
    assert [row.id for row in rows if row.is_active] == [book.active]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_unconfirmed_commit_never_returns_success_or_sends_notification(book, mode):
    book.fail_commit = mode == 'before'
    book.lose_ack = mode == 'after'
    response = await restore(book)
    assert response.status_code == 503 and 'private' not in response.text
    chapter, rows = current(book)
    assert chapter.content_text == (BEFORE if mode == 'before' else RESTORED)
    assert len(rows) == (2 if mode == 'before' else 3)
    book.dispatch.assert_not_called()
    if mode == 'after':
        response = await restore(book)
        assert response.status_code == 409  # Read the saved state before retrying.
        assert len(current(book)[1]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['busy', 'active_pipeline', 'trash', 'foreign_version'])
async def test_restore_rejects_busy_or_unavailable_sources_without_mutation(book, condition):
    if condition == 'busy': book.writer_lock.return_value = False
    elif condition == 'active_pipeline':
        book.session.add(PipelineRun(project_id=book.pid, state='queued')); book.session.commit()
    elif condition == 'trash':
        book.session.get(Project, book.pid).deleted_at = datetime.now(timezone.utc); book.session.commit()
    elif condition == 'foreign_version': book.vid = book.foreign
    response = await restore(book)
    assert response.status_code == (409 if condition in {'busy', 'active_pipeline'} else 404)
    chapter, rows = current(book)
    assert chapter.content_text == BEFORE and len(rows) == 2
    assert [row.id for row in rows if row.is_active] == [book.active]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_source_endpoint_reads_actual_body_instead_of_selected_version(book):
    book.session.get(ChapterVersion, book.active).content_text = '版本选择不代表实际正文。'
    book.session.commit()
    async with book.client() as client:
        response = await client.get(f'/api/chapters/{book.cid}/versions/source')
    assert response.status_code == 200
    assert response.json()['content_text'] == BEFORE and response.json()['word_count'] == len(BEFORE)
    assert response.json()['project_id'] == str(book.pid)


@pytest.mark.asyncio
async def test_activate_only_marks_a_comparison_version_and_commits_that_fact(book):
    async with book.client() as client:
        response = await client.post(f'/api/chapters/{book.cid}/versions/{book.vid}/activate')
    assert response.status_code == 200 and response.json()['applied_to_chapter'] is False
    chapter, rows = current(book)
    assert chapter.content_text == BEFORE and len(rows) == 2
    assert [row.id for row in rows if row.is_active] == [book.vid]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_real_branch_route_copies_the_selected_source_and_has_one_selection(book):
    async with book.client() as client:
        response = await client.post(f'/api/chapters/{book.cid}/versions/branch',
            json={'source_version_id': str(book.vid), 'branch_name': '另一种收束'})
        repeated = await client.post(f'/api/chapters/{book.cid}/versions/branch',
            json={'source_version_id': str(book.vid), 'branch_name': '另一种收束'})
    assert response.status_code == 201, response.text
    assert response.json()['content_text'] == RESTORED and response.json()['branch_name'] == '另一种收束'
    assert response.json()['parent_id'] == str(book.vid) and response.json()['word_count'] == len(RESTORED)
    assert repeated.status_code == 409
    chapter, rows = current(book)
    assert chapter.content_text == BEFORE
    assert [str(row.id) for row in rows if row.is_active] == [response.json()['id']]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create_parent', 'branch', 'diff_a', 'diff_b', 'merge'])
async def test_versions_cannot_use_or_expose_another_chapters_content(book, operation):
    async with book.client() as client:
        if operation == 'create_parent':
            response = await client.post(f'/api/chapters/{book.cid}/versions',
                json={'content': RESTORED, 'parent_id': str(book.foreign)})
        elif operation == 'branch':
            response = await client.post(f'/api/chapters/{book.cid}/versions/branch',
                json={'source_version_id': str(book.foreign), 'branch_name': '跨章'})
        elif operation in {'diff_a', 'diff_b'}:
            a, b = (book.foreign, book.vid) if operation == 'diff_a' else (book.vid, book.foreign)
            response = await client.get(f'/api/chapters/{book.cid}/versions/diff', params={'a': str(a), 'b': str(b)})
        else:
            with pytest.raises(VersionUnavailable):
                await VersionControlService(book.db).merge_branch(str(book.cid), str(book.foreign), str(book.vid))
            await book.db.rollback()
            response = None
    if response is not None:
        assert response.status_code == 404 and '另一章的内容' not in response.text
    chapter, rows = current(book)
    assert chapter.content_text == BEFORE and len(rows) == 2
    assert [row.id for row in rows if row.is_active] == [book.active]


@pytest.mark.asyncio
async def test_diff_headers_and_lines_remain_separate_without_terminal_newlines(book):
    book.session.get(ChapterVersion, book.active).content_text = '旧稿'
    book.session.get(ChapterVersion, book.vid).content_text = '新稿'
    book.session.commit()
    async with book.client() as client:
        response = await client.get(f'/api/chapters/{book.cid}/versions/diff', params={'a': str(book.active), 'b': str(book.vid)})
    assert response.status_code == 200
    lines = response.json()['diff'].splitlines()
    assert lines[:3] == ['--- parent', '+++ current', '@@ -1 +1 @@']
    assert '-旧稿' in lines and '+新稿' in lines
    assert lines.count('\\ No newline at end of file') == 2


@pytest.mark.asyncio
async def test_legacy_multiple_selections_are_not_resolved_by_first_row(book):
    book.session.get(ChapterVersion, book.vid).is_active = 1
    book.session.commit()
    with pytest.raises(VersionConflict):
        await VersionControlService(book.db).get_active_version(str(book.cid))
