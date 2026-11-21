"""Celery tasks for reference-book operations (vectorize/upload/test/crawl).

Split out of knowledge_tasks.py (2026-07-26); task names are unchanged.
"""

import asyncio
import json
import logging

from app.tasks import celery_app
from app.tasks.common import _make_session, _run_async

logger = logging.getLogger(__name__)

@celery_app.task(name="tasks.vectorize_book")
def vectorize_book_task(book_id: str):
    """Vectorize all chunks of an existing book into Qdrant."""
    _run_async(_vectorize_book_async(book_id))


async def _vectorize_book_async(book_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import TextChunk, ReferenceBook
    from app.services.model_router import get_model_router_async
    from app.services.qdrant_store import QdrantStore
    from qdrant_client import AsyncQdrantClient
    from app.config import settings

    router = await get_model_router_async()
    qc = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    store = QdrantStore(qc)
    await store.ensure_collections()

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            logger.error("Book %s not found", book_id)
            return

        result = await db.execute(
            select(TextChunk)
            .where(TextChunk.book_id == book_id)
            .order_by(TextChunk.sequence_id)
        )
        chunks = list(result.scalars().all())
        logger.info("Vectorizing %d chunks for %s", len(chunks), book.title)

        vectorized = 0
        for chunk in chunks:
            try:
                embedding = await router.embed(chunk.content[:500])
                if embedding and any(v != 0 for v in embedding[:10]):
                    await store.store_style_features(
                        book_id=book_id,
                        chunk_id=str(chunk.id),
                        sequence_id=chunk.sequence_id,
                        features_dict={
                            "chapter_title": chunk.chapter_title or "",
                            "char_count": chunk.char_count,
                        },
                        embedding=embedding,
                    )
                    vectorized += 1
            except Exception as e:
                logger.debug("Chunk vectorization failed: %s", e)

            # Log progress every 100 chunks
            if vectorized > 0 and vectorized % 100 == 0:
                logger.info("  %d/%d vectorized...", vectorized, len(chunks))

        logger.info("Vectorization complete: %d/%d chunks for %s", vectorized, len(chunks), book.title)

    await qc.close()


@celery_app.task(name="tasks.process_uploaded_book")
def process_uploaded_book(book_id: str, file_path: str, filename: str, user_title: str = "", user_author: str = ""):
    """Process an uploaded book file: parse → chunk → auto-score."""
    _run_async(_process_uploaded_book_async(book_id, file_path, filename, user_title, user_author))


async def _process_uploaded_book_async(book_id: str, file_path: str, filename: str, user_title: str, user_author: str):
    import os
    from app.db.session import async_session_factory
    from app.models.project import ReferenceBook, TextChunk
    from app.services.text_pipeline import process_text_file

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            logger.error("Book %s not found", book_id)
            return

        try:
            # Stage 1: cleaning
            book.status = "cleaning"
            await db.commit()

            with open(file_path, "rb") as f:
                content = f.read()

            parse_result, blocks = process_text_file(content, filename)

            if parse_result.title and not user_title:
                book.title = parse_result.title
            if parse_result.author and not user_author:
                book.author = parse_result.author
            book.total_chapters = len(parse_result.chapters)
            book.total_words = parse_result.total_chars

            # Save text chunks
            for block in blocks:
                chunk = TextChunk(
                    book_id=book.id,
                    chapter_idx=block.chapter_idx,
                    block_idx=block.block_idx,
                    chapter_title=block.chapter_title,
                    content=block.content,
                    char_count=block.char_count,
                    sequence_id=block.sequence_id,
                )
                db.add(chunk)
            await db.commit()

            # Stage 2: vectorize chunks into Qdrant
            book.status = "extracting"
            await db.commit()

            try:
                from app.services.qdrant_store import QdrantStore
                from app.services.model_router import get_model_router_async
                from qdrant_client import AsyncQdrantClient as _QC
                from app.config import settings as _s

                # Pre-load model router (with DB config + decrypted keys)
                router = await get_model_router_async()

                qdrant_client = _QC(host=_s.QDRANT_HOST, port=_s.QDRANT_PORT)
                store = QdrantStore(qdrant_client)
                await store.ensure_collections()
                vectorized = 0
                for block in blocks:
                    try:
                        embedding = await router.embed(block.content[:500])
                        if embedding and any(v != 0 for v in embedding[:10]):
                            await store.store_style_features(
                                book_id=str(book.id),
                                chunk_id=f"{block.chapter_idx}_{block.block_idx}",
                                sequence_id=block.sequence_id,
                                features_dict={"chapter_title": block.chapter_title, "char_count": block.char_count},
                                embedding=embedding,
                            )
                            vectorized += 1
                    except Exception:
                        pass
                await qdrant_client.close()
                logger.info("Vectorized %d/%d chunks for %s", vectorized, len(blocks), book.title)
            except Exception as e:
                logger.warning("Vectorization skipped for %s: %s", book.title, e)

            # Stage 3: auto quality scoring (if model configured)
            try:
                from app.services.model_router import get_model_router_async as _gmra
                await _gmra()  # Ensure router loaded before QualityScorer uses sync version
                from app.services.quality_scorer import QualityScorer
                scorer = QualityScorer()

                sample_blocks = blocks[:5] if len(blocks) <= 5 else [blocks[i] for i in range(0, len(blocks), max(1, len(blocks) // 5))][:5]
                samples = [b.content for b in sample_blocks]

                score, is_suitable = await scorer.score_and_filter(samples)
                metadata = book.metadata_json or {}
                metadata["quality_score"] = score.to_dict()
                book.metadata_json = metadata
                if not is_suitable:
                    book.status = "low_quality"
                else:
                    book.status = "ready"
            except Exception as e:
                logger.warning("Auto-scoring skipped for %s: %s", book.title, e)
                book.status = "ready"

            await db.commit()
            # PR-BOOK-PROFILE-BIND: ensure every reference_book has a bound StyleProfile
            try:
                from app.services.style_profile_resolver import get_or_create_book_profile
                await get_or_create_book_profile(db, str(book.id))
            except Exception as e:
                logger.warning("auto profile binding failed for %s: %s", book.title, e)
            logger.info("Book processed: %s — %d chapters, %d chars", book.title, book.total_chapters, book.total_words)

        except Exception as e:
            book.status = "error"
            book.error_message = str(e)[:500]
            await db.commit()
            logger.exception("Failed to process book %s", book_id)
        finally:
            # Clean up temp file
            try:
                os.unlink(file_path)
            except OSError:
                pass


@celery_app.task(name="tasks.consolidate_reference_book")
def consolidate_reference_book(book_id: str):
    """Build the per-book dossier (style/plot/world consolidation)."""
    from app.services.book_dossier import build_dossier

    _run_async(build_dossier(book_id))


@celery_app.task(name="tasks.consolidate_author")
def consolidate_author_task(author: str):
    """Build the author-level dossier (merges the author's book dossiers)."""
    from app.services.book_dossier import consolidate_author

    _run_async(consolidate_author(author))


@celery_app.task(name="tasks.batch_test_sources")
def batch_test_sources_task(source_ids: list[str]):
    """Batch test book sources for connectivity in background."""
    _run_async(_batch_test_async(source_ids))


async def _batch_test_async(source_ids: list[str]):
    import httpx
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import BookSource

    client = httpx.AsyncClient(
        timeout=6,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/107.0 Mobile Safari/537.36"},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )

    ok_count = 0
    fail_count = 0

    async with async_session_factory() as db:
        result = await db.execute(
            select(BookSource).where(BookSource.id.in_(source_ids))
        )
        sources = list(result.scalars().all())

        async def test_one(source: BookSource):
            sj = source.source_json or {}
            base = sj.get("bookSourceUrl", "")
            if not base:
                return False
            try:
                resp = await client.get(base)
                reachable = resp.status_code < 500
                source.last_test_ok = 1 if reachable else 0
                source.last_test_at = datetime.now(timezone.utc)
                if reachable:
                    source.success_count = (source.success_count or 0) + 1
                    source.consecutive_fails = 0
                    source.score = min(10.0, (source.score or 5.0) + 0.3)
                else:
                    source.fail_count = (source.fail_count or 0) + 1
                    source.consecutive_fails = (source.consecutive_fails or 0) + 1
                    source.score = max(0.0, (source.score or 5.0) - 0.5)
                return reachable
            except Exception:
                source.last_test_ok = 0
                source.last_test_at = datetime.now(timezone.utc)
                source.fail_count = (source.fail_count or 0) + 1
                source.consecutive_fails = (source.consecutive_fails or 0) + 1
                source.score = max(0.0, (source.score or 5.0) - 1.0)
                return False

        batch_size = 30
        for i in range(0, len(sources), batch_size):
            batch = sources[i:i + batch_size]
            results = await asyncio.gather(*[test_one(s) for s in batch], return_exceptions=True)
            for r in results:
                if r is True:
                    ok_count += 1
                else:
                    fail_count += 1
            await db.commit()
            logger.info("Batch test progress: %d/%d (ok=%d, fail=%d)", i + len(batch), len(sources), ok_count, fail_count)

    await client.aclose()
    logger.info("Batch test complete: %d total, %d ok, %d failed", len(sources), ok_count, fail_count)


@celery_app.task(bind=True, name="tasks.crawl_book", max_retries=3)
def crawl_book(self, task_id: str):
    """
    Crawl a book using the book source engine.

    Steps:
    1. Load CrawlTask and BookSource from DB
    2. Parse book source rules
    3. Get table of contents
    4. Fetch each chapter with rate limiting
    5. Save raw content to ReferenceBook chapters
    6. Trigger text cleaning pipeline
    """
    _run_async(_crawl_book_async(self, task_id))


async def _crawl_book_async(task, task_id: str):
    from app.db.session import async_session_factory
    from app.models.project import CrawlTask, BookSource, ReferenceBook, TextChunk
    from app.services.book_source_engine import BookSourceEngine
    from app.services.text_pipeline import _strip_noise, slice_chapters_to_blocks, ChapterData

    engine = BookSourceEngine()

    async with async_session_factory() as db:
        crawl_task = await db.get(CrawlTask, task_id)
        if not crawl_task:
            logger.error("CrawlTask %s not found", task_id)
            return

        crawl_task.status = "running"
        await db.commit()

        book = await db.get(ReferenceBook, str(crawl_task.book_id))
        source = await db.get(BookSource, str(crawl_task.source_id)) if crawl_task.source_id else None

        if not source:
            crawl_task.status = "error"
            crawl_task.error_message = "Book source not found"
            await db.commit()
            return

        try:
            config = engine.parse_source(source.source_json)

            # Get TOC
            chapters = await engine.get_toc(config, crawl_task.book_url)
            crawl_task.total_chapters = len(chapters)
            await db.commit()

            if not chapters:
                crawl_task.status = "error"
                crawl_task.error_message = "No chapters found"
                await db.commit()
                return

            # Fetch each chapter
            all_chapter_data = []
            for idx, ch in enumerate(chapters):
                try:
                    content = await engine.get_content(config, ch.url)
                    if content:
                        content = _strip_noise(content)
                        all_chapter_data.append(ChapterData(
                            chapter_idx=idx + 1,
                            title=ch.title,
                            content=content,
                        ))

                    crawl_task.completed_chapters = idx + 1
                    if idx % 10 == 0:
                        await db.commit()

                    # Rate limiting
                    import asyncio as aio
                    await aio.sleep(1.0)

                except Exception as e:
                    logger.warning("Failed to fetch chapter %d: %s", idx, e)
                    continue

            # Slice into blocks
            blocks = slice_chapters_to_blocks(all_chapter_data)

            # Save blocks
            for block in blocks:
                chunk = TextChunk(
                    book_id=book.id,
                    chapter_idx=block.chapter_idx,
                    block_idx=block.block_idx,
                    chapter_title=block.chapter_title,
                    content=block.content,
                    char_count=block.char_count,
                    sequence_id=block.sequence_id,
                )
                db.add(chunk)

            book.total_chapters = len(all_chapter_data)
            book.total_words = sum(ch.char_count for ch in all_chapter_data)
            book.status = "ready"
            crawl_task.status = "completed"
            await db.commit()

            logger.info(
                "Crawl complete: %s — %d chapters, %d blocks",
                book.title, len(all_chapter_data), len(blocks),
            )

        except Exception as e:
            logger.exception("Crawl failed for task %s", task_id)
            crawl_task.status = "error"
            crawl_task.error_message = str(e)
            book.status = "error"
            book.error_message = str(e)
            await db.commit()

    await engine.close()


