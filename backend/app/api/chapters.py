"""Chapter management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, Project, Volume
from app.services.chapter_target_words import (
    CHAPTER_DEFAULT_WORD_COUNT,
    resolve_chapter_target_word_count,
)
from app.services.outline_readiness import LAYER_LABELS, build_outline_readiness_report

router = APIRouter(prefix="/api/projects/{project_id}/chapters", tags=["chapters"])


class ChapterCreate(BaseModel):
    volume_id: str
    title: str
    chapter_idx: int
    outline_json: dict = {}


class ChapterUpdate(BaseModel):
    title: str | None = None
    content_text: str | None = None
    outline_json: dict | None = None
    status: str | None = None
    target_word_count: int | None = None
    # PR-CHAPTER-PROTECT-V1: bypass the empty / shrink guards when the
    # caller really does mean to wipe or drastically reduce the chapter.
    force: bool | None = False


class ChapterSyncRequest(BaseModel):
    old_text: str
    new_text: str


class ChapterResponse(BaseModel):
    id: UUID
    volume_id: UUID
    title: str
    chapter_idx: int
    # PR-FIX-CHAPTER-422: tolerate NULL columns (legacy rows from V2 generation)
    outline_json: dict | None = None
    content_text: str | None = ""
    word_count: int | None = 0
    status: str
    summary: str | None = None
    target_word_count: int | None = CHAPTER_DEFAULT_WORD_COUNT

    model_config = {"from_attributes": True}


def _chapter_response_payload(
    chapter: Chapter,
    project_target_chapter_words: int | None,
) -> dict:
    payload = {
        "id": chapter.id,
        "volume_id": chapter.volume_id,
        "title": chapter.title,
        "chapter_idx": chapter.chapter_idx,
        "outline_json": chapter.outline_json,
        "content_text": chapter.content_text,
        "word_count": chapter.word_count,
        "status": chapter.status,
        "summary": chapter.summary,
        "target_word_count": resolve_chapter_target_word_count(
            chapter.target_word_count,
            project_target_chapter_words,
        ),
        "created_at": chapter.created_at,
        "updated_at": chapter.updated_at,
    }
    return payload


@router.get("")
async def list_chapters(
    project_id: str,
    volume_id: str | None = None,
    lightweight: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """List chapters, optionally filtered by volume.

    lightweight=true omits content_text and outline_json for fast loading.
    """
    if volume_id:
        query = select(Chapter).where(Chapter.volume_id == volume_id).order_by(Chapter.chapter_idx)
    else:
        # Get all chapters for project via volumes
        vol_query = select(Volume.id).where(Volume.project_id == project_id)
        vol_result = await db.execute(vol_query)
        volume_ids = [str(v) for v in vol_result.scalars().all()]
        if not volume_ids:
            return []
        query = select(Chapter).where(Chapter.volume_id.in_(volume_ids)).order_by(Chapter.chapter_idx)

    result = await db.execute(query)
    chapters = result.scalars().all()
    project_settings: dict | None = None
    project_target_chapter_words: int | None = None
    project_result = await db.execute(select(Project).where(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    if project is not None and isinstance(project.settings_json, dict):
        project_settings = project.settings_json
        project_target_chapter_words = project_settings.get("target_chapter_words")
    if lightweight:
        # PR-OUTLINE-BUTTONS: keep outline_json so OutlineTree's per-chapter
        # "⊶大纲" button can render. Drop only content_text (the heavy field).
        return [
            {
                "id": str(c.id),
                "volume_id": str(c.volume_id),
                "title": c.title,
                "chapter_idx": c.chapter_idx,
                "word_count": c.word_count,
                "status": c.status,
                "target_word_count": resolve_chapter_target_word_count(
                    c.target_word_count,
                    project_target_chapter_words,
                ),
                "outline_json": c.outline_json,
                "summary": c.summary,
            }
            for c in chapters
        ]
    return [
        ChapterResponse.model_validate(
            _chapter_response_payload(c, project_target_chapter_words)
        )
        for c in chapters
    ]


@router.post("", status_code=201)
async def create_chapter(
    project_id: str,
    body: ChapterCreate,
    db: AsyncSession = Depends(get_db),
) -> ChapterResponse:
    """Create a new chapter."""
    chapter = Chapter(
        volume_id=body.volume_id,
        title=body.title,
        chapter_idx=body.chapter_idx,
        outline_json=body.outline_json,
    )
    db.add(chapter)
    await db.flush()
    await db.refresh(chapter)
    project_result = await db.execute(select(Project).where(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    project_target_chapter_words = None
    if project is not None and isinstance(project.settings_json, dict):
        project_target_chapter_words = project.settings_json.get("target_chapter_words")
    return ChapterResponse.model_validate(
        _chapter_response_payload(chapter, project_target_chapter_words)
    )


@router.get("/{chapter_id}")
async def get_chapter(
    project_id: str,
    chapter_id: str,
    db: AsyncSession = Depends(get_db),
) -> ChapterResponse:
    """Get a single chapter with full content."""
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    project_result = await db.execute(select(Project).where(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    project_target_chapter_words = None
    if project is not None and isinstance(project.settings_json, dict):
        project_target_chapter_words = project.settings_json.get("target_chapter_words")
    return ChapterResponse.model_validate(
        _chapter_response_payload(chapter, project_target_chapter_words)
    )


@router.put("/{chapter_id}")
async def update_chapter(
    project_id: str,
    chapter_id: str,
    body: ChapterUpdate,
    db: AsyncSession = Depends(get_db),
) -> ChapterResponse:
    """Update a chapter (content, title, status, etc.)."""
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    # ----- PR-CHAPTER-PROTECT-V1 guards -----
    # The chapter body is the single most expensive artifact in this app.
    # An accidental PUT with an empty / shrunk body has wiped real content
    # in production (lct2 ch21/22 incident). Block destructive writes
    # unless the caller explicitly opts in by sending ``force: true`` in
    # the JSON body alongside ``content_text``.
    data = body.model_dump(exclude_unset=True)

    if body.title is not None:
        chapter.title = body.title
    if body.content_text is not None and "content_text" in data:
        existing = chapter.content_text or ""
        new_text = body.content_text or ""
        force_flag = bool(data.get("force") or False)
        if not force_flag:
            stripped_new = new_text.strip()
            if not stripped_new and len(existing.strip()) >= 200:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "chapter_protect_empty_content",
                        "message": (
                            "Refusing to overwrite a non-empty chapter "
                            "(>=200 chars) with empty content. Pass "
                            "force=true in the body to override."
                        ),
                        "existing_chars": len(existing),
                    },
                )
            if (
                len(existing.strip()) >= 1000
                and len(stripped_new) > 0
                and len(stripped_new) < len(existing.strip()) * 0.4
            ):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "chapter_protect_shrink",
                        "message": (
                            "Refusing to shrink chapter body by more than "
                            "60%. Pass force=true in the body to override."
                        ),
                        "existing_chars": len(existing),
                        "new_chars": len(new_text),
                    },
                )
        chapter.content_text = body.content_text
        chapter.word_count = len(body.content_text)
    if body.outline_json is not None:
        chapter.outline_json = body.outline_json
    if body.status is not None:
        chapter.status = body.status
    if "target_word_count" in data and body.target_word_count is not None:
        chapter.target_word_count = body.target_word_count

    await db.flush()
    await db.refresh(chapter)
    if body.content_text is not None:
        # B2' (v1.5.0): kick the entity-extraction Celery task whenever the
        # chapter body is rewritten via PATCH. Idempotent and non-blocking.
        from app.services.entity_dispatch import dispatch_for_chapter
        await dispatch_for_chapter(
            chapter, db,
            caller="api.chapters.update_chapter",
            project_id_hint=project_id,
        )
    project_result = await db.execute(select(Project).where(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    project_target_chapter_words = None
    if project is not None and isinstance(project.settings_json, dict):
        project_target_chapter_words = project.settings_json.get("target_chapter_words")
    return ChapterResponse.model_validate(
        _chapter_response_payload(chapter, project_target_chapter_words)
    )


@router.delete("/{chapter_id}", status_code=204)
async def delete_chapter(
    project_id: str,
    chapter_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a chapter."""
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    await db.delete(chapter)


@router.post("/{chapter_id}/sync")
async def sync_chapter_edit(
    project_id: str,
    chapter_id: str,
    body: ChapterSyncRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Trigger incremental sync after user edits a chapter."""
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    from app.services.incremental_sync import IncrementalSyncService

    sync_service = IncrementalSyncService(db=db)
    result = await sync_service.process_edit(
        chapter_id=chapter_id,
        old_text=body.old_text,
        new_text=body.new_text,
    )
    return result


@router.post("/{chapter_id}/outline/expand")
async def expand_chapter_outline_endpoint(
    project_id: str,
    chapter_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """PR-OUTLINE-DEEPDIVE Phase 1：调 LLM 为本章生成含跳板资产的详细大纲。

    输出覆盖写入 ``Chapter.outline_json``，返回新的 outline_json 与 LLM 调用提示。
    同步调用（1–3 秒 LLM 延迟内返回）。Celery 包装推后主提。
    """
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    readiness = await build_outline_readiness_report(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
    )
    upstream_missing = [
        layer for layer in readiness.missing_layers if layer in {"book", "volume"}
    ]
    if upstream_missing:
        labels = "、".join(LAYER_LABELS.get(layer, layer) for layer in upstream_missing)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "outline_chain_incomplete",
                "message": f"缺少：{labels}",
                "missing_layers": upstream_missing,
                "readiness": readiness.to_dict(),
            },
        )

    from app.services.chapter_outline_expander import (
        ChapterOutlineExpandError,
        expand_chapter_outline,
    )

    try:
        new_outline = await expand_chapter_outline(
            project_id=project_id,
            chapter_id=chapter_id,
            db=db,
        )
    except ChapterOutlineExpandError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return {
        "chapter_id": chapter_id,
        "outline_json": new_outline,
    }
