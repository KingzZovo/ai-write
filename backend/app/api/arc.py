"""/api/arc — 弧式增量创作循环编排端点（子项目 A）。"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.models.project import Project, Volume
from app.api.chapter_order import structure_write
from app.services.arc_materialization import materialize_arc
from app.services.arc_loop import (
    ArcState,
    build_arc_completion_suggestions,
    build_next_chapter_brief,
    generate_arc_outline,
    parse_arc_state,
    serialize_arc_state,
)

from app.services.arc_outline import validate_arc_target, validate_complete_arc_outline
from app.services.arc_sources import ArcSourceError, arc_source_snapshot, load_current_arc
from app.services.arc_progress_contract import ArcProgressError
from app.services.arc_progress_reader import load_arc_progress, require_current_progress
from app.services.outline_authority import outline_for_generation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/arc", tags=["arc"])


class StartArcBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str | None = None
    expected_source: dict | None = None
    idea: str
    background: str = ""
    core_setup: str = ""
    opening_scene: str = ""
    target_chapters: int = Field(default=20, strict=True, ge=4, le=40)


class ChapterWrittenBody(BaseModel):
    chapter_summary: str = ""


class NextDirectionBody(BaseModel):
    direction: str


def _arc_dict(state: ArcState) -> dict:
    return {
        "title": state.title,
        "core_setup": state.core_setup,
        "opening_scene": state.opening_scene,
        "target_chapters": state.target_chapters,
        "status": state.status,
        "chapters_written": state.chapters_written,
        "running_outline": state.running_outline,
        "next_direction": state.next_direction,
        "suggestions": state.suggestions,
    }


async def _load_current_arc(db: AsyncSession, project_id: str, *, lock=False):
    try:
        return await load_current_arc(db, project_id, lock=lock)
    except ArcSourceError as exc:
        raise HTTPException(409, detail=str(exc)) from None


async def _progress_for_outline(db, project_id, volume_idx, outline, *, lock=False):
    vid = outline.content_json.get("volume_id")
    if vid is None:
        vid = await db.scalar(select(Volume.id).where(Volume.project_id == UUID(str(project_id)),
                                                     Volume.volume_idx == volume_idx))
    try:
        view = await load_arc_progress(db, project_id, vid, lock=lock)
        if view.outline.id != outline.id or view.volume.volume_idx != volume_idx:
            raise ArcProgressError("arc_progress_changed", "当前弧在读取期间已变化，请重新核对。")
        return view
    except ArcProgressError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None


def _require_progress(view, *, completed=False):
    try:
        require_current_progress(view, completed=completed)
    except ArcProgressError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None


def _legacy_progress_error():
    raise HTTPException(428, detail={"code": "arc_progress_precondition_required",
        "message": "请通过分卷的作者进度入口，读取当前来源并以固定请求确认具体保存稿。旧摘要不能推进进度。"})


async def _create_arc(
    db: AsyncSession, project_id: str, body: StartArcBody, volume_idx: int
) -> dict:
    """生成弧大纲 + 建 Volume + Outline(_arc)。大纲失败抛 502（事务回滚不建半截）。"""
    target = validate_arc_target(body.target_chapters)
    outline = await generate_arc_outline(
        idea=body.idea,
        background=body.background,
        core_setup=body.core_setup,
        opening_scene=body.opening_scene,
        target_chapters=target,
        db=db,
        project_id=project_id,
    )
    if not outline.get("available"):
        raise HTTPException(status_code=502, detail="Arc outline generation failed")

    return await _save_generated_arc(db, project_id, body, volume_idx, outline, target)


async def _save_generated_arc(db, project_id, body, volume_idx, outline, target, *, expected_previous=None,
                              expected_progress=None):
    # Validate the complete result again at the write boundary. A mocked,
    # stale or legacy planner cannot materialize a half-planned volume.
    if target != body.target_chapters:
        raise HTTPException(409, detail="Arc chapter count no longer matches the original request")
    try:
        plan = validate_complete_arc_outline(outline, target)
    except (ValueError, TypeError) as exc:
        raise HTTPException(502, detail=f"Arc outline incomplete: {exc}") from None
    async with structure_write(db, project_id):
        if expected_previous is not None:
            current_idx, current, _ = await _load_current_arc(db, project_id, lock=True)
            if arc_source_snapshot(current_idx, current) != expected_previous:
                raise HTTPException(409, detail="Previous arc changed during planning; reload before starting the next arc")
            if current is not None:
                view = await _progress_for_outline(db, project_id, current_idx, current, lock=True)
                _require_progress(view, completed=True)
                if view.source != expected_progress:
                    raise HTTPException(409, detail="Previous arc progress changed during planning; review it again")
        return await materialize_arc(db, project_id, body.model_dump(), volume_idx, plan)


@router.post("/{project_id}/start", status_code=202)
async def start_arc(
    project_id: UUID,
    body: StartArcBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.api.arc_planning import ArcPlanBody, submit_arc_plan
    return await submit_arc_plan(project_id, ArcPlanBody(mode="start", **body.model_dump()), db)


@router.get("/{project_id}/current")
async def current_arc(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vidx, outline, state = await _load_current_arc(db, project_id)
    if state is None:
        return {"arc": None}
    view = await _progress_for_outline(db, project_id, vidx, outline)
    return {"volume_id": str(view.volume.id), "volume_idx": vidx,
            "arc": _arc_dict(view.projected_arc()), "progress": view.public()}


@router.post("/{project_id}/chapter-written")
async def chapter_written(
    project_id: str,
    body: ChapterWrittenBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Legacy unscoped reports cannot certify a saved manuscript."""
    _legacy_progress_error()


@router.post("/{project_id}/next-direction")
async def next_direction(
    project_id: str,
    body: NextDirectionBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _legacy_progress_error()


@router.get("/{project_id}/chapter-brief")
async def chapter_brief(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    view = await _progress_for_outline(db, project_id, vidx, ol)
    _require_progress(view)
    cid = view.public()["next_chapter_id"]
    chapter = next((chapter for chapter in view.chapters if chapter["id"] == cid), None)
    if chapter is None:
        raise HTTPException(409, detail="本弧没有待写章节；如需续写，请先明确继续并核对章节安排。")
    try:
        brief = build_next_chapter_brief(view.projected_arc(before_global_idx=chapter["global_idx"]), arc_outline=view.outline.content_json,
            chapter_idx=chapter["chapter_idx"], chapter_id=cid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, detail=f"Arc chapter plan unavailable: {exc}") from None
    return {"volume_id": str(view.volume.id), "volume_idx": vidx, "brief": brief,
            "chapter_id": cid, "next_chapter_idx": chapter["chapter_idx"], "source": view.source}


@router.post("/{project_id}/complete")
async def complete_arc(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    _legacy_progress_error()


@router.post("/{project_id}/next-arc", status_code=202)
async def next_arc(
    project_id: UUID,
    body: StartArcBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.api.arc_planning import ArcPlanBody, submit_arc_plan
    return await submit_arc_plan(project_id, ArcPlanBody(mode="next", **body.model_dump()), db)
