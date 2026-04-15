"""
Celery tasks for style-related periodic processing.

- Periodic DBSCAN clustering on extracted style features
"""

import asyncio
import logging

from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async function in sync Celery task context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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
