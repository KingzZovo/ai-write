"""Knowledge base management endpoints.

Handles:
- Book source (书源) CRUD and import
- Reference book management
- File upload (TXT/EPUB/HTML)
- Crawl task management
- Quality scoring
"""

import logging
import uuid
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


class SmartCrawlRequest(BaseModel):
    """Search for a book by name, find it in book sources, and start crawling."""
    keyword: str
    max_sources: int = 20


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
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        query = query.where(BookSource.name.ilike(f"%{escaped}%", escape="\\"))
        count_query = count_query.where(BookSource.name.ilike(f"%{escaped}%", escape="\\"))
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


class BatchDeleteRequest(BaseModel):
    source_ids: list[str]


class BatchTestRequest(BaseModel):
    source_ids: list[str] | None = None  # None = test all enabled
    max_sources: int = 0  # 0 = no limit


@router.post("/sources/batch-delete", status_code=200)
async def batch_delete_sources(
    body: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete multiple book sources at once."""
    if not body.source_ids:
        return {"deleted": 0}

    result = await db.execute(
        select(BookSource).where(BookSource.id.in_(body.source_ids))
    )
    sources = list(result.scalars().all())
    for s in sources:
        await db.delete(s)
    await db.flush()
    return {"deleted": len(sources)}


@router.post("/sources/batch-test")
async def batch_test_sources(
    body: BatchTestRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Start a batch test as a background task. Returns immediately with task status."""
    from app.tasks.knowledge_tasks import batch_test_sources_task

    if body.source_ids:
        result = await db.execute(
            select(BookSource).where(BookSource.id.in_(body.source_ids))
        )
    else:
        query = select(BookSource).where(BookSource.enabled == 1)
        if body.max_sources > 0:
            query = query.limit(body.max_sources)
        result = await db.execute(query)
    source_ids = [str(s.id) for s in result.scalars().all()]

    if not source_ids:
        return {"status": "done", "total": 0, "ok": 0, "failed": 0}

    # Launch Celery background task
    batch_test_sources_task.delay(source_ids)
    return {"status": "started", "total": len(source_ids), "message": f"正在后台测试 {len(source_ids)} 个书源..."}


@router.get("/sources/test-progress")
async def get_test_progress(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get current batch test progress from DB."""
    total = await db.scalar(select(func.count(BookSource.id)).where(BookSource.enabled == 1))
    tested = await db.scalar(select(func.count(BookSource.id)).where(BookSource.last_test_at.isnot(None)))
    ok = await db.scalar(select(func.count(BookSource.id)).where(BookSource.last_test_ok == 1))
    failed = await db.scalar(select(func.count(BookSource.id)).where(
        BookSource.last_test_ok == 0, BookSource.last_test_at.isnot(None)
    ))
    return {"total": total or 0, "tested": tested or 0, "ok": ok or 0, "failed": failed or 0}


@router.post("/sources/batch-disable-failed")
async def batch_disable_failed(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Disable all sources that have consecutive_fails >= 3."""
    result = await db.execute(
        select(BookSource).where(
            BookSource.enabled == 1,
            BookSource.consecutive_fails >= 3,
        )
    )
    sources = list(result.scalars().all())
    for s in sources:
        s.enabled = 0
    await db.flush()
    return {"disabled": len(sources)}


@router.post("/sources/delete-all-failed")
async def delete_all_failed(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete all sources that failed testing (last_test_ok == 0 and have been tested)."""
    result = await db.execute(
        select(BookSource).where(
            BookSource.last_test_ok == 0,
            BookSource.last_test_at.isnot(None),
        )
    )
    sources = list(result.scalars().all())
    for s in sources:
        await db.delete(s)
    await db.flush()
    return {"deleted": len(sources)}


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
    """Upload a file for style learning. Processing runs in background."""
    import os, tempfile

    content = await file.read()
    filename = file.filename or "unknown.txt"

    # Save file to temp location for background processing
    upload_dir = "/tmp/ai-write-uploads"
    os.makedirs(upload_dir, exist_ok=True)
    tmp_path = os.path.join(upload_dir, f"{uuid.uuid4().hex}_{filename}")
    with open(tmp_path, "wb") as f:
        f.write(content)

    # Create reference book record
    book = ReferenceBook(
        title=title or filename,
        author=author,
        source=f"upload_{filename.rsplit('.', 1)[-1].lower()}",
        source_detail=filename,
        status="pending",
    )
    db.add(book)
    await db.flush()
    await db.refresh(book)

    # Queue background processing
    from app.tasks.knowledge_tasks import process_uploaded_book
    process_uploaded_book.delay(str(book.id), tmp_path, filename, title, author)

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

    # Queue Celery crawl task
    from app.tasks.knowledge_tasks import crawl_book
    crawl_book.delay(str(task.id))

    return CrawlTaskResponse.model_validate(task)


@router.post("/crawl-tasks/smart", status_code=201)
async def smart_crawl(
    body: SmartCrawlRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Search for a book by name across enabled sources, then create a crawl task.

    Automatically:
    1. Searches top enabled sources for the book
    2. Picks the first match with a valid book_url
    3. Creates a crawl task for it
    """
    from app.services.book_source_engine import BookSourceEngine
    import asyncio

    if not body.keyword.strip():
        raise HTTPException(status_code=400, detail="请输入书名")

    result = await db.execute(
        select(BookSource)
        .where(BookSource.enabled == 1)
        .order_by(BookSource.score.desc())
        .limit(body.max_sources)
    )
    sources = list(result.scalars().all())
    if not sources:
        raise HTTPException(status_code=400, detail="没有可用的书源，请先导入并测试书源")

    engine = BookSourceEngine()
    found_books: list[dict] = []

    # Search concurrently across sources
    async def search_source(source: BookSource) -> list[dict]:
        config = engine.parse_source(source.source_json)
        if not config.search_url or config.search_url.strip().startswith("@js:"):
            return []
        try:
            results = await engine.search(config, body.keyword)
            return [
                {
                    "title": b.title, "author": b.author, "book_url": b.book_url,
                    "intro": b.intro, "kind": b.kind,
                    "source_id": str(source.id), "source_name": source.name,
                }
                for b in results if b.book_url
            ]
        except Exception:
            return []

    batch_size = 10
    for i in range(0, len(sources), batch_size):
        batch = sources[i:i + batch_size]
        batch_results = await asyncio.gather(*[search_source(s) for s in batch])
        for books in batch_results:
            found_books.extend(books)
        # Stop early if we found enough
        if len(found_books) >= 5:
            break

    await engine.close()

    if not found_books:
        return {"status": "not_found", "message": f"未在书源中找到 \"{body.keyword}\"，请确认书源可用", "books": []}

    # Return found books for user to choose, don't auto-crawl
    return {
        "status": "found",
        "message": f"找到 {len(found_books)} 个结果",
        "books": found_books[:20],
    }


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


# =============================================================================
# Built-in Novel Rankings (Quark / Third-party)
# =============================================================================

RANKING_SOURCES = {
    "quark_male_hot": {
        "name": "夸克热搜·男频",
        "url": "https://vt.quark.cn/blm/novel-rank-627/index?format=html&schema=v2&gender=male&cate=%E5%85%A8%E9%83%A8&rank=rank_hot",
    },
    "quark_female_hot": {
        "name": "夸克热搜·女频",
        "url": "https://vt.quark.cn/blm/novel-rank-627/index?format=html&schema=v2&gender=female&cate=%E5%85%A8%E9%83%A8&rank=rank_hot",
    },
    "quark_male_good": {
        "name": "夸克好评·男频",
        "url": "https://vt.quark.cn/blm/novel-rank-627/index?format=html&schema=v2&gender=male&cate=%E5%85%A8%E9%83%A8&rank=rank_good",
    },
    "quark_female_good": {
        "name": "夸克好评·女频",
        "url": "https://vt.quark.cn/blm/novel-rank-627/index?format=html&schema=v2&gender=female&cate=%E5%85%A8%E9%83%A8&rank=rank_good",
    },
}

QUARK_CATEGORIES = ["全部", "都市", "玄幻", "仙侠", "历史", "科幻", "灵异悬疑", "军事"]


@router.get("/rankings")
async def list_rankings() -> dict:
    """List available built-in ranking sources."""
    return {
        "sources": [
            {"key": k, "name": v["name"]}
            for k, v in RANKING_SOURCES.items()
        ],
        "categories": QUARK_CATEGORIES,
    }


class RankingRequest(BaseModel):
    source_key: str = "quark_male_hot"
    category: str = "全部"


@router.post("/rankings/fetch")
async def fetch_ranking(body: RankingRequest) -> dict:
    """Fetch novels from a built-in ranking source."""
    import httpx
    from bs4 import BeautifulSoup

    source = RANKING_SOURCES.get(body.source_key)
    if not source:
        raise HTTPException(status_code=400, detail="Unknown ranking source")

    url = source["url"]
    if body.category != "全部":
        from urllib.parse import quote
        url = url.replace("cate=%E5%85%A8%E9%83%A8", f"cate={quote(body.category)}")

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/146.0 Mobile Safari/537.36",
            })
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "lxml")

        # Parse quark ranking page structure
        books: list[dict] = []

        # Try common novel card patterns
        for card in soup.select(".novel-item, .book-item, .rank-item, [class*=novel], [class*=book-card]"):
            title_el = card.select_one(".title, .name, h3, h2, [class*=title]")
            author_el = card.select_one(".author, [class*=author]")
            desc_el = card.select_one(".desc, .intro, [class*=desc], [class*=intro]")

            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            books.append({
                "title": title,
                "author": author_el.get_text(strip=True) if author_el else "",
                "intro": desc_el.get_text(strip=True)[:200] if desc_el else "",
                "kind": "",
                "word_count": "",
            })

        # Fallback: parse text content if no structured cards found
        if not books:
            text = soup.get_text(separator="\n")
            import re
            # Quark ranking has pattern: title\ncount\nscore\nauthor\ntags\ndesc
            lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 1]

            # Find book entries by looking for numeric patterns (view counts)
            i = 0
            while i < len(lines) - 2:
                line = lines[i]
                # Skip navigation/header text
                if any(skip in line for skip in ["大家都在搜", "小说热搜", "好评榜", "男频", "女频"]):
                    i += 1
                    continue
                if any(cat == line for cat in QUARK_CATEGORIES):
                    i += 1
                    continue

                # Check if next line looks like a view count (e.g., "11,905,133")
                if i + 1 < len(lines) and re.match(r"[\d,]+$", lines[i + 1].replace(",", "")):
                    book: dict = {"title": line, "author": "", "intro": "", "kind": "", "word_count": lines[i + 1]}

                    # Score might be next
                    j = i + 2
                    if j < len(lines) and re.match(r"\d+\.\d+$", lines[j]):
                        j += 1  # skip score

                    # Author
                    if j < len(lines) and not re.match(r"[\d,]+$", lines[j].replace(",", "")):
                        book["author"] = lines[j]
                        j += 1

                    # Tags/kind
                    if j < len(lines) and len(lines[j]) < 50:
                        book["kind"] = lines[j]
                        j += 1

                    # Intro
                    if j < len(lines) and len(lines[j]) > 20:
                        book["intro"] = lines[j][:200]
                        j += 1

                    books.append(book)
                    i = j
                else:
                    i += 1

        return {
            "source": source["name"],
            "category": body.category,
            "books": books[:50],
            "total": len(books),
        }

    except Exception as e:
        logger.warning("Ranking fetch failed: %s", e)
        return {"source": source["name"], "category": body.category, "books": [], "error": str(e)}


# =============================================================================
# Book Search (search across book sources)
# =============================================================================

class SearchRequest(BaseModel):
    keyword: str
    source_id: str | None = None  # If None, search top sources


@router.post("/search")
async def search_books(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Search for books across book sources."""
    from app.services.book_source_engine import BookSourceEngine

    if not body.keyword.strip():
        raise HTTPException(status_code=400, detail="请输入搜索关键词")

    engine = BookSourceEngine()
    all_books: list[dict] = []

    try:
        if body.source_id:
            # Search specific source
            source = await db.get(BookSource, body.source_id)
            if not source:
                raise HTTPException(status_code=404, detail="书源不存在")
            config = engine.parse_source(source.source_json)
            results = await engine.search(config, body.keyword)
            for b in results:
                all_books.append({
                    "title": b.title, "author": b.author, "book_url": b.book_url,
                    "intro": b.intro, "kind": b.kind, "source_name": source.name,
                    "source_id": str(source.id),
                })
        else:
            # Search top 5 enabled sources by score
            result = await db.execute(
                select(BookSource)
                .where(BookSource.enabled == 1)
                .order_by(BookSource.score.desc())
                .limit(5)
            )
            sources = result.scalars().all()

            for source in sources:
                try:
                    config = engine.parse_source(source.source_json)
                    results = await engine.search(config, body.keyword)
                    for b in results[:10]:
                        all_books.append({
                            "title": b.title, "author": b.author, "book_url": b.book_url,
                            "intro": b.intro, "kind": b.kind, "source_name": source.name,
                            "source_id": str(source.id),
                        })
                except Exception as e:
                    logger.warning("Search failed for source %s: %s", source.name, e)

    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Search error: %s", e)
    finally:
        await engine.close()

    return {"keyword": body.keyword, "books": all_books[:50], "total": len(all_books)}
