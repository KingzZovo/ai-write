"""/api/ask-user — answer / cancel / list pending pauses."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.models.ask_user import AskUserPause

router = APIRouter(
    prefix="/api/ask-user",
    tags=["ask-user"],
)


@router.get("/pending")
async def list_pending(project_id: str, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(AskUserPause)
        .where(
            AskUserPause.project_id == project_id,
            AskUserPause.status == "pending",
        )
        .order_by(AskUserPause.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {
        "pending": [
            {
                "id": str(p.id),
                "question": p.question,
                "chapter_id": str(p.chapter_id) if p.chapter_id else None,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "timeout_at": p.timeout_at.isoformat(),
            }
            for p in rows
        ]
    }


@router.post("/{pause_id}/answer")
async def answer(
    pause_id: UUID,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    answer_text = (body.get("answer") or "").strip()
    if not answer_text:
        raise HTTPException(400, "answer required")
    pause = await db.get(AskUserPause, pause_id)
    if not pause:
        raise HTTPException(404)
    if pause.status != "pending":
        raise HTTPException(409, f"pause is {pause.status}")

    pause.status = "answered"
    pause.answer = answer_text
    pause.answered_at = datetime.now(timezone.utc)
    # 2026-11-17 审计:此前只 flush,commit 在 get_db 收尾 —— publish 先到,
    # worker 可能已按答案继续,而暂停点仍 pending(commit 失败时更是如此)。
    # 先落库再广播。
    await db.commit()
    await db.refresh(pause)

    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.REDIS_URL)
    try:
        await client.publish(f"ask_user:answer:{pause_id}", answer_text)
    finally:
        await client.close()
    return {"ok": True}


@router.post("/{pause_id}/cancel")
async def cancel(pause_id: UUID, db: AsyncSession = Depends(get_db)):
    pause = await db.get(AskUserPause, pause_id)
    if not pause:
        raise HTTPException(404)
    # 2026-11-17 审计:此前不看状态就改 cancelled,会把已答复的记录抹掉
    # (worker 可能已经按答案继续,前端却看到"已取消")。
    if pause.status != "pending":
        raise HTTPException(409, f"pause is {pause.status}")
    pause.status = "cancelled"
    await db.flush()
    return {"ok": True}
