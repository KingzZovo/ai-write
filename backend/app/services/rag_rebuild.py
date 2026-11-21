"""Rebuild chapter_summaries for a project — Celery task + async implementation."""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.project import Chapter, Volume
from app.services.chapter_summarizer import summarize_and_save_chapter, upsert_chapter_summary_embedding

logger = logging.getLogger(__name__)


async def redis_set_progress(project_id: str, payload: dict) -> None:
    """Write progress JSON into Redis key rag_rebuild:{project_id}."""
    try:
        from app.config import settings
        import redis.asyncio as aioredis
        client = aioredis.from_url(settings.REDIS_URL)
        try:
            await client.set(
                f"rag_rebuild:{project_id}",
                json.dumps(payload, ensure_ascii=False),
                ex=3600,
            )
        finally:
            await client.close()
    except Exception as e:
        logger.warning("redis_set_progress failed: %s", e)


async def rebuild_rag_for_project_async(
    *,
    project_id: str,
    db: AsyncSession | None = None,
    force: bool = False,
) -> dict:
    """Scan all chapters, (re)generate summaries and embeddings.

    Existing summaries are re-indexed unless force=True. A supplied session
    must have no pending changes; this job owns its per-chapter transactions.
    Returns {done, total, failed, status}.
    """
    owns = db is None
    if db is None:
        db = async_session_factory()

    try:
        if db.new or db.dirty or db.deleted:
            raise ValueError("RAG rebuild requires a session without pending author changes")
        # Read IDs only: a long novel must not load every full chapter into RAM.
        chapter_ids = list((await db.scalars(
            select(Chapter.id)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == project_id,
                   Chapter.content_text.isnot(None), Chapter.content_text != "")
            .order_by(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
        )).all())
        total = len(chapter_ids)
        done = 0
        failed: list[str] = []
        for chapter_id in chapter_ids:
            try:
                # Reload each chapter just before processing; never reuse the
                # initial book-wide identity snapshot across model calls.
                db.expire_all()
                row = (await db.execute(
                    select(Chapter, Volume.volume_idx)
                    .join(Volume, Chapter.volume_id == Volume.id)
                    .where(Chapter.id == chapter_id, Volume.project_id == project_id)
                )).first()
                if row is None:
                    raise ValueError("chapter_removed_or_moved")
                ch, vol_idx = row
                _, summary = await summarize_and_save_chapter(
                    chapter_id=chapter_id, db=db, overwrite=force, project_vectors=False)
                if not summary:
                    raise ValueError("summary_missing_or_source_changed")
                # The shared writer has committed new summaries before any
                # external IO. Existing summaries (force=False) are re-indexed.
                projected = await upsert_chapter_summary_embedding(
                    project_id=project_id, volume_id=ch.volume_id,
                    chapter_idx=ch.chapter_idx, chapter_title=ch.title or "",
                    summary=summary, global_idx=ch.global_idx, volume_idx=vol_idx,
                    chapter_id=ch.id, source_hash=ch.summary_source_hash,
                )
                if not projected:
                    raise RuntimeError("summary_vector_write_failed")
                done += 1
                if done % 5 == 0:
                    await redis_set_progress(str(project_id), {
                        "done": done, "total": total,
                        "current_chapter": ch.chapter_idx, "status": "running",
                    })
            except Exception as exc:
                await db.rollback()
                logger.warning("rebuild failed for chapter %s: %s", chapter_id, type(exc).__name__)
                failed.append(str(chapter_id))
        result_payload = {"done": done, "total": total, "failed": failed,
                          "status": "completed" if not failed else "partial"}
        await redis_set_progress(str(project_id), result_payload)
        return result_payload
    finally:
        if owns:
            await db.close()
