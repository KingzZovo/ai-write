"""Cognition work and entity work share one atomic source notification handoff."""
import pytest
from sqlalchemy import select
from app.models.cognition import ChapterCognition
from app.models.entity_extraction import ChapterEntityExtraction
from app.services.entity_outbox import drain_entity_source_outbox
from tests.test_entity_outbox import env, complete, pending
from tests.test_pipeline_control import runtime_db

@pytest.mark.asyncio
async def test_handoff_records_both_jobs_before_acknowledgement(env):
    r=env;await complete(r)
    await drain_entity_source_outbox(r.sessions)
    async with r.sessions() as db:
        cognition=await db.scalar(select(ChapterCognition));entity=await db.scalar(select(ChapterEntityExtraction))
        assert cognition.source_hash==entity.source_hash and cognition.status=='pending'
    assert await pending(r) is None

@pytest.mark.asyncio
async def test_cognition_handoff_failure_rolls_back_entity_and_ack(env,monkeypatch):
    r=env;await complete(r)
    async def fail(*args):raise RuntimeError('cognition insert failure')
    monkeypatch.setattr('app.services.cognition_extraction.enqueue_cognition',fail)
    with pytest.raises(RuntimeError):await drain_entity_source_outbox(r.sessions)
    assert await pending(r) is not None
    async with r.sessions() as db:
        assert await db.scalar(select(ChapterEntityExtraction)) is None
