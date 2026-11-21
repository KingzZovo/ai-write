"""Real PostgreSQL author-save and restore races; never run in offline mode."""
import asyncio
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from app.api import chapters, versions
from app.db.session import get_db
from app.models.project import Chapter, ChapterVersion
from tests.test_pipeline_control import runtime_db
from tests.test_version_application_pg import version_tables

pytestmark = pytest.mark.external_services


@pytest.mark.asyncio
@pytest.mark.parametrize('primary_writer', ['content', 'legacy'])
@pytest.mark.parametrize('other_writer', ['author', 'legacy', 'restore'])
async def test_only_one_writer_can_replace_the_same_reviewed_manuscript(runtime_db, monkeypatch, primary_writer, other_writer):
    r = runtime_db
    await version_tables(r)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        before = chapter.content_text or ''
        version = ChapterVersion(id=uuid4(), chapter_id=r.chapter, content_text='恢复的历史稿。')
        db.add(version)
        await db.commit()
        vid = version.id
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction', Mock(return_value=True))
    async def database():
        async with r.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    app = FastAPI()
    app.include_router(chapters.router)
    app.include_router(versions.router)
    app.dependency_overrides[get_db] = database
    path = f'/api/projects/{r.project}/chapters/{r.chapter}/content'
    legacy_path = f'/api/projects/{r.project}/chapters/{r.chapter}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        other = (client.put(path if other_writer == 'author' else legacy_path, json={'content_text': '另一个作者修改。', 'expected_content_text': before, 'force': True})
                 if other_writer != 'restore' else client.post(f'/api/chapters/{r.chapter}/versions/{vid}/rollback',
                    json={'expected_content_text': before, 'force': True}))
        responses = await asyncio.gather(client.put(path if primary_writer == 'content' else legacy_path, json={'content_text': '作者修改正文。',
                                         'expected_content_text': before, 'force': True}), other)
    assert sorted(response.status_code for response in responses) == [200, 409]
    result = next(response.json() for response in responses if response.status_code == 200)
    text = result['manuscript']['content_text'] if 'manuscript' in result else result['content_text']
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        assert chapter.content_text == text and chapter.word_count == len(text)
        backups = (await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == r.chapter,
            ChapterVersion.source.in_(['author_edit_backup', 'version_restore_backup'])))).all()
        assert len(backups) == 1 and backups[0].content_text == before
