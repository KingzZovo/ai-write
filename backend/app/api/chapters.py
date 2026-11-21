"""Chapter management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.db.session import get_db
from app.models.project import Chapter, Project, Volume
from app.services.chapter_target_words import (
    CHAPTER_DEFAULT_WORD_COUNT,
    resolve_chapter_target_word_count,
    resolve_chapter_words,
)
from app.services.outline_readiness import LAYER_LABELS, build_outline_readiness_report
from app.api.chapter_order import router as order_router, structure_write
from app.api.chapter_manuscripts import (
    ManuscriptReceipt, author_manuscript_write, manuscript_source, router as manuscript_router,
)
from app.services.author_manuscripts import prepare_author_manuscript
from app.services.chapter_order import chapter_order_version, reindex_chapter_structure, require_free_index
from app.services.chapter_content import ChapterContentError, check_chapter_replacement

router = APIRouter(prefix="/api/projects/{project_id}/chapters", tags=["chapters"])
router.include_router(order_router)
router.include_router(manuscript_router)


class ChapterCreate(BaseModel):
    volume_id: str
    title: str
    chapter_idx: int = Field(..., strict=True, ge=1, le=2147483647)
    outline_json: dict = {}
    target_word_count: int | None = None


class ChapterUpdate(BaseModel):
    title: str | None = None
    content_text: str | None = None
    expected_content_text: str | None = None
    expected_outline_json: dict | None = None
    outline_json: dict | None = None
    status: str | None = None
    target_word_count: int | None = None
    # Authors can correct (or clear) what the system remembers about a
    # chapter; summary used to be API-read-only.
    summary: str | None = None
    # Renumber to an unused label. Occupied labels use the atomic move API.
    chapter_idx: int | None = Field(None, strict=True, ge=1, le=2147483647)
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
    global_idx: int | None = None
    order_version: str | None = None
    # PR-FIX-CHAPTER-422: tolerate NULL columns (legacy rows from V2 generation)
    outline_json: dict | None = None
    content_text: str | None = ""
    word_count: int | None = 0
    status: str
    summary: str | None = None
    target_word_count: int | None = CHAPTER_DEFAULT_WORD_COUNT
    # Present for a body update, after a confirmed commit. Metadata-only
    # updates and reads retain their existing payload without claiming one.
    manuscript_receipt: ManuscriptReceipt | None = None

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
        "global_idx": chapter.global_idx,
        "order_version": chapter_order_version(chapter),
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


async def _project_chapter(db: AsyncSession, project_id: str, chapter_id: str, *, lock: bool = False) -> Chapter:
    query = select(Chapter).join(Volume, Chapter.volume_id == Volume.id).where(
        Chapter.id == UUID(str(chapter_id)), Volume.project_id == UUID(str(project_id)),
    ).execution_options(autoflush=False, populate_existing=lock)
    if lock:
        query = query.with_for_update(of=Chapter)
    chapter = (await db.execute(query)).scalar_one_or_none()
    if chapter is None:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


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
    query = select(Chapter).join(Volume, Chapter.volume_id == Volume.id).where(
        Volume.project_id == UUID(str(project_id)),
    ).order_by(Volume.volume_idx, Chapter.chapter_idx, Chapter.id)
    if volume_id:
        query = query.where(Chapter.volume_id == UUID(str(volume_id)))
    if lightweight:
        query = query.options(defer(Chapter.content_text))

    result = await db.execute(query)
    chapters = result.scalars().all()
    project_settings: dict | None = None
    project_target_chapter_words: int | None = None
    project_result = await db.execute(select(Project).where(Project.id == UUID(str(project_id))))
    project = project_result.scalar_one_or_none()
    if project is not None and isinstance(project.settings_json, dict):
        project_settings = project.settings_json
        project_target_chapter_words = resolve_chapter_words(project_settings)
    if lightweight:
        # PR-OUTLINE-BUTTONS: keep outline_json so OutlineTree's per-chapter
        # "⊶大纲" button can render. Drop only content_text (the heavy field).
        return [
            {
                "id": str(c.id),
                "volume_id": str(c.volume_id),
                "title": c.title,
                "chapter_idx": c.chapter_idx,
                "global_idx": c.global_idx,
                "order_version": chapter_order_version(c),
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
    async with structure_write(db, project_id):
        return await _create_chapter(project_id, body, db)


async def _create_chapter(project_id, body, db):
    volume = await db.scalar(select(Volume.id).where(
        Volume.id == UUID(str(body.volume_id)), Volume.project_id == UUID(str(project_id)),
    ).execution_options(autoflush=False))
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    await require_free_index(db, Chapter, volume, body.chapter_idx)
    chapter = Chapter(
        volume_id=volume,
        title=body.title,
        chapter_idx=body.chapter_idx,
        global_idx=0,  # Finalized with all book positions before this transaction commits.
        outline_json=body.outline_json,
    )
    # Only override when provided so the column default (and the
    # project-level fallback in resolve_chapter_target_word_count) applies.
    if body.target_word_count is not None:
        chapter.target_word_count = body.target_word_count
    db.add(chapter)
    await db.flush()
    await reindex_chapter_structure(db, project_id)
    # 2026-11-21 修复(实测):`reindex_chapter_structure` 之后这一行 refresh 只回填了
    # 5 个键(chapter_idx/id/outline_json/updated_at/volume_id),title 等列仍是未加载
    # 状态;随后构造响应时访问 `chapter.title` 触发**同步 IO** → MissingGreenlet →
    # `structure_write` 兜底成 503 chapter_order_unconfirmed,**建章接口直接不可用**。
    # 改成显式重读整行(populate_existing),保证响应 payload 用到的列都已加载。
    chapter = await db.get(Chapter, chapter.id, populate_existing=True)
    project_result = await db.execute(select(Project).where(Project.id == UUID(str(project_id))))
    project = project_result.scalar_one_or_none()
    project_target_chapter_words = None
    if project is not None and isinstance(project.settings_json, dict):
        project_target_chapter_words = resolve_chapter_words(project.settings_json)
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
    chapter = await _project_chapter(db, project_id, chapter_id)
    project_result = await db.execute(select(Project).where(Project.id == UUID(str(project_id))))
    project = project_result.scalar_one_or_none()
    project_target_chapter_words = None
    if project is not None and isinstance(project.settings_json, dict):
        project_target_chapter_words = resolve_chapter_words(project.settings_json)
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
    if body.content_text is not None:
        async with author_manuscript_write(db):
            # 2026-11-17 审计:prepare_author_manuscript 在 expected 缺失时抛
            # chapter_content_precondition_required,此前没有任何 except 接它,
            # 于是带正文的通用 PUT 在"没先读保存稿"时返回 500;而同一语义在
            # /content 端点上是 428。这里统一成 428/409/400。
            try:
                change = await prepare_author_manuscript(
                    db, project_id, chapter_id, text=body.content_text,
                    expected=body.expected_content_text, force=bool(body.force))
            except ChapterContentError as exc:
                status = (
                    428 if exc.code == 'chapter_content_precondition_required'
                    else 409 if exc.code == 'chapter_edit_conflict'
                    else 400
                )
                raise HTTPException(status_code=status, detail=exc.detail) from None
            response = await _update_chapter(project_id, chapter_id, body, db, chapter=change.chapter)
            response.manuscript_receipt = ManuscriptReceipt(manuscript=manuscript_source(project_id, change.chapter),
                changed=change.changed, backup_version_id=change.backup_version_id)
            chapter_idx = change.chapter.chapter_idx
        if change.changed:
            from app.services.entity_dispatch import dispatch_entity_extraction
            dispatch_entity_extraction(project_id=project_id, chapter_id=chapter_id, chapter_idx=chapter_idx,
                                       caller='api.chapters.update_chapter')
        return response
    if body.chapter_idx is not None:
        async with structure_write(db, project_id):
            return await _update_chapter(project_id, chapter_id, body, db)
    return await _update_chapter(project_id, chapter_id, body, db)


async def _update_chapter(project_id, chapter_id, body, db, *, chapter=None):
    if chapter is None:
        chapter = await _project_chapter(db, project_id, chapter_id, lock=body.expected_content_text is not None or body.expected_outline_json is not None)
    try:
        check_chapter_replacement(chapter.content_text, body.content_text,
                                  expected=body.expected_content_text, force=bool(body.force))
    except ChapterContentError as exc:
        raise HTTPException(409 if exc.code == 'chapter_edit_conflict' else 400, detail=exc.detail) from None

    if body.expected_outline_json is not None and (chapter.outline_json or {}) != body.expected_outline_json:
        raise HTTPException(status_code=409, detail={
            "code": "chapter_outline_edit_conflict",
            "message": "细纲已在其他位置更新，未覆盖新稿。请保留当前编辑并重新核对。",
        })

    # Shared content-loss and version checks have run before any mutation.
    data = body.model_dump(exclude_unset=True)
    reorder = body.chapter_idx is not None and body.chapter_idx != chapter.chapter_idx
    if reorder:
        await require_free_index(db, Chapter, chapter.volume_id, body.chapter_idx, excluding=chapter.id)

    if body.title is not None:
        chapter.title = body.title
    if body.content_text is not None and "content_text" in data:
        chapter.content_text = body.content_text
        chapter.word_count = len(body.content_text)
    if body.outline_json is not None:
        from app.services.outline_authority import record_text_edit
        from app.services.outline_order import prepare_author_chapter
        chapter.outline_json = prepare_author_chapter(chapter.outline_json,
            record_text_edit(chapter.outline_json, body.outline_json), chapter)
    if body.status is not None:
        # 2026-11-17 审计:status 无任何校验。通用 PUT 可把空稿直接标
        # completed —— completed 会被认知台账/图谱/前文窗口当作"已有定稿",
        # 从而绕过质量门与 acceptance。completed 至少要求正文非空。
        if body.status == "completed" and not (chapter.content_text or "").strip():
            raise HTTPException(status_code=409, detail={
                "code": "chapter_completed_requires_content",
                "message": "空章节不能直接标记为已完成,请先写入正文。",
            })
        chapter.status = body.status
    if "target_word_count" in data and body.target_word_count is not None:
        chapter.target_word_count = body.target_word_count
    if "summary" in data:
        # Plain assignment; explicit null clears the stored summary.
        chapter.summary = body.summary
        from app.services.summary_provenance import summary_source_hash

        chapter.summary_source_hash = (
            summary_source_hash(chapter.title, chapter.content_text, body.summary)
            if body.summary else None
        )

    if reorder:
        chapter.chapter_idx = body.chapter_idx

    await db.flush()
    if reorder:
        await reindex_chapter_structure(db, project_id)
    await db.refresh(chapter)
    project_result = await db.execute(select(Project).where(Project.id == UUID(str(project_id))))
    project = project_result.scalar_one_or_none()
    project_target_chapter_words = None
    if project is not None and isinstance(project.settings_json, dict):
        project_target_chapter_words = resolve_chapter_words(project.settings_json)
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
    async with structure_write(db, project_id):
        chapter = await _project_chapter(db, project_id, chapter_id)
        await db.delete(chapter)
        await db.flush()
        await reindex_chapter_structure(db, project_id)


@router.post("/{chapter_id}/sync")
async def sync_chapter_edit(
    project_id: str,
    chapter_id: str,
    body: ChapterSyncRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Trigger incremental sync after user edits a chapter."""
    chapter = await _project_chapter(db, project_id, chapter_id)

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
    chapter = await _project_chapter(db, project_id, chapter_id)

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
