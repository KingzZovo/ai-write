"""Explicit saved cognition decisions for planning fixtures, never auto-filled."""
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

from sqlalchemy import insert

from app.models.cognition import ChapterCognition as Record
from app.services.entity_sources import digest, resolve_entity_source


def cognition_values(source, changes, *, identity=None):
    return dict(id=identity or uuid4(), project_id=UUID(source.project_id), chapter_id=UUID(source.chapter_id),
        source_hash=source.source_hash, source_json=source.receipt, status='completed', source_mode='auto',
        changes_json=changes, payload_hash=digest([source.source_hash, changes]))


async def seed_cognition(r, chapter_id, changes):
    async with r.sessions() as db:
        source = await resolve_entity_source(db, r.pid, 0, chapter_id)
    values = cognition_values(source, changes)
    with r.engine.begin() as db: db.execute(insert(Record).values(**values))
    return NS(source=source, record_id=values['id'], changes=changes)
