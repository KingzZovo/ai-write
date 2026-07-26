"""Celery tasks for book analysis (feature extraction, quality scoring).

Split out of knowledge_tasks.py (2026-07-26); task names are unchanged.
"""

import asyncio
import json
import logging

from app.tasks import celery_app
from app.tasks.common import _make_session, _run_async

logger = logging.getLogger(__name__)

@celery_app.task(name="tasks.extract_features")
def extract_features(book_id: str):
    """Extract plot and style features from all chunks of a book."""
    _run_async(_extract_features_async(book_id))


async def _extract_features_async(book_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import TextChunk, ReferenceBook
    from app.services.feature_extractor import PlotExtractor, StyleExtractor

    plot_extractor = PlotExtractor()
    style_extractor = StyleExtractor()

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            return

        book.status = "extracting"
        await db.commit()

        result = await db.execute(
            select(TextChunk)
            .where(TextChunk.book_id == book_id)
            .order_by(TextChunk.sequence_id)
        )
        chunks = result.scalars().all()

        # Initialize Qdrant for vectorization
        qdrant_store = None
        try:
            from qdrant_client import AsyncQdrantClient
            from app.config import settings as _cfg
            from app.services.qdrant_store import QdrantStore
            from app.services.feature_extractor import generate_embedding

            _qc = AsyncQdrantClient(host=_cfg.QDRANT_HOST, port=_cfg.QDRANT_PORT)
            qdrant_store = QdrantStore(_qc)
            await qdrant_store.ensure_collections()
        except Exception as e:
            logger.warning("Qdrant not available for vectorization: %s", e)

        for chunk in chunks:
            try:
                # Style extraction (fast, no LLM)
                if not chunk.style_extracted:
                    style_features = style_extractor.extract(chunk.content)
                    chunk.style_features_json = style_features if isinstance(style_features, dict) else {"raw": str(style_features)}
                    chunk.style_extracted = 1

                # Plot extraction (LLM, slower)
                if not chunk.plot_extracted:
                    plot_features = await plot_extractor.extract(chunk.content)
                    chunk.plot_features_json = plot_features if isinstance(plot_features, dict) else {"raw": str(plot_features)}
                    chunk.plot_extracted = 1

                # Vectorize to Qdrant
                if qdrant_store:
                    try:
                        embedding = await generate_embedding(chunk.content[:500])
                        if embedding and any(v != 0 for v in embedding[:10]):
                            summary = str(chunk.plot_features_json.get("summary", "")) if chunk.plot_features_json else ""
                            await qdrant_store.store_plot_features(
                                book_id=book_id, chunk_id=str(chunk.id),
                                sequence_id=chunk.sequence_id, summary_text=summary,
                                embedding=embedding,
                            )
                            await qdrant_store.store_style_features(
                                book_id=book_id, chunk_id=str(chunk.id),
                                sequence_id=chunk.sequence_id,
                                features_dict=chunk.style_features_json or {},
                                embedding=embedding,
                            )
                    except Exception as ve:
                        logger.debug("Vectorization failed for chunk %s: %s", chunk.id, ve)

                await db.commit()
            except Exception as e:
                logger.warning("Feature extraction failed for chunk %s: %s", chunk.id, e)

        if qdrant_store:
            await _qc.close()

        book.status = "ready"
        await db.commit()
        logger.info("Feature extraction + vectorization complete for book %s", book.title)


@celery_app.task(name="tasks.run_quality_score")
def run_quality_score(book_id: str):
    """Run quality scoring on a reference book."""
    _run_async(_run_quality_score_async(book_id))


async def _run_quality_score_async(book_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import TextChunk, ReferenceBook
    from app.services.quality_scorer import QualityScorer

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            return

        result = await db.execute(
            select(TextChunk)
            .where(TextChunk.book_id == book_id)
            .order_by(TextChunk.sequence_id)
        )
        chunks = result.scalars().all()
        if not chunks:
            return

        # Sample 5 blocks evenly
        n = len(chunks)
        step = max(1, n // 5)
        samples = [chunks[i].content for i in range(0, n, step)][:5]

        scorer = QualityScorer()
        score, is_suitable = await scorer.score_and_filter(samples)

        metadata = book.metadata_json or {}
        metadata["quality_score"] = score.to_dict()
        book.metadata_json = metadata

        if not is_suitable:
            book.status = "low_quality"

        await db.commit()
        logger.info("Quality score for %s: %.1f (%s)", book.title, score.overall, score.verdict)
