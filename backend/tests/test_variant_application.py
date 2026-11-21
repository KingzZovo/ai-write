"""Actual SQLite saves and HTTP outcomes for applying an author's candidate.

The project advisory mutex and broker are replaced; this does not simulate
PostgreSQL concurrency or its transactional extraction-outbox triggers.
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

from app.api import chapters, variants
from app.db.session import get_db
from app.models.generation_run import GenerationRun
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, ChapterVariant, ChapterVersion, Project, Volume
from app.services import chapter_order
from app.services.entity_materialization_lock import MaterializationBusy


BEFORE = '沈青把信封收回，重新数了一遍钱。' * 100
CANDIDATE = '弟弟把信封放进内袋，答应天亮就走。' * 100


@pytest.fixture
def book(monkeypatch, tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "variants.sqlite"}')
    @sa.event.listens_for(engine, 'connect')
    def foreign_keys(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')
    for model in (Project, Volume, Chapter, GenerationRun, ChapterVariant, ChapterVersion, PipelineRun):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        project = Project(id=uuid4(), title='候选应用试书')
        volume = Volume(id=uuid4(), project_id=project.id, volume_idx=1, title='归家')
        chapter = Chapter(id=uuid4(), volume_id=volume.id, chapter_idx=1, global_idx=1,
                          title='交付', content_text=BEFORE, word_count=len(BEFORE), status='completed')
        run = GenerationRun(id=uuid4(), project_id=project.id, chapter_id=chapter.id)
        chosen = ChapterVariant(id=uuid4(), chapter_id=chapter.id, run_id=run.id, variant_idx=0,
                                content_text=CANDIDATE, word_count=3, is_winner=False, selected_by_user=False)
        previous = ChapterVariant(id=uuid4(), chapter_id=chapter.id, run_id=run.id, variant_idx=1,
                                  content_text='先前选择的候选。', is_winner=True, selected_by_user=True)
        # Explicit flush order makes the fixture valid with real foreign keys.
        session.add(project); session.flush()
        session.add(volume); session.flush()
        session.add(chapter); session.flush()
        session.add(run); session.flush()
        session.add_all([chosen, previous]); session.commit()
        r = NS(session=session, engine=engine, pid=project.id, cid=chapter.id, vid=chosen.id,
               previous=previous.id, fail_commit=False, lose_ack=False, queries=[])

        async def execute(query, *args, **kwargs):
            r.queries.append(query)
            return session.execute(query, *args, **kwargs)
        async def scalar(query, *args, **kwargs): return (await execute(query, *args, **kwargs)).scalar()
        async def get(model, identity): return session.get(model, UUID(str(identity)))
        async def flush(): session.flush()
        async def refresh(row): session.refresh(row)
        async def rollback(): session.rollback()
        async def commit():
            if r.fail_commit:
                r.fail_commit = False
                raise RuntimeError('private database failure')
            session.commit()
            if r.lose_ack:
                r.lose_ack = False
                raise RuntimeError('private acknowledgement failure')
        r.db = NS(execute=execute, scalar=scalar, get=get, add=session.add, flush=flush,
                  refresh=refresh, rollback=rollback, commit=commit)
        r.writer_lock = AsyncMock(return_value=True)
        r.materialization_lock = AsyncMock()
        monkeypatch.setattr(chapter_order, 'try_command_lock', r.writer_lock)
        monkeypatch.setattr(chapter_order, 'require_materialization_lock', r.materialization_lock)
        r.dispatch = Mock(return_value=True)
        monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction', r.dispatch)
        async def database():
            try:
                yield r.db
                await r.db.commit()
            except BaseException:
                await r.db.rollback()
                raise
        app = FastAPI()
        app.include_router(variants.router)
        app.include_router(chapters.router)
        app.dependency_overrides[get_db] = database
        r.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test')
        yield r
    engine.dispose()


def current(r):
    r.session.expire_all()
    chapter = r.session.get(Chapter, r.cid)
    choices = list(r.session.scalars(sa.select(ChapterVariant).order_by(ChapterVariant.variant_idx)))
    backups = list(r.session.scalars(sa.select(ChapterVersion)))
    return chapter, choices, backups


async def apply(r, **extra):
    async with r.client() as client:
        return await client.post(f'/api/variants/{r.vid}/select', json={'expected_content_text': BEFORE, **extra})


@pytest.mark.asyncio
async def test_applying_requires_the_manuscript_the_author_saw(book):
    async with book.client() as client:
        response = await client.post(f'/api/variants/{book.vid}/select', json={})
    assert response.status_code == 428
    chapter, choices, backups = current(book)
    assert chapter.content_text == BEFORE and not backups
    assert [row.selected_by_user for row in choices] == [False, True]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True])
async def test_stale_application_cannot_replace_later_author_edits_even_with_force(book, force):
    later = '她后来又改了一个动作。' + BEFORE
    book.session.get(Chapter, book.cid).content_text = later
    book.session.commit()
    response = await apply(book, force=force)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'chapter_edit_conflict'
    chapter, choices, backups = current(book)
    assert chapter.content_text == later and chapter.title == '交付' and not backups
    assert [row.selected_by_user for row in choices] == [False, True]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_application_reloads_author_edits_from_another_connection(book):
    cached = book.session.get(Chapter, book.cid)
    assert cached.content_text == BEFORE
    later = '另一处保存的作者改稿。' + BEFORE
    with book.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(content_text=later))
    assert cached.content_text == BEFORE  # The request's identity map is deliberately stale.

    response = await apply(book)

    assert response.status_code == 409
    chapter, choices, backups = current(book)
    assert chapter.content_text == later and not backups
    assert [row.selected_by_user for row in choices] == [False, True]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,code', [('', 'chapter_protect_empty_content'), ('先去看腿。', 'chapter_protect_shrink')])
@pytest.mark.parametrize('entrypoint', ['variant', 'chapter'])
async def test_candidate_and_manual_save_share_empty_and_shrink_protection(book, text, code, entrypoint):
    book.session.get(ChapterVariant, book.vid).content_text = text
    book.session.commit()
    if entrypoint == 'variant':
        response = await apply(book)
    else:
        async with book.client() as client:
            response = await client.put(f'/api/projects/{book.pid}/chapters/{book.cid}',
                json={'content_text': text, 'expected_content_text': BEFORE, 'title': '不能提前修改'})
    assert response.status_code == 400 and response.json()['detail']['code'] == code
    chapter, choices, backups = current(book)
    assert chapter.content_text == BEFORE and chapter.title == '交付' and not backups
    assert [row.selected_by_user for row in choices] == [False, True]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,force', [(CANDIDATE, False), ('先去看腿。', True), ('', True)], ids=['full', 'shortened', 'empty'])
async def test_success_saves_the_full_previous_body_and_choice_in_one_commit(book, text, force):
    book.session.get(ChapterVariant, book.vid).content_text = text
    book.session.commit()
    response = await apply(book, force=force)
    assert response.status_code == 200, response.text
    chapter, choices, backups = current(book)
    assert chapter.content_text == text and chapter.word_count == len(text)
    assert [row.selected_by_user for row in choices] == [True, False]
    assert [row.is_winner for row in choices] == [True, False]
    assert len(backups) == 1 and backups[0].content_text == BEFORE
    assert backups[0].metadata_json['variant_id'] == str(book.vid)
    assert response.json()['backup_version_id'] == str(backups[0].id)
    assert response.json()['applied_to_chapter'] is True
    assert response.json()['word_count'] == len(text)
    book.writer_lock.assert_awaited_once()
    book.materialization_lock.assert_awaited_once()
    book.dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_choosing_without_application_keeps_the_body_and_needs_no_body_precondition(book):
    async with book.client() as client:
        response = await client.post(f'/api/variants/{book.vid}/select', json={'apply_to_chapter': False})
    assert response.status_code == 200, response.text
    chapter, choices, backups = current(book)
    assert chapter.content_text == BEFORE and not backups
    assert [row.selected_by_user for row in choices] == [True, False]
    assert response.json()['applied_to_chapter'] is False
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_applying_an_identical_body_does_not_create_an_extra_backup_or_dispatch(book):
    book.session.get(ChapterVariant, book.vid).content_text = BEFORE
    book.session.commit()
    response = await apply(book)
    assert response.status_code == 200, response.text
    chapter, choices, backups = current(book)
    assert chapter.content_text == BEFORE and not backups
    assert choices[0].selected_by_user
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_by', ['writer', 'pipeline', 'materialization'])
async def test_active_work_prevents_candidate_application_without_partial_choice(book, blocked_by):
    if blocked_by == 'writer': book.writer_lock.return_value = False
    elif blocked_by == 'pipeline':
        book.session.add(PipelineRun(id=uuid4(), project_id=book.pid, state='generating'))
        book.session.commit()
    else: book.materialization_lock.side_effect = MaterializationBusy('busy')
    response = await apply(book)
    assert response.status_code == 409
    chapter, choices, backups = current(book)
    assert chapter.content_text == BEFORE and not backups
    assert [row.selected_by_user for row in choices] == [False, True]
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_recycled_project_cannot_receive_an_applied_variant(book):
    book.session.get(Project, book.pid).deleted_at = datetime.now(timezone.utc)
    book.session.commit()
    response = await apply(book)
    assert response.status_code == 404
    chapter, choices, backups = current(book)
    assert chapter.content_text == BEFORE and not backups
    assert [row.selected_by_user for row in choices] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_reached_database', [False, True])
async def test_unconfirmed_commit_returns_no_success_and_preserves_atomic_outcome(book, commit_reached_database):
    book.fail_commit = not commit_reached_database
    book.lose_ack = commit_reached_database
    response = await apply(book)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'variant_selection_unconfirmed'
    assert 'private' not in response.text
    chapter, choices, backups = current(book)
    assert chapter.content_text == (CANDIDATE if commit_reached_database else BEFORE)
    assert [row.selected_by_user for row in choices] == ([True, False] if commit_reached_database else [False, True])
    assert len(backups) == int(commit_reached_database)
    if backups: assert backups[0].content_text == BEFORE
    book.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_real_database_failure_rolls_back_body_backup_and_selection_together(book):
    with book.engine.begin() as connection:
        connection.exec_driver_sql('''CREATE TRIGGER reject_selection BEFORE UPDATE OF selected_by_user
            ON chapter_variants WHEN NEW.selected_by_user = 1
            BEGIN SELECT RAISE(ABORT, 'forced selection write failure'); END''')

    response = await apply(book)

    assert response.status_code == 503 and 'forced selection' not in response.text
    chapter, choices, backups = current(book)
    assert chapter.content_text == BEFORE and not backups
    assert [row.selected_by_user for row in choices] == [False, True]
    book.dispatch.assert_not_called()
