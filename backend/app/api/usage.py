"""/api/usage — 项目/章级 token 用量与缓存命中率(作者可见)。

两个只读端点:

* ``GET /api/usage/projects/{project_id}/summary`` — 按章 / 步骤 / 模型 / 天
  分组的 token 用量与缓存命中率;
* ``GET /api/usage/chapters/{chapter_id}/budget`` — 单章已用 token 与预算判定。

聚合全部在 SQL 里做(见 ``app/services/usage_summary.py``):只选 token 列与
分组键,不读 ``messages_json`` / ``response_text``。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.usage_summary import (
    DEFAULT_DAYS,
    DEFAULT_GROUP_BY,
    MAX_DAYS,
    MIN_DAYS,
    fetch_chapter_usage_budget,
    fetch_project_usage_summary,
    normalize_group_by,
)

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/projects/{project_id}/summary")
async def project_usage_summary(
    project_id: UUID,
    group_by: str = Query(DEFAULT_GROUP_BY),
    days: int = Query(DEFAULT_DAYS, ge=MIN_DAYS, le=MAX_DAYS),
    chapter_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    """项目 token 用量汇总(默认最近 30 天,按章分组)。

    非法 ``group_by`` 返回 400(而不是静默退回默认值):前端按下发的
    ``group_by`` 渲染分组列,悄悄换一个维度会让作者读到错的数字。
    """
    try:
        group_by = normalize_group_by(group_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return await fetch_project_usage_summary(
            db,
            project_id=project_id,
            group_by=group_by,
            days=days,
            chapter_id=chapter_id,
        )
    except ValueError as exc:  # 双保险:深层校验失败同样是 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chapters/{chapter_id}/budget")
async def chapter_usage_budget(
    chapter_id: UUID,
    limit_tokens: int = Query(100_000, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """单章 token 用量 + 预算判定(``used_tokens >= limit_tokens`` 即超)。"""
    return await fetch_chapter_usage_budget(
        db, chapter_id=chapter_id, limit_tokens=limit_tokens
    )