"""Version control and quality evaluation endpoints for chapters."""

from __future__ import annotations

from copy import deepcopy
from contextlib import asynccontextmanager
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, ChapterEvaluation, Project, Volume
from app.services.chapter_evaluator import ChapterEvaluator, EvaluationResult
from app.services.version_control import VersionConflict, VersionControlService, VersionNode, VersionUnavailable
from app.services.version_application import VersionApplicationPrecondition, prepare_version_application
from app.services.chapter_content import ChapterContentError
from app.services.chapter_order import ChapterOrderConflict, ChapterOrderUnavailable

router = APIRouter(prefix="/api/chapters/{chapter_id}", tags=["versions"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class VersionCreateRequest(BaseModel):
    content: str
    branch_name: str = Field(default='main', min_length=1, max_length=100)
    parent_id: UUID | None = None
    metadata: dict | None = None


class BranchCreateRequest(BaseModel):
    source_version_id: UUID
    branch_name: str = Field(min_length=1, max_length=100)


class VersionResponse(BaseModel):
    id: str
    chapter_id: str
    parent_id: str | None
    branch_name: str
    content_text: str
    content_diff: str
    word_count: int
    created_at: str
    is_active: bool
    source: str = 'unknown'
    metadata: dict

    model_config = {"from_attributes": True}


class DiffResponse(BaseModel):
    version_a: str
    version_b: str
    diff: str


class EvaluateRequest(BaseModel):
    previous_summary: str = ""
    style_profile: str = ""
    active_foreshadows: list[str] | None = None


class EvaluationResponse(BaseModel):
    id: str
    chapter_id: str
    plot_coherence: float
    character_consistency: float
    style_adherence: float
    narrative_pacing: float
    foreshadow_handling: float
    overall: float
    issues: list[dict]
    chapter_source: dict | None = None
    parse_failed: bool = False
    failure_reason: str | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_to_response(node: VersionNode) -> VersionResponse:
    """Convert a VersionNode to a VersionResponse."""
    return VersionResponse(
        id=node.id,
        chapter_id=node.chapter_id,
        parent_id=node.parent_id,
        branch_name=node.branch_name,
        content_text=node.content_text,
        content_diff=node.content_diff,
        word_count=node.word_count,
        created_at=node.created_at,
        is_active=node.is_active,
        source=node.source,
        metadata=node.metadata,
    )


async def _get_chapter_or_404(
    chapter_id: str, db: AsyncSession
) -> Chapter:
    """Fetch a chapter or raise 404."""
    chapter = await db.scalar(select(Chapter).join(Volume, Chapter.volume_id == Volume.id)
        .join(Project, Volume.project_id == Project.id)
        .where(Chapter.id == UUID(str(chapter_id)), Project.deleted_at.is_(None))
        .execution_options(populate_existing=True, autoflush=False))
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


@asynccontextmanager
async def version_write(db):
    """Only a confirmed commit can produce a successful version response."""
    try:
        async with asyncio.timeout(30):
            yield
            await db.commit()
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except HTTPException:
        await db.rollback()
        raise
    except VersionApplicationPrecondition as exc:
        await db.rollback()
        raise HTTPException(428, detail={'code': 'version_content_precondition_required', 'message': str(exc)}) from None
    except ChapterContentError as exc:
        await db.rollback()
        raise HTTPException(409 if exc.code == 'chapter_edit_conflict' else 400, detail=exc.detail) from None
    except (VersionUnavailable, ChapterOrderUnavailable) as exc:
        await db.rollback()
        raise HTTPException(404, detail={'code': 'version_unavailable', 'message': str(exc)}) from None
    except (VersionConflict, ChapterOrderConflict) as exc:
        await db.rollback()
        raise HTTPException(409, detail={'code': 'version_write_conflict', 'message': str(exc)}) from None
    except Exception as exc:
        await db.rollback()
        logger.warning('Version write not confirmed (%s)', type(exc).__name__)
        raise HTTPException(503, detail={'code': 'version_write_unconfirmed',
            'message': '尚未取得保存确认，请保留当前选择并刷新保存稿和版本列表核对。'}) from None


# ---------------------------------------------------------------------------
# Version endpoints
# ---------------------------------------------------------------------------


@router.get("/versions")
async def get_version_tree(
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[VersionResponse]:
    """Get the full version tree for a chapter."""
    await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    nodes = await svc.get_version_tree(chapter_id)
    return [_node_to_response(n) for n in nodes]


@router.post("/versions", status_code=201)
async def create_version(
    chapter_id: UUID,
    body: VersionCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> VersionResponse:
    """Create a new version for a chapter."""
    async with version_write(db):
        node = await VersionControlService(db).create_version(
            chapter_id=chapter_id, content=body.content, branch_name=body.branch_name,
            parent_id=body.parent_id, metadata=body.metadata,
        )
        return _node_to_response(node)


@router.post("/versions/{version_id}/activate")
async def activate_version(
    chapter_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Switch the active version for a chapter."""
    async with version_write(db):
        await VersionControlService(db).switch_active(chapter_id, version_id)
        return {"status": "ok", "active_version_id": str(version_id), "applied_to_chapter": False}


@router.post("/versions/branch", status_code=201)
async def create_branch(
    chapter_id: UUID,
    body: BranchCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> VersionResponse:
    """Create a new branch from an existing version."""
    async with version_write(db):
        node = await VersionControlService(db).create_branch(
            chapter_id=chapter_id,
            source_version_id=str(body.source_version_id),
            branch_name=body.branch_name,
        )
        return _node_to_response(node)


@router.get("/versions/diff")
async def get_diff(
    chapter_id: UUID,
    a: UUID,
    b: UUID,
    db: AsyncSession = Depends(get_db),
) -> DiffResponse:
    """Get a unified diff between two versions."""
    await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    try:
        diff_text = await svc.get_diff(chapter_id, a, b)
    except VersionUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return DiffResponse(version_a=str(a), version_b=str(b), diff=diff_text)


class VersionSourceResponse(BaseModel):
    chapter_id: str
    project_id: str
    title: str
    content_text: str
    word_count: int


@router.get('/versions/source', response_model=VersionSourceResponse)
async def get_version_source(chapter_id: UUID, db: AsyncSession = Depends(get_db)):
    chapter = await _get_chapter_or_404(chapter_id, db)
    pid = await db.scalar(select(Volume.project_id).where(Volume.id == chapter.volume_id))
    content = chapter.content_text or ''
    return VersionSourceResponse(chapter_id=str(chapter.id), project_id=str(pid), title=chapter.title or '',
                                 content_text=content, word_count=len(content))


class VersionRestoreRequest(BaseModel):
    expected_content_text: str | None = None
    force: bool = False


class RollbackResponse(BaseModel):
    status: str
    chapter_id: str
    active_version_id: str
    word_count: int
    content_text: str
    backup_version_id: str | None
    changed: bool


@router.post(
    "/versions/{version_id}/rollback",
    response_model=RollbackResponse,
)
async def rollback_version(
    chapter_id: UUID,
    version_id: UUID,
    body: VersionRestoreRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> RollbackResponse:
    """Restore a version only against the exact saved body the author saw."""
    request = body or VersionRestoreRequest()
    async with version_write(db):
        applied = await prepare_version_application(db, chapter_id, version_id,
            expected_content_text=request.expected_content_text, force=request.force)
        response = RollbackResponse(
            status='ok', chapter_id=str(chapter_id), active_version_id=str(version_id),
            word_count=len(applied.content_text), content_text=applied.content_text,
            backup_version_id=str(applied.backup_version_id) if applied.backup_version_id else None,
            changed=applied.changed,
        )
    if applied.changed:
        from app.services.entity_dispatch import dispatch_entity_extraction
        dispatch_entity_extraction(project_id=applied.project_id, chapter_id=applied.chapter_id,
                                   chapter_idx=applied.chapter_idx, caller='api.versions.rollback')
    return response


# ---------------------------------------------------------------------------
# Evaluation endpoint
# ---------------------------------------------------------------------------


@router.post("/evaluate")
async def evaluate_chapter(
    chapter_id: str,
    body: EvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> EvaluationResponse:
    """Run quality evaluation on a chapter using LLM-as-a-Judge."""
    chapter = await _get_chapter_or_404(chapter_id, db)

    if not chapter.content_text or not chapter.content_text.strip():
        raise HTTPException(
            status_code=400, detail="Chapter has no content to evaluate"
        )

    chapter_text = chapter.content_text
    chapter_outline = deepcopy(chapter.outline_json or {})
    evaluation_project_id = None

    # Load source-bound cognition before scoring; no empty fallback on error.
    cognition_ledger_text = ""
    try:
        from app.models.project import Volume
        from app.services import character_cognition as _cognition

        volume = await db.get(Volume, chapter.volume_id)
        if volume is None:
            raise ValueError('evaluation_project_missing')
        evaluation_project_id = str(volume.project_id)
        cognition_ledger_text = await _cognition.load_prompt_context(
            db, volume.project_id, before_chapter_id=chapter.id,
            chapter_outline=chapter_outline, chapter_text=chapter_text,
            defer_retrieval=True,
        )
    except Exception as _cog_err:
        import logging
        logging.getLogger(__name__).warning(
            "Manual evaluate: cognition ledger load failed (%s)", type(_cog_err).__name__
        )
        await db.rollback()
        from app.services.generation_errors import cognition_unavailable_message
        raise HTTPException(status_code=503, detail=cognition_unavailable_message(_cog_err)) from _cog_err

    from app.services.evaluation_neighbors import load_evaluation_neighbors
    neighbors = await load_evaluation_neighbors(db, chapter_id)
    previous_context = neighbors.previous_context
    if body.previous_summary:
        previous_context += "\n\n作者提供的前文说明：\n" + body.previous_summary
    # End the read transaction before network IO; rollback expires ORM rows,
    # so only copied scalar/JSON inputs may be used by the evaluator below.
    await db.rollback()
    from app.services.cognition_retrieval import resolve_prompt_context
    from app.services.context_errors import RequiredContextUnavailable
    try:
        cognition_ledger_text = await resolve_prompt_context(cognition_ledger_text, db=db, extra_context=previous_context)
    except RequiredContextUnavailable as exc:
        from app.services.generation_errors import cognition_unavailable_message
        raise HTTPException(status_code=503, detail=cognition_unavailable_message(exc)) from exc
    evaluator = ChapterEvaluator()
    result: EvaluationResult = await evaluator.evaluate(
        chapter_text=chapter_text,
        project_id=evaluation_project_id,
        chapter_id=chapter_id,
        chapter_outline=chapter_outline,
        previous_summary=previous_context,
        following_context=neighbors.following_context,
        style_profile=body.style_profile,
        active_foreshadows=body.active_foreshadows,
        cognition_ledger_text=cognition_ledger_text,
    )

    # Persist evaluation result
    await _get_chapter_or_404(chapter_id, db)
    evaluation = ChapterEvaluation(
        chapter_id=chapter_id,
        plot_coherence=result.plot_coherence,
        character_consistency=result.character_consistency,
        style_adherence=result.style_adherence,
        narrative_pacing=result.narrative_pacing,
        foreshadow_handling=result.foreshadow_handling,
        overall=result.overall,
        issues_json=result.issues,
        source_json=getattr(result, "chapter_source", None),
    )
    db.add(evaluation)
    await db.flush()
    await db.refresh(evaluation)

    return EvaluationResponse(
        id=str(evaluation.id),
        chapter_id=str(evaluation.chapter_id),
        plot_coherence=evaluation.plot_coherence,
        character_consistency=evaluation.character_consistency,
        style_adherence=evaluation.style_adherence,
        narrative_pacing=evaluation.narrative_pacing,
        foreshadow_handling=evaluation.foreshadow_handling,
        overall=evaluation.overall,
        issues=evaluation.issues_json or [],
        chapter_source=evaluation.source_json,
        parse_failed=result.parse_failed,
        failure_reason=result.failure_reason,
    )
