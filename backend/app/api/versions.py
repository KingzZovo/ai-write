"""Version control and quality evaluation endpoints for chapters."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, ChapterEvaluation
from app.services.chapter_evaluator import ChapterEvaluator, EvaluationResult
from app.services.version_control import VersionControlService, VersionNode

router = APIRouter(prefix="/api/chapters/{chapter_id}", tags=["versions"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class VersionCreateRequest(BaseModel):
    content: str
    branch_name: str = "main"
    parent_id: str | None = None
    metadata: dict | None = None


class BranchCreateRequest(BaseModel):
    source_version_id: str
    branch_name: str


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
        metadata=node.metadata,
    )


async def _get_chapter_or_404(
    chapter_id: str, db: AsyncSession
) -> Chapter:
    """Fetch a chapter or raise 404."""
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


# ---------------------------------------------------------------------------
# Version endpoints
# ---------------------------------------------------------------------------


@router.get("/versions")
async def get_version_tree(
    chapter_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[VersionResponse]:
    """Get the full version tree for a chapter."""
    await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    nodes = await svc.get_version_tree(chapter_id)
    return [_node_to_response(n) for n in nodes]


@router.post("/versions", status_code=201)
async def create_version(
    chapter_id: str,
    body: VersionCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> VersionResponse:
    """Create a new version for a chapter."""
    await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    node = await svc.create_version(
        chapter_id=chapter_id,
        content=body.content,
        branch_name=body.branch_name,
        parent_id=body.parent_id,
        metadata=body.metadata,
    )
    return _node_to_response(node)


@router.post("/versions/{version_id}/activate")
async def activate_version(
    chapter_id: str,
    version_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Switch the active version for a chapter."""
    await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    try:
        await svc.switch_active(chapter_id, version_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "ok", "active_version_id": version_id}


@router.post("/versions/branch", status_code=201)
async def create_branch(
    chapter_id: str,
    body: BranchCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> VersionResponse:
    """Create a new branch from an existing version."""
    await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    try:
        node = await svc.create_branch(
            chapter_id=chapter_id,
            source_version_id=body.source_version_id,
            branch_name=body.branch_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _node_to_response(node)


@router.get("/versions/diff")
async def get_diff(
    chapter_id: str,
    a: str,
    b: str,
    db: AsyncSession = Depends(get_db),
) -> DiffResponse:
    """Get a unified diff between two versions."""
    await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    try:
        diff_text = await svc.get_diff(a, b)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return DiffResponse(version_a=a, version_b=b, diff=diff_text)


class RollbackResponse(BaseModel):
    status: str
    chapter_id: str
    active_version_id: str
    word_count: int


@router.post(
    "/versions/{version_id}/rollback",
    response_model=RollbackResponse,
)
async def rollback_version(
    chapter_id: str,
    version_id: str,
    db: AsyncSession = Depends(get_db),
) -> RollbackResponse:
    """Rollback a chapter to a previous version.

    Activates the target version (via VersionControlService.switch_active)
    and syncs the chapter's ``content_text`` + ``word_count`` to the
    activated version so downstream consumers (ContextPack, vector store
    sync, exports) see the rolled-back content as the canonical chapter.
    """
    chapter = await _get_chapter_or_404(chapter_id, db)
    svc = VersionControlService(db)
    try:
        await svc.switch_active(chapter_id, version_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    active = await svc.get_active_version(chapter_id)
    if active is None:
        raise HTTPException(
            status_code=500, detail="No active version after rollback"
        )
    chapter.content_text = active.content_text
    chapter.word_count = active.word_count
    await db.flush()
    return RollbackResponse(
        status="ok",
        chapter_id=chapter_id,
        active_version_id=active.id,
        word_count=active.word_count,
    )


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

    evaluator = ChapterEvaluator()
    result: EvaluationResult = await evaluator.evaluate(
        chapter_text=chapter.content_text,
        chapter_outline=chapter.outline_json or {},
        previous_summary=body.previous_summary,
        style_profile=body.style_profile,
        active_foreshadows=body.active_foreshadows,
    )

    # Persist evaluation result
    evaluation = ChapterEvaluation(
        chapter_id=chapter_id,
        plot_coherence=result.plot_coherence,
        character_consistency=result.character_consistency,
        style_adherence=result.style_adherence,
        narrative_pacing=result.narrative_pacing,
        foreshadow_handling=result.foreshadow_handling,
        overall=result.overall,
        issues_json=result.issues,
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
    )
