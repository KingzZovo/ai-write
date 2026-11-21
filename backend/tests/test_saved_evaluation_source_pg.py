"""Real database gate; requires the configured PostgreSQL test runtime."""
import pytest
from sqlalchemy import select
from tests.test_pipeline_control import runtime_db
from tests.test_context_cross_volume import seed
from app.models.project import Chapter, ChapterEvaluation, EvaluateTask
from app.services.evaluation_source import chapter_evaluation_source
from app.api.evaluate import latest_chapter_evaluation


@pytest.mark.asyncio
async def test_saved_source_survives_roundtrip_and_detects_later_edit(runtime_db):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: ChapterEvaluation.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: EvaluateTask.__table__.create(c, checkfirst=True))
    _, volume_id, _, _ = await seed(r)
    async with r.sessions() as db:
        chapter = await db.scalar(select(Chapter).where(Chapter.volume_id == volume_id))
        cid = chapter.id
        source = chapter_evaluation_source(cid, chapter.content_text, chapter.outline_json)
        db.add(ChapterEvaluation(chapter_id=cid, overall=9, issues_json=[], source_json=source))
        await db.commit()
    async with r.sessions() as db:
        result = await latest_chapter_evaluation(cid, db)
        assert result.source_status == 'unchanged' and result.result['chapter_source'] == source
        chapter = await db.get(Chapter, cid)
        chapter.content_text = '作者后来重写的正文。'
        await db.commit()
    async with r.sessions() as db:
        result = await latest_chapter_evaluation(cid, db)
        assert result.source_status == 'changed'
        assert result.result['chapter_source'] == source
