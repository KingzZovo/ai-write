"""/api/call-logs — read and filter persisted LLM call logs."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import verify_token
from app.db.session import get_db
from app.models.call_log import LLMCallLog

router = APIRouter(
    prefix="/api/call-logs",
    tags=["call-logs"],
)


@router.get("")
async def list_logs(
    db: AsyncSession = Depends(get_db),
    project_id: str | None = None,
    chapter_id: str | None = None,
    task_type: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = (
        select(LLMCallLog)
        .order_by(desc(LLMCallLog.created_at))
        .limit(limit)
        .offset(offset)
    )
    if project_id:
        stmt = stmt.where(LLMCallLog.project_id == project_id)
    if chapter_id:
        stmt = stmt.where(LLMCallLog.chapter_id == chapter_id)
    if task_type:
        stmt = stmt.where(LLMCallLog.task_type == task_type)
    if status:
        stmt = stmt.where(LLMCallLog.status == status)

    result = await db.execute(stmt)
    logs = result.scalars().all()
    return {
        "logs": [
            {
                "id": str(log.id),
                "task_type": log.task_type,
                "project_id": str(log.project_id) if log.project_id else None,
                "chapter_id": str(log.chapter_id) if log.chapter_id else None,
                "model": log.model,
                "input_tokens": log.input_tokens,
                "output_tokens": log.output_tokens,
                "latency_ms": log.latency_ms,
                "status": log.status,
                "created_at": log.created_at.isoformat() if log.created_at else None,
                "rag_hits_count": len(log.rag_hits_json or []),
                "response_preview": (log.response_text or "")[:200],
            }
            for log in logs
        ]
    }


@router.get("/{log_id}")
async def get_log(log_id: UUID, db: AsyncSession = Depends(get_db)):
    log = await db.get(LLMCallLog, log_id)
    if not log:
        raise HTTPException(404, "Log not found")
    return {
        "id": str(log.id),
        "task_type": log.task_type,
        "project_id": str(log.project_id) if log.project_id else None,
        "chapter_id": str(log.chapter_id) if log.chapter_id else None,
        "prompt_id": str(log.prompt_id) if log.prompt_id else None,
        "endpoint_id": str(log.endpoint_id) if log.endpoint_id else None,
        "messages_json": log.messages_json,
        "rag_hits_json": log.rag_hits_json or [],
        "response_text": log.response_text,
        "input_tokens": log.input_tokens,
        "output_tokens": log.output_tokens,
        "latency_ms": log.latency_ms,
        "model": log.model,
        "status": log.status,
        "error_message": log.error_message,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


@router.delete("/{log_id}", status_code=204)
async def delete_log(log_id: UUID, db: AsyncSession = Depends(get_db)):
    log = await db.get(LLMCallLog, log_id)
    if not log:
        raise HTTPException(404, "Log not found")
    await db.delete(log)
    await db.flush()
