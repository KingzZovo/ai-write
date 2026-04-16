"""Knowledge base management endpoints.

Handles:
- Book source (书源) CRUD and import
- Reference book management
- File upload (TXT/EPUB/HTML)
- Crawl task management
- Quality scoring
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import BookSource, ReferenceBook, TextChunk, CrawlTask

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


# =============================================================================
# Schemas
# =============================================================================

class BookSourceImport(BaseModel):
    sources_json: list[dict]  # Array of legado BookSource JSON objects


class BookSourceResponse(BaseModel):
    id: UUID
    name: str
    source_url: str
    source_group: str | None
    enabled: int
    last_test_ok: int
    score: float
    success_count: int
    fail_count: int
    consecutive_fails: int
    total_books_fetched: int

    model_config = {"from_attributes": True}


class ReferenceBookResponse(BaseModel):
    id: UUID
    title: str
    author: str | None
    source: str
    total_chapters: int
    total_words: int
    status: str
    error_message: str | None
    metadata_json: dict

    model_config = {"from_attributes": True}


class CrawlTaskCreate(BaseModel):
    source_id: str
    book_url: str
    title: str
    author: str = ""


class CrawlTaskResponse(BaseModel):
    id: UUID
    book_id: UUID
    book_url: str
    total_chapters: int
    completed_chapters: int
    status: str

    model_config = {"from_attributes": True}


class ExploreRequest(BaseModel):
    source_id: str
    page: int = 1
    category_index: int = 0


class QualityScoreResponse(BaseModel):
    book_id: str
    overall: float
    verdict: str
    is_suitable: bool
    brief_comment: str
    scores: dict


# =============================================================================
# Book Source Endpoints
# =============================================================================

@router.get("/sources")
async def list_sources(
    page: int = 1,
    page_size: int = 30,
    search: str = "",
    group: str = "",
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List book sources with pagination."""
    query = select(BookSource)
    count_query = select(func.count(BookSource.id))

    if search:
        query = query.where(BookSource.name.ilike(f"%{search}%"))
        count_query = count_query.where(BookSource.name.ilike(f"%{search}%"))
    if group:
        query = query.where(BookSource.source_group == group)
        count_query = count_query.where(BookSource.source_group == group)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(BookSource.score.desc(), BookSource.name)
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    sources = [BookSourceResponse.model_validate(s) for s in result.scalars().all()]

    # Get distinct groups for filter
    groups_result = await db.execute(
        select(BookSource.source_group).where(BookSource.source_group.isnot(None)).distinct()
    )
    groups = sorted([g for g in groups_result.scalars().all() if g])

    return {
        "sources": sources,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "groups": groups,
    }


@router.post("/sources/upload", status_code=201)
async def upload_sources_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upload a legado book source JSON file (supports large files)."""
    import json as _json
    content = await file.read()
    try:
        sources_list = _json.loads(content)
        if isinstance(sources_list, dict):
            sources_list = [sources_list]
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON 格式无效")

    imported = 0
    skipped = 0
    for source_json in sources_list:
        name = source_json.get("bookSourceName", "Unknown")
        url = source_json.get("bookSourceUrl", "")
        group = source_json.get("bookSourceGroup", "")
        if not url:
            continue
        existing = await db.execute(
            select(BookSource).where(BookSource.source_url == url)
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue
        source = BookSource(
            name=name, source_url=url, source_group=group, source_json=source_json,
        )
        db.add(source)
        imported += 1

    await db.flush()
    return {"imported": imported, "skipped": skipped, "total": len(sources_list)}


@router.post("/sources/import", status_code=201)
async def import_sources(
    body: BookSourceImport,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Import legado book source JSON(s) via request body."""
    imported = 0
    for source_json in body.sources_json:
        name = source_json.get("bookSourceName", "Unknown")
        url = source_json.get("bookSourceUrl", "")
        group = source_json.get("bookSourceGroup", "")

        if not url:
            continue

        # Check for duplicate
        existing = await db.execute(
            select(BookSource).where(BookSource.source_url == url)
        )
        if existing.scalar_one_or_none():
            continue

        source = BookSource(
            name=name,
            source_url=url,
            source_group=group,
            source_json=source_json,
        )
        db.add(source)
        imported += 1

    await db.flush()
    return {"imported": imported, "total": len(body.sources_json)}


@router.post("/sources/{source_id}/test")
async def test_source(
    source_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Test if a book source is working."""
    source = await db.get(BookSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    from app.services.book_source_engine import BookSourceEngine
    engine = BookSourceEngine()
    try:
        config = engine.parse_source(source.source_json)
        # Test by searching for a common keyword
        results = await engine.search(config, "\u6d4b\u8bd5")  # "测试"
        source.last_test_ok = 1 if results else 0
        from datetime import datetime, timezone
        source.last_test_at = datetime.now(timezone.utc)
        await db.flush()
        return {
            "source_id": source_id,
            "status": "ok" if results else "no_results",
            "result_count": len(results),
        }
    except Exception as e:
        source.last_test_ok = 0
        await db.flush()
        return {"source_id": source_id, "status": "error", "message": str(e)}
    finally:
        await engine.close()


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a book source."""
    source = await db.get(BookSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    await db.delete(source)


@router.post("/sources/{source_id}/toggle")
async def toggle_source(
    source_id: str,
    db: AsyncSession = Depends(get_db),
) -> BookSourceResponse:
    """Enable or disable a book source."""
    source = await db.get(BookSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    source.enabled = 0 if source.enabled else 1
    await db.flush()
    await db.refresh(source)
    return BookSourceResponse.model_validate(source)


@router.post("/sources/{source_id}/report-result")
async def report_source_result(
    source_id: str,
    success: bool = True,
    quality: float = 5.0,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Report a fetch result — updates health score automatically.
    Called internally after each crawl attempt."""
    source = await db.get(BookSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    if success:
        source.success_count = (source.success_count or 0) + 1
        source.consecutive_fails = 0
        # Update average quality
        total = source.success_count
        old_avg = source.avg_quality or 0.0
        source.avg_quality = ((old_avg * (total - 1)) + quality) / total
    else:
        source.fail_count = (source.fail_count or 0) + 1
        source.consecutive_fails = (source.consecutive_fails or 0) + 1
        # Auto-disable after 5 consecutive failures
        if source.consecutive_fails >= 5:
            source.enabled = 0
            logger.warning("Source '%s' auto-disabled after %d consecutive failures",
                           source.name, source.consecutive_fails)

    # Recalculate score (0-10)
    total_attempts = (source.success_count or 0) + (source.fail_count or 0)
    if total_attempts > 0:
        success_rate = (source.success_count or 0) / total_attempts
        quality_factor = (source.avg_quality or 5.0) / 10.0
        source.score = round(success_rate * 6 + quality_factor * 4, 1)  # 60% reliability + 40% quality
    source.score = max(0, min(10, source.score or 5.0))

    from datetime import datetime, timezone
    source.last_test_at = datetime.now(timezone.utc)
    source.last_test_ok = 1 if success else 0

    await db.flush()
    return {
        "source_id": source_id,
        "score": source.score,
        "enabled": source.enabled,
        "consecutive_fails": source.consecutive_fails,
        "auto_disabled": source.consecutive_fails >= 5,
    }


@router.get("/sources/ranking")
async def get_sources_ranking(
    limit: int = 30,
    db: AsyncSession = Depends(get_db),
) -> list[BookSourceResponse]:
    """Get top book sources ranked by score (best first)."""
    result = await db.execute(
        select(BookSource)
        .where(BookSource.enabled == 1)
        .order_by(BookSource.score.desc(), BookSource.success_count.desc())
        .limit(limit)
    )
    return [BookSourceResponse.model_validate(s) for s in result.scalars().all()]


@router.post("/sources/{source_id}/explore")
async def explore_source(
    source_id: str,
    body: ExploreRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Browse the explore/ranking page of a book source.
    Uses the ruleExplore rules to fetch bestseller/ranking lists.
    """
    source = await db.get(BookSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    from app.services.book_source_engine import BookSourceEngine
    engine = BookSourceEngine()
    try:
        config = engine.parse_source(source.source_json)
        categories = engine.get_explore_categories(config)
        books = await engine.explore(config, page=body.page, category_index=body.category_index)
        return {
            "source_id": source_id,
            "page": body.page,
            "category_index": body.category_index,
            "categories": [{"index": c["index"], "title": c["title"]} for c in categories],
            "books": [
                {
                    "title": b.title,
                    "author": b.author,
                    "book_url": b.book_url,
                    "intro": b.intro,
                    "kind": b.kind,
                    "word_count": b.word_count,
                    "last_chapter": b.last_chapter,
                }
                for b in books
            ],
        }
    except Exception as e:
        logger.warning("Explore failed: %s", e)
        return {"source_id": source_id, "page": body.page, "books": [], "error": str(e)}
    finally:
        await engine.close()


# =============================================================================
# Reference Book Endpoints
# =============================================================================

@router.get("/books")
async def list_books(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[ReferenceBookResponse]:
    """List all reference books."""
    query = select(ReferenceBook).order_by(ReferenceBook.created_at.desc())
    if status:
        query = query.where(ReferenceBook.status == status)
    result = await db.execute(query)
    return [ReferenceBookResponse.model_validate(b) for b in result.scalars().all()]


@router.get("/books/{book_id}")
async def get_book(
    book_id: str,
    db: AsyncSession = Depends(get_db),
) -> ReferenceBookResponse:
    """Get a reference book with details."""
    book = await db.get(ReferenceBook, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return ReferenceBookResponse.model_validate(book)


@router.delete("/books/{book_id}", status_code=204)
async def delete_book(
    book_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a reference book and all its chunks."""
    book = await db.get(ReferenceBook, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    await db.delete(book)


@router.post("/books/{book_id}/score")
async def score_book(
    book_id: str,
    db: AsyncSession = Depends(get_db),
) -> QualityScoreResponse:
    """Run quality scoring on a reference book."""
    book = await db.get(ReferenceBook, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Get sample chunks from different parts of the book
    result = await db.execute(
        select(TextChunk)
        .where(TextChunk.book_id == book_id)
        .order_by(TextChunk.sequence_id)
    )
    chunks = result.scalars().all()

    if not chunks:
        raise HTTPException(status_code=400, detail="No text chunks available for scoring")

    # Sample 5 blocks evenly distributed
    n = len(chunks)
    step = max(1, n // 5)
    samples = [chunks[i].content for i in range(0, n, step)][:5]

    from app.services.quality_scorer import QualityScorer
    scorer = QualityScorer()
    score, is_suitable = await scorer.score_and_filter(samples)

    # Update book metadata with score
    metadata = book.metadata_json or {}
    metadata["quality_score"] = score.to_dict()
    book.metadata_json = metadata

    if not is_suitable:
        book.status = "low_quality"
    await db.flush()

    return QualityScoreResponse(
        book_id=book_id,
        overall=score.overall,
        verdict=score.verdict,
        is_suitable=is_suitable,
        brief_comment=score.brief_comment,
        scores=score.to_dict(),
    )


# =============================================================================
# Upload Endpoints
# =============================================================================

@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    title: str = Form(""),
    author: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> ReferenceBookResponse:
    """Upload a TXT/EPUB/HTML file for style learning."""
    content = await file.read()
    filename = file.filename or "unknown.txt"

    # Create reference book record
    book = ReferenceBook(
        title=title or filename,
        author=author,
        source=f"upload_{filename.rsplit('.', 1)[-1].lower()}",
        source_detail=filename,
        status="cleaning",
    )
    db.add(book)
    await db.flush()
    await db.refresh(book)

    # Process text
    from app.services.text_pipeline import process_text_file
    try:
        parse_result, blocks = process_text_file(content, filename)

        if parse_result.title and not title:
            book.title = parse_result.title
        if parse_result.author and not author:
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

        book.status = "ready"
        await db.flush()
        await db.refresh(book)

    except Exception as e:
        book.status = "error"
        book.error_message = str(e)
        await db.flush()
        logger.exception("Failed to process uploaded file: %s", filename)

    return ReferenceBookResponse.model_validate(book)


# =============================================================================
# Crawl Task Endpoints
# =============================================================================

@router.get("/crawl-tasks")
async def list_crawl_tasks(
    db: AsyncSession = Depends(get_db),
) -> list[CrawlTaskResponse]:
    """List all crawl tasks."""
    result = await db.execute(
        select(CrawlTask).order_by(CrawlTask.created_at.desc())
    )
    return [CrawlTaskResponse.model_validate(t) for t in result.scalars().all()]


@router.post("/crawl-tasks", status_code=201)
async def create_crawl_task(
    body: CrawlTaskCreate,
    db: AsyncSession = Depends(get_db),
) -> CrawlTaskResponse:
    """Create a new crawl task."""
    # Create reference book
    book = ReferenceBook(
        title=body.title,
        author=body.author,
        source="crawler",
        source_detail=body.book_url,
        status="crawling",
    )
    db.add(book)
    await db.flush()

    # Create crawl task
    task = CrawlTask(
        book_id=book.id,
        source_id=body.source_id,
        book_url=body.book_url,
        status="pending",
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)

    # TODO: Queue Celery crawl task
    # from app.tasks.crawl import crawl_book
    # crawl_book.delay(str(task.id))

    return CrawlTaskResponse.model_validate(task)


@router.post("/crawl-tasks/{task_id}/pause")
async def pause_crawl_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> CrawlTaskResponse:
    """Pause a running crawl task."""
    task = await db.get(CrawlTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.status = "paused"
    await db.flush()
    await db.refresh(task)
    return CrawlTaskResponse.model_validate(task)


@router.post("/crawl-tasks/{task_id}/resume")
async def resume_crawl_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> CrawlTaskResponse:
    """Resume a paused crawl task."""
    task = await db.get(CrawlTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.status = "running"
    await db.flush()
    await db.refresh(task)
    return CrawlTaskResponse.model_validate(task)
