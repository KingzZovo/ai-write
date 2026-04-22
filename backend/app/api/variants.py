"""BVSR variants API (v1.0 chunk 7).

Endpoints:
  GET  /api/chapters/{chapter_id}/variants  — list variants for a chapter
  GET  /api/variants/{variant_id}           — full content of one variant
  POST /api/variants/{variant_id}/select    — mark variant as user-selected winner
                                              and copy its content onto the chapter
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, ChapterVariant
from app.services.bvsr import select_variant as _select_variant

router = APIRouter(tags=["variants"])


class VariantOut(BaseModel):
    id: str
    chapter_id: str
    run_id: str | None
    variant_idx: int
    word_count: int
    score: float | None
    hard_count: int
    soft_count: int
    ai_trap_count: int
    is_winner: bool
    selected_by_user: bool
    created_at: str


class VariantDetail(VariantOut):
    content_text: str
    critic_report_json: dict


class SelectVariantRequest(BaseModel):
    apply_to_chapter: bool = True  # copy content_text onto the chapter


def _to_out(v: ChapterVariant) -> VariantOut:
    return VariantOut(
        id=str(v.id),
        chapter_id=str(v.chapter_id),
        run_id=str(v.run_id) if v.run_id else None,
        variant_idx=v.variant_idx,
        word_count=v.word_count or 0,
        score=v.score,
        hard_count=v.hard_count or 0,
        soft_count=v.soft_count or 0,
        ai_trap_count=v.ai_trap_count or 0,
        is_winner=bool(v.is_winner),
        selected_by_user=bool(v.selected_by_user),
        created_at=v.created_at.isoformat() if v.created_at else "",
    )


@router.get("/api/chapters/{chapter_id}/variants", response_model=list[VariantOut])
async def list_chapter_variants(
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[VariantOut]:
    res = await db.execute(
        select(ChapterVariant)
        .where(ChapterVariant.chapter_id == chapter_id)
        .order_by(ChapterVariant.run_id, ChapterVariant.variant_idx)
    )
    return [_to_out(v) for v in res.scalars().all()]


@router.get("/api/variants/{variant_id}", response_model=VariantDetail)
async def get_variant_detail(
    variant_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> VariantDetail:
    res = await db.execute(select(ChapterVariant).where(ChapterVariant.id == variant_id))
    v = res.scalar_one_or_none()
    if v is None:
        raise HTTPException(404, "variant not found")
    base = _to_out(v).model_dump()
    return VariantDetail(
        **base,
        content_text=v.content_text,
        critic_report_json=v.critic_report_json or {},
    )


@router.post("/api/variants/{variant_id}/select", response_model=VariantOut)
async def select_variant_endpoint(
    variant_id: UUID,
    body: SelectVariantRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> VariantOut:
    try:
        variant = await _select_variant(db, variant_id=variant_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    apply = True if body is None else body.apply_to_chapter
    if apply:
        res = await db.execute(select(Chapter).where(Chapter.id == variant.chapter_id))
        chapter = res.scalar_one_or_none()
        if chapter is not None:
            chapter.content_text = variant.content_text
            chapter.word_count = variant.word_count or len(variant.content_text)
    await db.commit()
    await db.refresh(variant)
    return _to_out(variant)
