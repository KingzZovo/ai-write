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


@celery_app.task(name="tasks.run_pipeline_generation")
def run_pipeline_generation(pipeline_id: str):
    """Execute pipeline generation: iterate chapters, generate, review."""
    _run_async(_run_pipeline_async(pipeline_id))


async def _run_pipeline_async(pipeline_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.pipeline import PipelineRun, PipelineChapterStatus
    from app.models.project import Chapter
    from app.services.model_router import get_model_router_async
    from datetime import datetime, timezone
    import asyncio as aio

    async with async_session_factory() as db:
        pipeline = await db.get(PipelineRun, pipeline_id)
        if not pipeline or pipeline.state not in ("generating", "planning"):
            return

        if pipeline.state == "planning":
            pipeline.state = "generating"
            pipeline.started_at = datetime.now(timezone.utc)
            await db.commit()

        router = await get_model_router_async()

        # Get pending chapters
        result = await db.execute(
            select(PipelineChapterStatus)
            .where(
                PipelineChapterStatus.pipeline_id == pipeline.id,
                PipelineChapterStatus.state == "pending",
            )
            .order_by(PipelineChapterStatus.chapter_idx)
        )
        pending = list(result.scalars().all())

        for cs in pending:
            if pipeline.state == "paused":
                break

            chapter = await db.get(Chapter, str(cs.chapter_id))
            if not chapter:
                cs.state = "failed"
                cs.error_message = "章节不存在"
                await db.commit()
                continue

            cs.state = "generating"
            cs.started_at = datetime.now(timezone.utc)
            pipeline.current_chapter_idx = cs.chapter_idx
            await db.commit()

            try:
                gen_result = await router.generate(
                    task_type="generation",
                    messages=[
                        {"role": "system", "content": "你是一位专业的小说内容生成引擎。根据章节标题和大纲生成正文。每章至少3000字。"},
                        {"role": "user", "content": f"章节标题：{chapter.title}\n大纲：{chapter.outline_json or '无'}"},
                    ],
                    max_tokens=8192,
                )

                chapter.content_text = gen_result.text
                chapter.word_count = len(gen_result.text)
                chapter.status = "completed"
                cs.state = "completed"
                cs.word_count = len(gen_result.text)
                cs.completed_at = datetime.now(timezone.utc)
                pipeline.completed_chapters = (pipeline.completed_chapters or 0) + 1

            except Exception as e:
                cs.state = "failed"
                cs.error_message = str(e)[:200]
                logger.warning("Pipeline chapter %d failed: %s", cs.chapter_idx, e)

            await db.commit()
            await aio.sleep(1)  # Rate limit

        # Advance pipeline state
        from app.services.pipeline_service import advance_pipeline
        await advance_pipeline(db, pipeline_id)
        await db.commit()

        logger.info("Pipeline %s state: %s (%d/%d chapters)",
                     pipeline_id, pipeline.state, pipeline.completed_chapters, pipeline.total_chapters)


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
                from app.services.feature_extractor import generate_embedding
                from qdrant_client import AsyncQdrantClient as _QC
                from app.config import settings as _s

                qdrant_client = _QC(host=_s.QDRANT_HOST, port=_s.QDRANT_PORT)
                if qdrant_client:
                    store = QdrantStore(qdrant_client)
                    await store.ensure_collections()
                    vectorized = 0
                    for block in blocks:
                        try:
                            embedding = await generate_embedding(block.content[:500])
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
                            pass  # Skip individual chunk failures
                    logger.info("Vectorized %d/%d chunks for %s", vectorized, len(blocks), book.title)
            except Exception as e:
                logger.warning("Vectorization skipped for %s: %s", book.title, e)

            # Stage 3: auto quality scoring (if model configured)
            try:
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
