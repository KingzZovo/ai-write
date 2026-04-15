"""
Celery tasks for knowledge base operations.

- Crawling novels via book source engine
- Text cleaning and slicing
- Feature extraction (plot + style)
- Quality scoring
- Style clustering
"""

import asyncio
import logging
from uuid import UUID

from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async function in sync Celery task context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import CrawlTask, BookSource, ReferenceBook, TextChunk
    from app.services.book_source_engine import BookSourceEngine
    from app.services.text_pipeline import _strip_noise, _detect_chapters, slice_chapters_to_blocks, ChapterData

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

        for chunk in chunks:
            try:
                # Style extraction (fast, no LLM)
                if not chunk.style_extracted:
                    style_features = style_extractor.extract(chunk.content)
                    # Store in metadata (will be vectorized later)
                    chunk.style_extracted = 1

                # Plot extraction (LLM, slower)
                if not chunk.plot_extracted:
                    plot_features = await plot_extractor.extract(chunk.content)
                    chunk.plot_extracted = 1

                await db.commit()
            except Exception as e:
                logger.warning("Feature extraction failed for chunk %s: %s", chunk.id, e)

        book.status = "ready"
        await db.commit()
        logger.info("Feature extraction complete for book %s", book.title)


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
