"""/api/arc — 弧式增量创作循环编排端点（子项目 A）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import get_db
from app.models.project import Outline, Project, Volume
from app.services.arc_loop import (
    ArcState,
    advance_arc_state,
    build_arc_completion_suggestions,
    build_next_chapter_brief,
    clamp_target_chapters,
    generate_arc_outline,
    parse_arc_state,
    serialize_arc_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/arc", tags=["arc"])


class StartArcBody(BaseModel):
    idea: str
    background: str = ""
    core_setup: str = ""
    opening_scene: str = ""
    target_chapters: int = 20


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


async def _load_current_arc(db: AsyncSession, project_id: str):
    """返回最高 volume_idx 的弧 (volume_idx, Outline, ArcState)；无则 (None, None, None)。"""
    result = await db.execute(
        select(Outline).where(
            Outline.project_id == project_id,
            Outline.level == "volume",
        )
    )
    best = None
    for o in result.scalars().all():
        st = parse_arc_state(o.content_json)
        if st is None:
            continue
        vidx = int((o.content_json or {}).get("volume_idx") or 0)
        if best is None or vidx > best[0]:
            best = (vidx, o, st)
    if best is None:
        return None, None, None
    return best[0], best[1], best[2]


async def _persist_arc(
    db: AsyncSession, outline: Outline, state: ArcState, volume_idx: int
) -> None:
    content_json = serialize_arc_state(state, volume_idx=volume_idx)
    existing = outline.content_json or {}
    if "beats" in existing:
        content_json["beats"] = existing["beats"]
    outline.content_json = content_json
    flag_modified(outline, "content_json")
    await db.flush()


async def _create_arc(
    db: AsyncSession, project_id: str, body: StartArcBody, volume_idx: int
) -> dict:
    """生成弧大纲 + 建 Volume + Outline(_arc)。大纲失败抛 502（事务回滚不建半截）。"""
    target = clamp_target_chapters(body.target_chapters)
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

    title = outline.get("title") or f"第{volume_idx}弧"
    volume = Volume(
        project_id=project_id,
        title=title,
        volume_idx=volume_idx,
        summary=body.core_setup,
    )
    db.add(volume)
    await db.flush()

    state = ArcState(
        title=title,
        core_setup=body.core_setup,
        opening_scene=body.opening_scene,
        target_chapters=target,
        status="active",
        chapters_written=0,
        running_outline="",
        next_direction=body.opening_scene or None,
    )
    content_json = serialize_arc_state(state, volume_idx=volume_idx)
    content_json["beats"] = outline.get("beats", [])
    db.add(
        Outline(
            project_id=project_id,
            level="volume",
            content_json=content_json,
            is_confirmed=1,
        )
    )
    await db.flush()
    return {"volume_idx": volume_idx, "arc": _arc_dict(state)}


@router.post("/{project_id}/start", status_code=201)
async def start_arc(
    project_id: str,
    body: StartArcBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return await _create_arc(db, project_id, body, volume_idx=1)


@router.get("/{project_id}/current")
async def current_arc(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vidx, _ol, state = await _load_current_arc(db, project_id)
    if state is None:
        return {"arc": None}
    return {"volume_idx": vidx, "arc": _arc_dict(state)}


class ChapterWrittenBody(BaseModel):
    chapter_summary: str = ""


class NextDirectionBody(BaseModel):
    direction: str


async def _persist_arc(db: AsyncSession, outline: Outline, state: ArcState, volume_idx: int) -> None:
    content_json = serialize_arc_state(state, volume_idx=volume_idx)
    # 保留 beats（不在 ArcState 里）
    existing = outline.content_json or {}
    if "beats" in existing:
        content_json["beats"] = existing["beats"]
    outline.content_json = content_json
    # SQLAlchemy JSON 列就地改不脏标记，显式 flag
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(outline, "content_json")
    await db.flush()


@router.post("/{project_id}/chapter-written")
async def chapter_written(
    project_id: str,
    body: ChapterWrittenBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """章节写完后推进弧状态（chapters_written+1，更新 running_outline）。"""
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    new_state = advance_arc_state(
        state, event="chapter_written",
        running_outline_append=body.chapter_summary,
    )
    await _persist_arc(db, ol, new_state, vidx)
    return {"volume_idx": vidx, "arc": _arc_dict(new_state)}


@router.post("/{project_id}/next-direction")
async def next_direction(
    project_id: str,
    body: NextDirectionBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    new_state = advance_arc_state(state, event="set_direction", next_direction=body.direction)
    await _persist_arc(db, ol, new_state, vidx)
    return {"volume_idx": vidx, "arc": _arc_dict(new_state)}


@router.get("/{project_id}/chapter-brief")
async def chapter_brief(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    beats = (ol.content_json or {}).get("beats", [])
    brief = build_next_chapter_brief(state, arc_beats=beats)
    return {"volume_idx": vidx, "brief": brief, "next_chapter_idx": state.chapters_written + 1}
