"""Style profile management API.

CRUD for writing style profiles, style detection from text,
test-writing with a selected style, and binding to books/chapters.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import StyleProfile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/styles", tags=["styles"])


# =========================================================================
# Schemas
# =========================================================================

class StyleProfileCreate(BaseModel):
    name: str
    description: str = ""
    rules_json: list[dict] = []
    anti_ai_rules: list[dict] = []
    tone_keywords: list[str] = []
    sample_passages: list = []


class StyleProfileUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    rules_json: list[dict] | None = None
    anti_ai_rules: list[dict] | None = None
    tone_keywords: list[str] | None = None
    sample_passages: list | None = None
    is_active: int | None = None


class StyleProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    source_book: str | None
    rules_json: list
    anti_ai_rules: list
    tone_keywords: list
    sample_passages: list
    bind_level: str
    bind_target_id: UUID | None
    is_active: int
    config_json: dict
    created_at: Any
    updated_at: Any


class BindRequest(BaseModel):
    bind_level: str  # global / book / chapter
    bind_target_id: str | None = None


class DetectRequest(BaseModel):
    text: str
    name: str = ""


class TestWriteRequest(BaseModel):
    prompt: str = "写一段200字的场景描写，一个人走在雨中的小巷里。"


class TestWriteResponse(BaseModel):
    text: str
    style_name: str


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=list[StyleProfileResponse])
async def list_styles(
    db: AsyncSession = Depends(get_db),
) -> list[StyleProfileResponse]:
    """List all style profiles."""
    result = await db.execute(
        select(StyleProfile).order_by(StyleProfile.is_active.desc(), StyleProfile.updated_at.desc())
    )
    profiles = result.scalars().all()
    return [StyleProfileResponse.model_validate(p) for p in profiles]


@router.post("", response_model=StyleProfileResponse, status_code=201)
async def create_style(
    body: StyleProfileCreate,
    db: AsyncSession = Depends(get_db),
) -> StyleProfileResponse:
    """Create a new style profile."""
    profile = StyleProfile(
        name=body.name,
        description=body.description,
        rules_json=body.rules_json,
        anti_ai_rules=body.anti_ai_rules,
        tone_keywords=body.tone_keywords,
        sample_passages=body.sample_passages,
    )
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return StyleProfileResponse.model_validate(profile)


@router.get("/{style_id}", response_model=StyleProfileResponse)
async def get_style(
    style_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> StyleProfileResponse:
    """Get a style profile by ID."""
    profile = await db.get(StyleProfile, str(style_id))
    if not profile:
        raise HTTPException(status_code=404, detail="写法不存在")
    return StyleProfileResponse.model_validate(profile)


@router.put("/{style_id}", response_model=StyleProfileResponse)
async def update_style(
    style_id: UUID,
    body: StyleProfileUpdate,
    db: AsyncSession = Depends(get_db),
) -> StyleProfileResponse:
    """Update a style profile."""
    profile = await db.get(StyleProfile, str(style_id))
    if not profile:
        raise HTTPException(status_code=404, detail="写法不存在")

    update_data = body.model_dump(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(profile, field_name, value)

    await db.flush()
    await db.refresh(profile)
    return StyleProfileResponse.model_validate(profile)


@router.delete("/{style_id}", status_code=204)
async def delete_style(
    style_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a style profile."""
    profile = await db.get(StyleProfile, str(style_id))
    if not profile:
        raise HTTPException(status_code=404, detail="写法不存在")
    await db.delete(profile)


@router.post("/{style_id}/bind", response_model=StyleProfileResponse)
async def bind_style(
    style_id: UUID,
    body: BindRequest,
    db: AsyncSession = Depends(get_db),
) -> StyleProfileResponse:
    """Bind a style profile to a target (global/book/chapter)."""
    if body.bind_level not in ("global", "book", "chapter"):
        raise HTTPException(status_code=400, detail="bind_level 必须是 global/book/chapter")

    profile = await db.get(StyleProfile, str(style_id))
    if not profile:
        raise HTTPException(status_code=404, detail="写法不存在")

    profile.bind_level = body.bind_level
    profile.bind_target_id = body.bind_target_id
    await db.flush()
    await db.refresh(profile)
    return StyleProfileResponse.model_validate(profile)


@router.post("/detect-from-book/{book_id}", response_model=StyleProfileResponse, status_code=201)
async def detect_from_book(
    book_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> StyleProfileResponse:
    """Detect writing style from a reference book in the knowledge base."""
    from app.models.project import ReferenceBook, TextChunk
    from app.services.style_detection import detect_style_features, detect_style_with_llm, features_to_rules

    book = await db.get(ReferenceBook, str(book_id))
    if not book:
        raise HTTPException(status_code=404, detail="参考书不存在")

    # Get chunks from DB
    result = await db.execute(
        select(TextChunk)
        .where(TextChunk.book_id == str(book_id))
        .order_by(TextChunk.sequence_id)
    )
    chunks = list(result.scalars().all())
    if not chunks:
        raise HTTPException(status_code=400, detail="该书尚未处理完成，没有可分析的文本")

    # Check vectorization completeness — must be done before extraction
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        from app.config import settings
        qc = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
        count_result = await qc.count(
            collection_name="styles",
            count_filter=Filter(must=[FieldCondition(key="book_id", match=MatchValue(value=str(book_id)))]),
        )
        await qc.close()
        vectorized = count_result.count
        total = len(chunks)
        if vectorized < total * 0.9:  # At least 90% vectorized
            raise HTTPException(
                status_code=400,
                detail=f"向量化未完成（{vectorized}/{total}），请先完成向量化再提取风格"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Vectorization check failed: %s", e)

    # Multi-query Qdrant sampling: use different queries to get diverse representative chunks
    sampled_texts: list[str] = []
    try:
        from qdrant_client import AsyncQdrantClient
        from app.config import settings
        from app.services.model_router import get_model_router_async

        router = await get_model_router_async()
        qc = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        book_filter = Filter(must=[FieldCondition(key="book_id", match=MatchValue(value=str(book_id)))])

        # Multiple diverse queries to capture different aspects of style
        queries = [
            "精彩的动作场面和战斗描写",
            "深刻的对话和角色互动",
            "细腻的心理活动和内心独白",
            "环境描写和氛围营造",
            "幽默搞笑的轻松段落",
        ]
        seen_ids: set[str] = set()
        for q in queries:
            query_vec = await router.embed(q)
            results = await qc.query_points(
                collection_name="styles", query=query_vec,
                query_filter=book_filter, limit=3,
            )
            if results.points:
                for point in results.points:
                    cid = point.payload.get("chunk_id", "")
                    if cid not in seen_ids:
                        seen_ids.add(cid)
        await qc.close()

        if seen_ids:
            # Get the actual chunk content from DB by chunk_id
            chunk_map = {str(c.id): c.content for c in chunks}
            for cid in seen_ids:
                if cid in chunk_map and len(sampled_texts) < 12:
                    sampled_texts.append(chunk_map[cid])
    except Exception as e:
        logger.warning("Qdrant sampling failed, falling back to even spacing: %s", e)

    # Fallback: even spacing across the book (skip first 10% which is often TOC/preface)
    if len(sampled_texts) < 5:
        n = len(chunks)
        start = max(1, n // 10)  # Skip beginning (TOC/copyright)
        step = max(1, (n - start) // 8)
        sampled_texts = [chunks[i].content for i in range(start, n, step)][:8]

    combined_text = "\n\n".join(sampled_texts[:8])

    # Statistical analysis on sampled text
    features = detect_style_features(combined_text)

    # LLM deep analysis — send a representative sample (up to 5000 chars)
    llm_analysis = await detect_style_with_llm(combined_text[:5000])

    # Merge into rules
    rules, anti_ai = features_to_rules(features, llm_analysis)

    profile = StyleProfile(
        name=f"{book.title} 风格",
        description=f"从参考书《{book.title}》({book.total_words or 0}字) 自动提取的写作风格",
        source_book=book.title,
        rules_json=rules,
        anti_ai_rules=anti_ai,
        tone_keywords=(llm_analysis.get("style_labels", []) if isinstance(llm_analysis.get("style_labels"), list) else [])
                      + features.get("style_labels", []),
        sample_passages=[s[:500] for s in sampled_texts[:3]],
        config_json={"detection_features": features, "llm_analysis": llm_analysis, "source_book_id": str(book_id)},
    )
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return StyleProfileResponse.model_validate(profile)


@router.post("/detect", response_model=StyleProfileResponse, status_code=201)
async def detect_style(
    body: DetectRequest,
    db: AsyncSession = Depends(get_db),
) -> StyleProfileResponse:
    """Detect writing style from text and create a new profile."""
    if len(body.text) < 200:
        raise HTTPException(status_code=400, detail="文本至少需要200字才能分析风格")

    from app.services.style_detection import detect_style_features, features_to_rules

    features = detect_style_features(body.text)
    rules, anti_ai = features_to_rules(features)

    profile = StyleProfile(
        name=body.name or "自动检测的写法",
        description=f"从 {len(body.text)} 字文本中自动提取的写作风格",
        source_book="text_detection",
        rules_json=rules,
        anti_ai_rules=anti_ai,
        tone_keywords=features.get("style_labels", []),
        sample_passages=[body.text[:500]],
        config_json={"detection_features": features},
    )
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return StyleProfileResponse.model_validate(profile)


@router.post("/{style_id}/test-write", response_model=TestWriteResponse)
async def test_write(
    style_id: UUID,
    body: TestWriteRequest,
    db: AsyncSession = Depends(get_db),
) -> TestWriteResponse:
    """Generate a test passage using a selected style profile."""
    profile = await db.get(StyleProfile, str(style_id))
    if not profile:
        raise HTTPException(status_code=404, detail="写法不存在")

    from app.services.style_compiler import compile_style
    from app.services.model_router import get_model_router

    style_prompt = compile_style(profile)
    router = get_model_router()

    result = await router.generate(
        task_type="generation",
        messages=[
            {"role": "system", "content": f"你是一个小说作家。请严格按照以下写法指导来写作：\n\n{style_prompt}"},
            {"role": "user", "content": body.prompt},
        ],
        max_tokens=1024,
    )

    return TestWriteResponse(text=result.text, style_name=profile.name)


@router.post("/compile-preview")
async def compile_preview(
    body: StyleProfileCreate,
) -> dict:
    """Preview the compiled prompt for a set of rules (without saving)."""
    from app.services.style_compiler import compile_style

    # Create a temporary profile object for compilation
    profile = StyleProfile(
        name=body.name or "预览",
        description=body.description,
        rules_json=body.rules_json,
        anti_ai_rules=body.anti_ai_rules,
        tone_keywords=body.tone_keywords,
        sample_passages=body.sample_passages,
    )
    compiled = compile_style(profile)
    return {"compiled_prompt": compiled, "char_count": len(compiled)}


# =========================================================================
# Plot Structure (optional — separate from writing style)
# =========================================================================


@router.post("/extract-structure/{book_id}")
async def extract_book_structure(
    book_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Extract plot structure from a reference book. Separate from style (文笔)."""
    from app.models.project import ReferenceBook, TextChunk
    from app.services.plot_structure import extract_plot_structure

    book = await db.get(ReferenceBook, str(book_id))
    if not book:
        raise HTTPException(status_code=404, detail="参考书不存在")

    result = await db.execute(
        select(TextChunk)
        .where(TextChunk.book_id == str(book_id))
        .order_by(TextChunk.sequence_id)
    )
    chunks = list(result.scalars().all())
    if not chunks:
        raise HTTPException(status_code=400, detail="该书没有可分析的文本")

    # Sample from different parts of the book
    n = len(chunks)
    samples = [chunks[i].content for i in range(0, n, max(1, n // 6))][:6]
    combined = "\n\n".join(samples)

    structure = await extract_plot_structure(combined)
    return {"book_title": book.title, "structure": structure}
