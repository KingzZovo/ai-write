"""Project management endpoints."""

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
    db: AsyncSession = Depends(get_db),
) -> ProjectListResponse:
    """List all projects with pagination."""
    count_result = await db.execute(select(func.count(Project.id)))
    total = count_result.scalar_one()

    result = await db.execute(
        select(Project)
        .order_by(Project.updated_at.desc())
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
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Get a single project by ID, including its volumes."""
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
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a project and all related data."""
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    await db.delete(project)
    await db.flush()


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
