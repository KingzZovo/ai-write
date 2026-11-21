"""Create a real target for HTTP generation tests that exercise writer fencing."""
from uuid import UUID
from sqlalchemy.engine import make_url
from app.config import settings
from app.db.session import async_session_factory
from app.models.project import Chapter, Volume


async def create_chapter_target(project_id):
    url=make_url(settings.DATABASE_URL)
    if url.host not in {'localhost','127.0.0.1'} or url.database != 'aiwrite_pipeline_test':
        raise RuntimeError('HTTP writer tests require the disposable aiwrite_pipeline_test database')
    async with async_session_factory() as db:
        volume=Volume(project_id=UUID(str(project_id)),volume_idx=1,title='测试卷')
        db.add(volume)
        await db.flush()
        chapter=Chapter(volume_id=volume.id,chapter_idx=1,title='测试章',outline_json={'summary':'测试概要'})
        db.add(chapter)
        await db.commit()
        return str(volume.id)
