"""v0.7 — Memory compactor for chapter_summaries.

- Triggered manually via API, auto-fired after chapter saves when the
  project's live point count exceeds MEMORY_COMPACT_THRESHOLD_POINTS
  (see chapter_summarizer), or scheduled via Celery beat.
- Takes the oldest 80% of summary points, groups them in batches of 5,
  calls task_type="compact" to produce a second-level summary, stores the
  result into the project's compacted shard
  (``chapter_summaries_compacted__<project-hex>``), and marks the source
  points compacted=true so live recall excludes them. Recall then merges
  both tiers (qdrant_store.search_project_summaries), so compaction
  compresses memory without dropping it (W10-compound fix).

Sharding: the live tier is read through qdrant_store.resolve_summary_read
(per-project shard preferred, legacy global + project filter fallback);
compacted output always goes to the project's shard.
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import qdrant_store as qs
from app.services.feature_extractor import generate_embedding
from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)

COMPACT_BATCH_SIZE = 5
KEEP_RECENT_RATIO = 0.20
MIN_TOTAL_POINTS = 100


async def compact_project_memory(
    *,
    project_id: str,
    db: AsyncSession,
    force: bool = False,
) -> dict[str, Any]:
    """Compact old chapter_summaries for one project.

    Returns a summary dict with counts.
    """
    client = AsyncQdrantClient(
        host=getattr(settings, "QDRANT_HOST", "localhost"),
        port=getattr(settings, "QDRANT_PORT", 6333),
    )
    try:
        # Resolve the live tier through the shard resolver (per-project
        # collection preferred; legacy global + project filter fallback).
        source_collection, needs_filter = await qs.resolve_summary_read(
            client, project_id
        )
        must = (
            [
                qmodels.FieldCondition(
                    key="project_id",
                    match=qmodels.MatchValue(value=project_id),
                )
            ]
            if needs_filter
            else None
        )

        # Collect project summaries that are not yet compacted
        try:
            scroll_res, _offset = await client.scroll(
                collection_name=source_collection,
                scroll_filter=qmodels.Filter(
                    must=must,
                    must_not=[
                        qmodels.FieldCondition(
                            key="compacted",
                            match=qmodels.MatchValue(value=True),
                        )
                    ],
                ),
                limit=10000,
                with_payload=True,
            )
        except Exception as exc:
            logger.info(
                "%s scroll failed (collection may be empty): %s",
                source_collection, exc,
            )
            return {"status": "skipped", "reason": "no_collection"}

        points = list(scroll_res)
        total = len(points)
        if not force and total < MIN_TOTAL_POINTS:
            return {"status": "skipped", "reason": "below_threshold", "total": total}

        # Order by chapter_idx ascending so the oldest 80% get compacted
        points.sort(key=lambda p: (p.payload or {}).get("chapter_idx", 0))
        keep_recent = max(1, int(total * KEEP_RECENT_RATIO))
        to_compact = points[:-keep_recent] if keep_recent < total else []
        if not to_compact:
            return {"status": "skipped", "reason": "nothing_to_compact", "total": total}

        compacted = 0
        compacted_collection: str | None = None
        for i in range(0, len(to_compact), COMPACT_BATCH_SIZE):
            batch = to_compact[i : i + COMPACT_BATCH_SIZE]
            texts = []
            for p in batch:
                payload = p.payload or {}
                texts.append(
                    f"[CH-{payload.get('chapter_idx','?')}] {payload.get('chapter_title','')}: "
                    f"{payload.get('summary','')}"
                )
            joined = "\n".join(texts)
            try:
                result = await run_structured_prompt(
                    "compact",
                    f"<原始摘要组>\n{joined}\n</原始摘要组>\n\n请输出合并后的二级摘要。",
                    db,
                    project_id=project_id,
                )
            except Exception as exc:
                logger.warning("compact prompt failed for batch %d: %s", i, exc)
                continue

            summary_text = (
                result.get("summary")
                if isinstance(result, dict)
                else str(result or "")
            )
            if not summary_text:
                continue

            embedding = await generate_embedding(summary_text)
            if not embedding:
                continue

            # Lazily ensure the project's compacted shard with the actual
            # embedding dimension (dimension-agnostic; live model is 2048).
            if compacted_collection is None:
                compacted_collection = await qs.ensure_summary_shard(
                    client, project_id, len(embedding), compacted=True
                )

            # Upsert compacted point
            new_id = abs(hash(f"{project_id}:{i}:{summary_text[:64]}")) & 0x7FFFFFFFFFFFFFFF
            await client.upsert(
                collection_name=compacted_collection,
                points=[
                    qmodels.PointStruct(
                        id=new_id,
                        vector=embedding,
                        payload={
                            "project_id": project_id,
                            "summary": summary_text,
                            "source_count": len(batch),
                            "source_chapter_range": [
                                (batch[0].payload or {}).get("chapter_idx"),
                                (batch[-1].payload or {}).get("chapter_idx"),
                            ],
                        },
                    )
                ],
            )
            # Mark sources compacted (in whichever collection they came from)
            try:
                await client.set_payload(
                    collection_name=source_collection,
                    payload={"compacted": True},
                    points=[p.id for p in batch],
                )
            except Exception as exc:
                logger.warning("set_payload compacted=true failed: %s", exc)

            compacted += len(batch)

        return {
            "status": "ok",
            "total": total,
            "kept_recent": keep_recent,
            "compacted": compacted,
        }
    finally:
        await client.close()
