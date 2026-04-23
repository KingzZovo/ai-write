"""Project management endpoints."""

from datetime import datetime, timezone as _tz
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.project import Project
from app.schemas.project import (
    ProjectCreate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    skip: int = 0,
    limit: int = 50,
    trashed: bool = False,
    db: AsyncSession = Depends(get_db),
) -> ProjectListResponse:
    """List projects; active by default. Pass ?trashed=true for the trash bin."""
    deleted_filter = (
        Project.deleted_at.is_not(None) if trashed else Project.deleted_at.is_(None)
    )
    order_col = (
        Project.deleted_at.desc() if trashed else Project.updated_at.desc()
    )

    count_result = await db.execute(
        select(func.count(Project.id)).where(deleted_filter)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Project)
        .where(deleted_filter)
        .order_by(order_col)
        .offset(skip)
        .limit(limit)
    )
    projects = list(result.scalars().all())

    return ProjectListResponse(
        projects=[ProjectResponse.model_validate(p) for p in projects],
        total=total,
    )


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Create a new writing project."""
    project = Project(
        title=body.title,
        genre=body.genre,
        premise=body.premise,
        settings_json=body.settings_json or {},
    )
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: UUID,
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Get a single project by ID. Returns 404 for soft-deleted unless include_deleted=true."""
    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.volumes),
        )
        .where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.deleted_at is not None and not include_deleted:
        raise HTTPException(status_code=404, detail="Project not found")

    return ProjectResponse.model_validate(project)


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Update an existing project."""
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)

    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: UUID,
    purge: bool = False,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete by default; pass ?purge=true to hard-delete from trash."""
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if purge:
        await db.delete(project)
    else:
        if project.deleted_at is None:
            project.deleted_at = datetime.now(_tz.utc)
    await db.flush()


@router.post("/{project_id}/restore", response_model=ProjectResponse)
async def restore_project(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Restore a soft-deleted project."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.deleted_at is None:
        raise HTTPException(status_code=400, detail="Project is not deleted")
    project.deleted_at = None
    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.get("/{project_id}/export")
async def export_project(
    project_id: UUID,
    format: str = "txt",
    db: AsyncSession = Depends(get_db),
):
    """Export a project's chapters as TXT or EPUB."""
    from fastapi.responses import Response
    from app.models.project import Volume, Chapter

    project = await db.get(Project, str(project_id))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get all volumes and chapters
    vol_result = await db.execute(
        select(Volume).where(Volume.project_id == str(project_id)).order_by(Volume.volume_idx)
    )
    volumes = list(vol_result.scalars().all())

    vol_ids = [str(v.id) for v in volumes]
    ch_result = await db.execute(
        select(Chapter).where(Chapter.volume_id.in_(vol_ids) if vol_ids else Chapter.id.is_(None)).order_by(Chapter.chapter_idx)
    )
    all_chapters = list(ch_result.scalars().all())

    # Group chapters by volume
    vol_ids = {str(v.id) for v in volumes}
    chapters_by_vol: dict[str, list] = {str(v.id): [] for v in volumes}
    ungrouped = []
    for ch in all_chapters:
        vid = str(ch.volume_id) if ch.volume_id else ""
        if vid in chapters_by_vol:
            chapters_by_vol[vid].append(ch)
        elif vid == "" or vid not in vol_ids:
            ungrouped.append(ch)

    if format == "txt":
        lines = [f"《{project.title}》\n\n"]
        for vol in volumes:
            lines.append(f"\n{'='*40}\n{vol.title}\n{'='*40}\n\n")
            for ch in chapters_by_vol.get(str(vol.id), []):
                lines.append(f"\n{ch.title}\n\n")
                lines.append((ch.content_text or "") + "\n")
        for ch in ungrouped:
            lines.append(f"\n{ch.title}\n\n")
            lines.append((ch.content_text or "") + "\n")

        content = "".join(lines)
        return Response(
            content=content.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{project.title}.txt"'},
        )

    elif format == "epub":
        import io
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier(str(project_id))
        book.set_title(project.title)
        book.set_language("zh")

        spine = ["nav"]
        toc = []

        for vol in volumes:
            for ch in chapters_by_vol.get(str(vol.id), []):
                c = epub.EpubHtml(
                    title=ch.title,
                    file_name=f"ch_{ch.chapter_idx}.xhtml",
                    lang="zh",
                )
                text = (ch.content_text or "").replace("\n", "<br/>")
                c.content = f"<h2>{ch.title}</h2><p>{text}</p>"
                book.add_item(c)
                spine.append(c)
                toc.append(c)

        book.toc = toc
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = spine

        buf = io.BytesIO()
        epub.write_epub(buf, book)
        buf.seek(0)

        return Response(
            content=buf.read(),
            media_type="application/epub+zip",
            headers={"Content-Disposition": f'attachment; filename="{project.title}.epub"'},
        )

    raise HTTPException(status_code=400, detail="不支持的格式，请使用 txt 或 epub")


@router.post("/{project_id}/allocate-budget")
async def allocate_budget(
    project_id: UUID,
    force: bool = False,
    dry_run: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Allocate target_word_count across volumes and chapters of a project.

    Strategy: equal split with remainder absorbed by the last slot.

    Query params:
      - force:   when true, overwrite all targets regardless of current value.
                 when false (default), only overwrite values still equal to the
                 documented default (Volume=200000, Chapter=50000).
      - dry_run: when true, return the computed plan without persisting.
    """
    from app.models.project import Chapter, Volume
    from app.services.budget_allocator import allocate_project_budget

    project = await db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    vol_result = await db.execute(
        select(Volume)
        .where(Volume.project_id == project_id)
        .order_by(Volume.volume_idx)
    )
    volumes = list(vol_result.scalars().all())

    chapters_by_vol: dict[str, list] = {str(v.id): [] for v in volumes}
    if volumes:
        vol_ids = [v.id for v in volumes]
        ch_result = await db.execute(
            select(Chapter)
            .where(Chapter.volume_id.in_(vol_ids))
            .order_by(Chapter.chapter_idx)
        )
        for ch in ch_result.scalars().all():
            chapters_by_vol.setdefault(str(ch.volume_id), []).append(ch)

    vol_input: list[dict] = []
    for v in volumes:
        vol_input.append(
            {
                "id": str(v.id),
                "volume_idx": v.volume_idx,
                "current_target": int(v.target_word_count or 0),
                "chapters": [
                    {
                        "id": str(c.id),
                        "chapter_idx": c.chapter_idx,
                        "current_target": int(c.target_word_count or 0),
                    }
                    for c in chapters_by_vol.get(str(v.id), [])
                ],
            }
        )

    plan = allocate_project_budget(
        project_total=int(project.target_word_count or 0),
        volumes=vol_input,
        force=force,
    )

    if not dry_run:
        v_by_id = {str(v.id): v for v in volumes}
        ch_by_id: dict[str, object] = {}
        for chs in chapters_by_vol.values():
            for c in chs:
                ch_by_id[str(c.id)] = c
        for v_plan in plan["volumes"]:
            if v_plan["changed"]:
                vm = v_by_id.get(v_plan["id"])
                if vm is not None:
                    vm.target_word_count = int(v_plan["new_target"])
            for c_plan in v_plan["chapters"]:
                if c_plan["changed"]:
                    cm = ch_by_id.get(c_plan["id"])
                    if cm is not None:
                        cm.target_word_count = int(c_plan["new_target"])
        await db.flush()

    plan["dry_run"] = dry_run
    plan["force"] = force
    return plan


@router.get("/{project_id}/budget-status")
async def budget_status(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read-only audit of how target_word_count is distributed on a project.

    Returns three coherence levels:
      - project_total vs SUM(volumes.target_word_count)   => volumes_drift
      - each volume.target_word_count vs SUM(its chapters) => per_volume[].chapters_drift
      - project_total vs SUM(all chapters.target_word_count) => chapters_drift

    drift > 0 means over-allocated (assigned more than the parent budget).
    drift < 0 means under-allocated. healthy = (drift == 0).

    Never mutates data. Safe to poll.
    """
    from app.models.project import Chapter, Volume

    project = await db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    vol_result = await db.execute(
        select(Volume)
        .where(Volume.project_id == project_id)
        .order_by(Volume.volume_idx)
    )
    volumes = list(vol_result.scalars().all())

    chapters_by_vol: dict[str, list] = {str(v.id): [] for v in volumes}
    if volumes:
        vol_ids = [v.id for v in volumes]
        ch_result = await db.execute(
            select(Chapter)
            .where(Chapter.volume_id.in_(vol_ids))
            .order_by(Chapter.chapter_idx)
        )
        for ch in ch_result.scalars().all():
            chapters_by_vol.setdefault(str(ch.volume_id), []).append(ch)

    project_total = int(project.target_word_count or 0)
    volumes_sum = 0
    chapters_sum = 0
    per_volume: list[dict] = []
    for v in volumes:
        v_target = int(v.target_word_count or 0)
        v_chapters = chapters_by_vol.get(str(v.id), [])
        v_ch_sum = sum(int(c.target_word_count or 0) for c in v_chapters)
        v_drift = v_ch_sum - v_target
        volumes_sum += v_target
        chapters_sum += v_ch_sum
        per_volume.append(
            {
                "volume_id": str(v.id),
                "volume_idx": v.volume_idx,
                "target": v_target,
                "chapter_count": len(v_chapters),
                "chapters_sum": v_ch_sum,
                "chapters_drift": v_drift,
                "chapters_healthy": v_drift == 0,
            }
        )

    volumes_drift = volumes_sum - project_total
    chapters_drift_vs_project = chapters_sum - project_total

    return {
        "project_id": str(project.id),
        "project_total": project_total,
        "volume_count": len(volumes),
        "volumes_sum": volumes_sum,
        "volumes_drift": volumes_drift,
        "volumes_healthy": volumes_drift == 0,
        "chapters_sum": chapters_sum,
        "chapters_drift": chapters_drift_vs_project,
        "chapters_healthy": chapters_drift_vs_project == 0,
        "per_volume": per_volume,
    }
