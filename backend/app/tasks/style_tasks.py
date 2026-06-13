"""
Celery tasks for style-related periodic processing.

- Periodic DBSCAN clustering on extracted style features
- C2/F1 (v1.9.2): whole-book style-statistics recompute (ainovel stylestat)
"""

import logging

from app.tasks import celery_app

logger = logging.getLogger(__name__)

# C2/F1: public task name for the whole-book style-stats recompute. Defined
# here so the dispatch helper in app.services.style_stat can enqueue it without
# importing this module (keeps celery out of sync paths / tests).
STYLE_STATS_TASK = "style.recompute_stats"


def _run_async(coro):
    """Run async function in sync Celery task context.

    v1.7 X2: delegates to the unified _run_async_safe from app.tasks.
    """
    from app.tasks import _run_async_safe
    return _run_async_safe(coro)


@celery_app.task(name="tasks.run_style_clustering")
def run_style_clustering():
    """Periodic task: run DBSCAN clustering on all books that have extracted features."""
    _run_async(_run_style_clustering_async())


async def _run_style_clustering_async():
    from sqlalchemy import select

    from app.db.session import async_session_factory
    from app.models.project import ReferenceBook, TextChunk
    from app.services.feature_extractor import StyleExtractor
    from app.services.style_clustering import cluster_style_features

    style_extractor = StyleExtractor()

    async with async_session_factory() as db:
        # Find all books in "ready" status that have extracted features
        result = await db.execute(
            select(ReferenceBook).where(ReferenceBook.status == "ready")
        )
        books = result.scalars().all()

        if not books:
            logger.info("No ready books found for style clustering")
            return

        for book in books:
            try:
                # Get all chunks with extracted style features
                chunk_result = await db.execute(
                    select(TextChunk)
                    .where(
                        TextChunk.book_id == book.id,
                        TextChunk.style_extracted == 1,
                    )
                    .order_by(TextChunk.sequence_id)
                )
                chunks = chunk_result.scalars().all()

                if len(chunks) < 5:
                    logger.info(
                        "Book %s has too few extracted chunks (%d) for clustering",
                        book.title,
                        len(chunks),
                    )
                    continue

                # Re-extract style features for clustering
                features = []
                block_ids = []
                for chunk in chunks:
                    feat = style_extractor.extract(chunk.content)
                    features.append(feat.to_dict())
                    block_ids.append(str(chunk.id))

                # Run clustering
                profiles = cluster_style_features(
                    features, block_ids, method="dbscan"
                )

                # Store clustering results in book metadata
                metadata = book.metadata_json or {}
                metadata["style_clusters"] = [
                    {
                        "name": p.name,
                        "vocab_whitelist": p.vocab_whitelist[:10],
                        "sentence_ratio": p.sentence_ratio,
                        "dialogue_ratio": p.dialogue_ratio,
                        "pov_type": p.pov_type,
                        "sample_count": len(p.sample_block_ids),
                    }
                    for p in profiles
                ]
                book.metadata_json = metadata
                await db.commit()

                logger.info(
                    "Style clustering for book %s: %d clusters found",
                    book.title,
                    len(profiles),
                )

            except Exception as exc:
                logger.warning(
                    "Style clustering failed for book %s: %s", book.title, exc
                )
                continue


# ---------------------------------------------------------------------------
# C2 / F1: whole-book style statistics recompute (ainovel stylestat).
# ---------------------------------------------------------------------------


@celery_app.task(
    name=STYLE_STATS_TASK,
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
)
def recompute_style_stats(self, project_id: str, caller: str = "unknown") -> dict:
    """Recompute and upsert whole-book style statistics for one project.

    Deterministic (zero LLM). Pulls all written chapters + entity names, runs
    ``style_stat.compute_style_stats``, and upserts the ``style_stats`` row.
    """
    from app.tasks import _run_async_safe

    return _run_async_safe(
        _recompute_style_stats_async(str(project_id), str(caller))
    )


async def _recompute_style_stats_async(project_id: str, caller: str) -> dict:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from app.db.session import async_session_factory
    from app.models.project import (
        Chapter,
        Character,
        Location,
        Organization,
        StyleStat,
        Volume,
    )
    from app.services.foreshadow_lifecycle import get_volume_first_global_idx
    from app.services.style_stat import compute_style_stats

    async with async_session_factory() as db:
        # Load chapters with content, ordered by (volume_idx, chapter_idx), and
        # assign book-global indices via the per-volume offset.
        vol_rows = (
            await db.execute(
                select(Volume.id, Volume.volume_idx)
                .where(Volume.project_id == project_id)
                .order_by(Volume.volume_idx)
            )
        ).all()
        chapters: list[tuple[int, str]] = []
        for vol_id, vol_idx in vol_rows:
            base = await get_volume_first_global_idx(db, project_id, vol_idx)
            ch_rows = (
                await db.execute(
                    select(Chapter.chapter_idx, Chapter.content_text)
                    .where(Chapter.volume_id == vol_id)
                    .order_by(Chapter.chapter_idx)
                )
            ).all()
            for ch_idx, content in ch_rows:
                if content and content.strip():
                    chapters.append((base + max(0, int(ch_idx)), content))

        if not chapters:
            logger.info(
                "style stats: no written chapters (project=%s caller=%s)",
                project_id, caller,
            )
            return {"project_id": project_id, "chapter_count": 0, "skipped": True}

        # Entity names (PG read-only projections) used to stop-list n-grams.
        names: set[str] = set()
        for model in (Character, Location, Organization):
            rows = (
                await db.execute(
                    select(model.name).where(model.project_id == project_id)
                )
            ).all()
            names.update(n for (n,) in rows if n)

        stats = compute_style_stats(chapters, names)

        await db.execute(
            insert(StyleStat)
            .values(
                project_id=project_id,
                stats_json=stats,
                chapter_count=stats.get("chapter_count", 0),
            )
            .on_conflict_do_update(
                index_elements=["project_id"],
                set_={
                    "stats_json": stats,
                    "chapter_count": stats.get("chapter_count", 0),
                },
            )
        )
        await db.commit()

    logger.info(
        "style stats recomputed (project=%s caller=%s chapters=%d phrases=%d)",
        project_id, caller, stats.get("chapter_count", 0),
        len(stats.get("top_phrases", [])),
    )
    return {
        "project_id": project_id,
        "chapter_count": stats.get("chapter_count", 0),
    }
