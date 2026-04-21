"""Rebuild chapter_summaries for a project — Celery task + async implementation."""

from __future__ import annotations

import hashlib
import json
import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import async_session_factory
from app.models.project import Chapter, Volume
from app.services.feature_extractor import generate_embedding
from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)

CHAPTER_SUMMARY_COLLECTION = "chapter_summaries"


async def redis_set_progress(project_id: str, payload: dict) -> None:
    """Write progress JSON into Redis key rag_rebuild:{project_id}."""
    try:
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

    Returns {done, total, failed, status}.
    """
    owns = db is None
    if db is None:
        db = async_session_factory()

    try:
        result = await db.execute(
            select(Chapter)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(
                Volume.project_id == project_id,
                Chapter.content_text.isnot(None),
                Chapter.content_text != "",
            )
            .order_by(Chapter.chapter_idx.asc())
        )
        chapters = list(result.scalars().all())

        qdrant = AsyncQdrantClient(
            host=getattr(settings, "QDRANT_HOST", "localhost"),
            port=getattr(settings, "QDRANT_PORT", 6333),
        )

        try:
            await qdrant.get_collection(CHAPTER_SUMMARY_COLLECTION)
        except Exception:
            try:
                await qdrant.create_collection(
                    collection_name=CHAPTER_SUMMARY_COLLECTION,
                    vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
                )
            except Exception:
                pass

        total = len(chapters)
        done = 0
        failed: list[str] = []

        for ch in chapters:
            try:
                data = await run_structured_prompt(
                    "summary",
                    ch.content_text,
                    db=db,
                    project_id=str(project_id),
                    chapter_id=str(ch.id),
                )
                summary = data.get("summary") or (ch.content_text or "")[:200]

                ch.summary = summary
                await db.flush()

                vec = await generate_embedding(summary)
                key = f"{ch.volume_id}_{ch.chapter_idx}"
                point_id = int(hashlib.md5(key.encode()).hexdigest()[:16], 16)
                await qdrant.upsert(
                    collection_name=CHAPTER_SUMMARY_COLLECTION,
                    points=[
                        PointStruct(
                            id=point_id,
                            vector=vec,
                            payload={
                                "project_id": str(project_id),
                                "volume_id": str(ch.volume_id),
                                "chapter_idx": ch.chapter_idx,
                                "chapter_title": ch.title or "",
                                "summary": summary,
                            },
                        )
                    ],
                )
                done += 1
                if done % 5 == 0:
                    await redis_set_progress(
                        str(project_id),
                        {
                            "done": done,
                            "total": total,
                            "current_chapter": ch.chapter_idx,
                            "status": "running",
                        },
                    )
            except Exception as e:
                logger.warning("rebuild failed for chapter %s: %s", ch.id, e)
                failed.append(str(ch.id))

        try:
            await qdrant.close()
        except Exception:
            pass

        result_payload = {
            "done": done,
            "total": total,
            "failed": failed,
            "status": "completed" if not failed else "partial",
        }
        await redis_set_progress(str(project_id), result_payload)
        return result_payload
    finally:
        if owns:
            await db.close()
