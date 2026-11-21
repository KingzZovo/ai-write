"""BVSR variants API (v1.0 chunk 7).

Endpoints:
  GET  /api/chapters/{chapter_id}/variants  — list variants for a chapter
  GET  /api/variants/{variant_id}           — full content of one variant
  POST /api/variants/{variant_id}/select    — mark variant as user-selected winner
                                              and copy its content onto the chapter
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import ChapterVariant
from app.services.bvsr import select_variant as _select_variant
from app.services.chapter_content import ChapterContentError
from app.services.chapter_order import ChapterOrderConflict, ChapterOrderUnavailable
from app.services.variant_application import (
    VariantApplicationPrecondition, VariantApplicationUnavailable, prepare_variant_application,
)
from app.services.review_contract import COMPLETE, critic_review_status

router = APIRouter(tags=["variants"])
logger = logging.getLogger(__name__)


class VariantOut(BaseModel):
    id: str
    chapter_id: str
    run_id: str | None
    variant_idx: int
    word_count: int
    score: float | None
    review_status: str
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
    expected_content_text: str | None = None
    force: bool = False


class VariantSelectionOut(VariantOut):
    applied_to_chapter: bool
    backup_version_id: str | None = None


def _to_out(v: ChapterVariant) -> VariantOut:
    review_status = critic_review_status(v.critic_report_json, v.content_text)
    return VariantOut(
        id=str(v.id),
        chapter_id=str(v.chapter_id),
        run_id=str(v.run_id) if v.run_id else None,
        variant_idx=v.variant_idx,
        word_count=v.word_count or 0,
        score=v.score if review_status == COMPLETE else None,
        review_status=review_status,
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


@router.post("/api/variants/{variant_id}/select", response_model=VariantSelectionOut)
async def select_variant_endpoint(
    variant_id: UUID,
    body: SelectVariantRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> VariantSelectionOut:
    request = body or SelectVariantRequest()
    applied = None
    try:
        async with asyncio.timeout(30):
            if request.apply_to_chapter:
                applied = await prepare_variant_application(
                    db, variant_id, expected_content_text=request.expected_content_text, force=request.force,
                )
                variant = applied.variant
            else:
                try:
                    variant = await _select_variant(db, variant_id=variant_id)
                except ValueError:
                    raise VariantApplicationUnavailable('候选已不存在。') from None
            # Build the response before commit; no expired ORM reads are
            # needed after acknowledgement. It is returned only on success.
            response = VariantSelectionOut(
                **_to_out(variant).model_dump(), applied_to_chapter=request.apply_to_chapter,
                backup_version_id=str(applied.backup_version_id) if applied and applied.backup_version_id else None,
            )
            await db.commit()
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except VariantApplicationPrecondition as exc:
        await db.rollback()
        raise HTTPException(428, detail={'code': 'variant_content_precondition_required', 'message': str(exc)}) from None
    except ChapterContentError as exc:
        await db.rollback()
        raise HTTPException(409 if exc.code == 'chapter_edit_conflict' else 400, detail=exc.detail) from None
    except (VariantApplicationUnavailable, ChapterOrderUnavailable) as exc:
        await db.rollback()
        raise HTTPException(404, detail={'code': 'variant_unavailable', 'message': str(exc)}) from None
    except ChapterOrderConflict:
        await db.rollback()
        raise HTTPException(409, detail={'code': 'variant_application_conflict',
            'message': '本书正在生成、调整或同步，请稍后重新核对并应用候选。'}) from None
    except Exception as exc:
        await db.rollback()
        logger.warning('Variant selection not confirmed (%s)', type(exc).__name__)
        raise HTTPException(503, detail={'code': 'variant_selection_unconfirmed',
            'message': '尚未取得保存确认，请先刷新正文和候选状态核对，再决定下一步。'}) from None

    if applied and applied.changed:
        # The transactional extraction outbox remains the durable recovery
        # path; this notification only reduces latency after a known commit.
        from app.services.entity_dispatch import dispatch_entity_extraction
        dispatch_entity_extraction(project_id=applied.project_id, chapter_id=applied.chapter_id,
                                   chapter_idx=applied.chapter_idx, caller='api.variants.select_variant')
    return response
