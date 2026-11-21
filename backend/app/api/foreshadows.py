"""Foreshadow management endpoints."""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from neo4j import AsyncDriver
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import neo4j as neo4j_db
from app.db.session import get_db
from app.models.project import Foreshadow
from app.services.foreshadow_provenance import REVIEW_SOURCE_FIELDS, foreshadow_source_hash
from app.services.foreshadow_write_contract import make_write_command

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/foreshadows",
    tags=["foreshadows"],
)

from app.api.foreshadow_sources import router as source_router
router.include_router(source_router)


async def get_foreshadow_driver() -> AsyncDriver | None:
    """Local lifecycle edits remain usable without an initialized graph driver."""
    return neo4j_db._driver


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


ForeshadowStatus = Literal["planned", "pending", "planted", "ripening", "ready", "resolved"]


class ForeshadowCreateRequest(BaseModel):
    type: str = Field(..., max_length=20, description="e.g. plot, character, worldbuilding, mystery")
    description: str = Field(..., description="What the foreshadow is about")
    planted_chapter: int = Field(..., ge=0, le=2147483647, strict=True, description="Chapter index where the foreshadow was planted")
    resolve_conditions: list[str] = Field(
        default_factory=list,
        description="Narrative conditions that would make resolution natural",
    )
    resolution_blueprint: dict[str, Any] | None = Field(
        None, description="Optional guidance on how to resolve"
    )

    @field_validator("type", "description")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("伏笔类型和描述不能为空")
        return value


class ForeshadowUpdateRequest(BaseModel):
    type: str | None = Field(None, max_length=20)
    description: str | None = None
    planted_chapter: int | None = Field(None, ge=0, le=2147483647, strict=True)
    resolve_conditions: list[str] | None = None
    resolution_blueprint: dict[str, Any] | None = None
    narrative_proximity: float | None = Field(None, ge=0.0, le=1.0)
    status: ForeshadowStatus | None = Field(
        None, description="Changing to resolved requires resolved_chapter; other statuses clear it"
    )
    resolved_chapter: int | None = Field(
        None, ge=0, le=2147483647, strict=True, description="Must be supplied together with status=resolved"
    )

    @field_validator("type", "description", "planted_chapter", "status")
    @classmethod
    def required_patch_value(cls, value: Any) -> Any:
        # Defaults are not validated: omission preserves the existing field,
        # explicit null must not delete a required PG/graph property.
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("该伏笔字段不能清空；不修改时请省略字段")
        return value

    @model_validator(mode="after")
    def coherent_resolution_patch(self) -> ForeshadowUpdateRequest:
        # Treat status and resolution chapter as one write, without a stale
        # read/merge against either store. Descriptive edits remain partial.
        if "status" not in self.model_fields_set:
            if "resolved_chapter" in self.model_fields_set:
                raise ValueError("修改兑现章号时请同时提供伏笔状态")
            return self
        if self.status == "resolved":
            if self.resolved_chapter is None:
                raise ValueError("标记已兑现时必须提供兑现章号")
        else:
            if self.resolved_chapter is not None:
                raise ValueError("未兑现状态不能保留兑现章号")
            # Assignment marks the field as set, so exclude_unset retains the
            # explicit clear in both the PG update and Neo4j SET statement.
            self.resolved_chapter = None
        return self


class ForeshadowResolveRequest(BaseModel):
    resolved_chapter: int = Field(..., ge=0, le=2147483647, strict=True, description="Chapter where foreshadow was resolved")


class ForeshadowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    type: str
    description: str
    planted_chapter: int
    resolve_conditions_json: list[Any] | dict[str, Any] | None = None
    resolution_blueprint_json: dict[str, Any] | None = None
    narrative_proximity: float | None = None
    status: str
    resolved_chapter: int | None = None
    created_at: Any
    source: str | None = None
    source_state: Literal['current', 'changed', 'unavailable', 'unknown'] = 'unknown'
    source_kind: Literal['prose', 'record'] = 'record'
    source_label: str = '来源未核实'
    source_evidence: str | None = None
    source_origin: Literal['author', 'candidate'] | None = None
    source_link_id: UUID | None = None
    source_chapter_id: UUID | None = None
    source_global_idx: int | None = None

    @computed_field
    @property
    def version(self) -> str:
        return foreshadow_source_hash({field: getattr(self, field) for field in REVIEW_SOURCE_FIELDS})


class ForeshadowListResponse(BaseModel):
    foreshadows: list[ForeshadowResponse]
    total: int
    # Deprecated: mixed historical numbers cannot establish literary debt.
    debt: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class ForeshadowReviewResponse(BaseModel):
    id: UUID
    chapter_id: UUID
    foreshadow_id: UUID
    source_snapshot: dict[str, Any]
    evidence: str
    evidence_start: int
    evidence_end: int
    reason: str
    status: str
    created_at: Any
    handled_at: Any = None
    handled_by: str | None = None
    confirmation_status: str | None = None
    source_state: Literal["current", "accepted_current", "changed", "unavailable", "unknown", "projection_current"]


class ForeshadowReviewsResponse(BaseModel):
    reviews: list[ForeshadowReviewResponse]
    has_more: bool


class ForeshadowReviewJobResponse(BaseModel):
    id: UUID
    chapter_id: UUID
    chapter_title: str
    global_idx: int | None
    status: str
    attempts: int
    discovery_count: int | None = None
    rescan_discovery: bool = False
    discovery_run_id: UUID | None = None
    updated_at: Any


class ForeshadowReviewJobsResponse(BaseModel):
    jobs: list[ForeshadowReviewJobResponse]
    has_more: bool


@router.get("/review-jobs", response_model=ForeshadowReviewJobsResponse)
async def list_foreshadow_review_jobs(
    project_id: UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    from app.services.foreshadow_review_jobs import list_review_jobs
    return await list_review_jobs(db, project_id, limit, offset)


class ForeshadowReviewCreateRequest(BaseModel):
    chapter_id: UUID
    expected_content: str = Field(..., min_length=1, strict=True)
    request_id: UUID | None = None
    rescan_discovery: bool = Field(False, strict=True)

    @model_validator(mode='after')
    def require_rescan_request_id(self):
        if self.rescan_discovery and self.request_id is None:
            raise ValueError('重新识别线索需要固定请求编号')
        return self


@router.post("/review-jobs")
async def create_foreshadow_review_job(
    project_id: UUID, body: ForeshadowReviewCreateRequest, db: AsyncSession = Depends(get_db),
):
    from app.services.foreshadow_review_jobs import create_review_job, ReviewSourceNotReady
    from app.services.foreshadow_review_requests import ReviewRequestConflict
    try:
        result = await create_review_job(db, project_id, body.chapter_id, body.expected_content,
            request_id=body.request_id, rescan_discovery=body.rescan_discovery)
        await db.commit()
    except (ReviewSourceNotReady, ReviewRequestConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        logger.exception('Foreshadow review submission was not confirmed')
        raise HTTPException(status_code=503, detail='尚未获得核查提交确认，请保留原请求重试') from None
    return result


@router.post("/review-jobs/{job_id}/retry")
async def retry_foreshadow_review_job(project_id: UUID, job_id: UUID, db: AsyncSession = Depends(get_db)):
    from app.services.foreshadow_review_jobs import retry_review_job, ReviewJobNotFound, ReviewJobNotRetryable
    try:
        await retry_review_job(db, project_id, job_id)
        await db.commit()
    except ReviewJobNotFound:
        raise HTTPException(status_code=404, detail="未找到该核查任务")
    except ReviewJobNotRetryable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"id": str(job_id), "status": "pending"}


@router.get("/reviews", response_model=ForeshadowReviewsResponse)
async def list_foreshadow_reviews(
    project_id: UUID,
    status: Literal["pending", "confirming", "accepted", "dismissed"] | None = Query("pending"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    from app.services.foreshadow_review_reader import list_resolution_reviews
    return await list_resolution_reviews(db, project_id, status, limit, offset)


@router.post("/reviews/{review_id}/dismiss")
async def dismiss_foreshadow_review(
    project_id: UUID, review_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
):
    from app.services.foreshadow_review_actions import dismiss_review, ReviewNotFound, ReviewAlreadyHandled
    try:
        await dismiss_review(db, project_id, review_id, actor=getattr(request.state, 'auth_username', None))
        # Confirm durability before acknowledging the UI; do not rely on
        # a yield dependency committing after the response is sent.
        await db.commit()
    except ReviewNotFound:
        raise HTTPException(status_code=404, detail="未找到该核查建议")
    except ReviewAlreadyHandled:
        raise HTTPException(status_code=409, detail="该建议已被其他操作处理，请刷新列表")
    return {"id": str(review_id), "status": "dismissed"}


@router.post("/reviews/{review_id}/accept")
async def accept_foreshadow_review(
    project_id: UUID, review_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
):
    from app.services.foreshadow_review_actions import (
        accept_review, ReviewNotFound, ReviewAlreadyHandled, ReviewSourceChanged, ReviewSourceRequiresSync,
    )
    try:
        result = await accept_review(db, project_id, review_id, actor=getattr(request.state, 'auth_username', None))
        await db.commit()
    except ReviewNotFound:
        raise HTTPException(status_code=404, detail="未找到该核查建议")
    except ReviewAlreadyHandled:
        raise HTTPException(status_code=409, detail="该建议已被其他操作处理，请刷新列表")
    except ReviewSourceChanged:
        raise HTTPException(status_code=409, detail="正文或伏笔来源已变化，请重新核查后确认")
    except ReviewSourceRequiresSync:
        raise HTTPException(status_code=409, detail="该伏笔的来源同步尚未核验，暂不能通过此建议确认")
    if result == "confirming":
        return JSONResponse(status_code=202, content={"id": str(review_id), "status": "confirming"})
    return {"id": str(review_id), "status": "accepted"}


class ForeshadowDiscoveryResponse(BaseModel):
    id: UUID
    chapter_id: UUID
    description: str
    type: str
    evidence: str
    evidence_start: int
    evidence_end: int
    reason: str
    scan_reason: str | None = None
    reused_in_scan: bool = False
    source_snapshot: dict
    source_state: Literal['current', 'changed', 'unavailable', 'unknown']
    status: Literal['pending', 'accepted', 'dismissed']
    foreshadow_id: UUID | None
    record_present: bool | None
    handled_at: Any = None
    handled_by: str | None = None
    created_at: Any


class ForeshadowDiscoveriesResponse(BaseModel):
    discoveries: list[ForeshadowDiscoveryResponse]
    has_more: bool


class ForeshadowDiscoveryRunResponse(BaseModel):
    id: UUID
    chapter_id: UUID
    chapter_title: str | None
    source_snapshot: dict
    source_state: Literal['current', 'changed', 'unavailable', 'unknown']
    result_state: Literal['complete', 'unknown']
    scan_revision: int
    model_name: str | None = None
    candidate_count: int | None
    created_at: Any


class ForeshadowDiscoveryRunsResponse(BaseModel):
    runs: list[ForeshadowDiscoveryRunResponse]
    has_more: bool


@router.get('/discovery-runs', response_model=ForeshadowDiscoveryRunsResponse)
async def list_foreshadow_discovery_runs(
    project_id: UUID, chapter_id: UUID | None = Query(None), limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db),
):
    from app.services.foreshadow_discovery_reader import list_discovery_runs
    return await list_discovery_runs(db, project_id, chapter_id=chapter_id, limit=limit, offset=offset)


@router.get('/discoveries', response_model=ForeshadowDiscoveriesResponse)
async def list_foreshadow_discoveries(
    project_id: UUID, status: Literal['pending', 'accepted', 'dismissed', 'all'] | None = Query('pending'),
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db),
    run_id: UUID | None = Query(None),
):
    from app.services.foreshadow_discovery_actions import list_discoveries
    from app.services.foreshadow_discovery_reader import DiscoveryRunNotFound
    try:
        return await list_discoveries(db, project_id, None if status == 'all' else status, limit, offset, run_id=run_id)
    except DiscoveryRunNotFound:
        raise HTTPException(status_code=404, detail='未找到该轮线索识别记录') from None


async def _decide_discovery(db, project_id, candidate_id, request, *, accept):
    from app.services.foreshadow_discovery_actions import (
        accept_discovery, dismiss_discovery, DiscoveryNotFound, DiscoveryAlreadyHandled,
        DiscoverySourceChanged, DiscoveryWriteConflict, DiscoveryProjectUnavailable,
    )
    try:
        action = accept_discovery if accept else dismiss_discovery
        fid = await action(db, project_id, candidate_id, actor=getattr(request.state, 'auth_username', None))
        await db.commit()
    except DiscoveryNotFound:
        raise HTTPException(404, detail='未找到该线索候选') from None
    except DiscoveryAlreadyHandled:
        raise HTTPException(409, detail='该线索已被其他操作处理，请刷新核对') from None
    except DiscoverySourceChanged:
        raise HTTPException(409, detail='正文或章节位置已变化，请重新核查后登记') from None
    except DiscoveryWriteConflict:
        raise HTTPException(409, detail='登记记录存在冲突，请刷新核对') from None
    except DiscoveryProjectUnavailable:
        raise HTTPException(409, detail='作品已不可编辑，请恢复作品后再登记') from None
    except Exception:
        logger.exception('Foreshadow discovery decision was not confirmed')
        raise HTTPException(503, detail='尚未获得处理完成确认，请刷新核对后重试同一候选') from None
    return {'id': str(candidate_id), 'status': 'accepted' if accept else 'dismissed',
            'foreshadow_id': str(fid) if accept else None}


@router.post('/discoveries/{candidate_id}/accept')
async def accept_foreshadow_discovery(project_id: UUID, candidate_id: UUID, request: Request,
                                     db: AsyncSession = Depends(get_db)):
    return await _decide_discovery(db, project_id, candidate_id, request, accept=True)


@router.post('/discoveries/{candidate_id}/dismiss')
async def dismiss_foreshadow_discovery(project_id: UUID, candidate_id: UUID, request: Request,
                                      db: AsyncSession = Depends(get_db)):
    return await _decide_discovery(db, project_id, candidate_id, request, accept=False)


@router.get("", response_model=ForeshadowListResponse)
async def list_foreshadows(
    project_id: UUID,
    status: str | None = Query(None, description="Filter by status: planned, planted, ripening, ready, resolved"),
    db: AsyncSession = Depends(get_db),
) -> ForeshadowListResponse:
    """List all foreshadows for a project, optionally filtered by status."""
    query = select(Foreshadow).where(
        Foreshadow.project_id == project_id
    )
    if status is not None:
        query = query.where(Foreshadow.status == status)
    query = query.order_by(Foreshadow.planted_chapter)

    result = await db.execute(query)
    foreshadows = list(result.scalars().all())

    from app.services.foreshadow_sources import inspect_foreshadow_sources
    records = [{column.key: getattr(row, column.key) for column in Foreshadow.__table__.columns}
               for row in foreshadows]
    try:
        sources = await inspect_foreshadow_sources(db, project_id, records)
    except Exception as exc:
        logger.warning("Foreshadow source list unavailable: %s", type(exc).__name__)
        raise HTTPException(503, detail="伏笔来源读取失败，请刷新重试") from None
    return ForeshadowListResponse(
        foreshadows=[ForeshadowResponse.model_validate({**row, **sources[str(row['id'])]}) for row in records],
        total=len(records),
    )


class ForeshadowPlanResponse(BaseModel):
    id: UUID
    source_kind: Literal['volume_outline', 'chapter_outline']
    source_id: UUID
    volume_id: UUID
    volume_idx: int
    chapter_id: UUID | None
    chapter_idx: int | None
    planted_chapter: int | None
    description: str
    type: str
    hint: str
    status: Literal['planned']
    source_label: str
    is_confirmed: bool | None


class ForeshadowPlansResponse(BaseModel):
    plans: list[ForeshadowPlanResponse]
    total: int
    has_more: bool
    unavailable_sources: int


@router.get('/plans', response_model=ForeshadowPlansResponse)
async def list_foreshadow_plans(
    project_id: UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    from app.services.foreshadow_plans import read_foreshadow_plans
    try:
        result = await read_foreshadow_plans(db, project_id)
    except Exception as exc:
        logger.warning("Foreshadow plans unavailable: %s", type(exc).__name__)
        raise HTTPException(503, detail="大纲伏笔计划读取失败，请刷新重试") from None
    plans = result['plans']
    return {**result, 'plans': plans[offset:offset + limit], 'total': len(plans),
            'has_more': offset + limit < len(plans)}


async def _run_author_write(db, driver, command):
    from app.services.entity_materialization_lock import MaterializationBusy
    from app.services.foreshadow_write_contract import (
        ForeshadowWriteConflict, ForeshadowProjectUnavailable,
        ForeshadowSyncPending, ForeshadowWriteUnconfirmed,
    )
    from app.services.foreshadow_writes import apply_author_write
    try:
        row = await apply_author_write(db, driver, command)
        return ForeshadowResponse.model_validate(row) if row is not None else None
    except ForeshadowProjectUnavailable:
        raise HTTPException(404, detail={'code': 'project_unavailable', 'message': '作品不存在或已移入回收站。'}) from None
    except ForeshadowWriteConflict as exc:
        messages = {
            'source_changed': '伏笔已被修改，未覆盖新版本。请保留草稿并刷新核对。',
            'source_missing': '伏笔已删除或来源不可用，未重新创建记录。',
            'unknown_source': '这条旧伏笔的存储来源尚未确认，请先完成资料同步。',
            'invalid_source': '伏笔来源记录的格式异常，未覆盖原数据，请先核对。',
            'already_resolved': '伏笔已有兑现记录，请刷新核对。',
            'already_exists': '这条伏笔已存在，请刷新核对。',
            'changed_after_write': '操作曾经完成，但记录随后又有变化，请刷新核对。',
            'request_id_reused': '请求编号已用于其他内容，未重复执行。请刷新核对。',
        }
        code = exc.code if exc.code in messages else 'foreshadow_write_conflict'
        raise HTTPException(409, detail={'code': code, 'message': messages.get(exc.code, '操作记录发生冲突，请保留输入并刷新核对。')}) from None
    except MaterializationBusy:
        raise HTTPException(503, detail={'code': 'materialization_busy', 'message': '作品资料正在同步，请稍后重试。'}) from None
    except ForeshadowSyncPending:
        raise HTTPException(503, detail={'code': 'foreshadow_sync_pending', 'message': '操作已记入图谱，列表同步尚未完成；系统会继续同步，也可保留当前输入重试。'}) from None
    except ForeshadowWriteUnconfirmed:
        raise HTTPException(503, detail={'code': 'foreshadow_write_unconfirmed', 'message': '尚未获得操作完成确认。请保留当前输入重试，系统会核对原请求。'}) from None


@router.post("", response_model=ForeshadowResponse, status_code=201)
async def create_foreshadow(
    project_id: UUID,
    body: ForeshadowCreateRequest,
    neo4j: AsyncDriver | None = Depends(get_foreshadow_driver),
    db: AsyncSession = Depends(get_db),
    request_id: UUID = Header(..., alias='Idempotency-Key'),
) -> ForeshadowResponse:
    changes = dict(type=body.type.strip(), description=body.description.strip(),
        planted_chapter=body.planted_chapter, resolve_conditions_json=body.resolve_conditions,
        resolution_blueprint_json=body.resolution_blueprint or {}, narrative_proximity=0.0,
        status='planted', resolved_chapter=None)
    # A lost create response is retried with the same clue identity.
    command = make_write_command(request_id, project_id, request_id, 'create', None, changes)
    return await _run_author_write(db, neo4j, command)


@router.put("/{foreshadow_id}", response_model=ForeshadowResponse)
async def update_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    body: ForeshadowUpdateRequest,
    neo4j: AsyncDriver | None = Depends(get_foreshadow_driver),
    db: AsyncSession = Depends(get_db),
    request_id: UUID = Header(..., alias='Idempotency-Key'),
    expected_version: str = Header(..., alias='If-Match', pattern='^[0-9a-f]{64}$'),
) -> ForeshadowResponse:
    mapping = {'resolve_conditions': 'resolve_conditions_json', 'resolution_blueprint': 'resolution_blueprint_json'}
    changes = {mapping.get(key, key): value for key, value in body.model_dump(exclude_unset=True).items()}
    return await _run_author_write(db, neo4j, make_write_command(
        request_id, project_id, foreshadow_id, 'update', expected_version, changes))


@router.delete("/{foreshadow_id}", status_code=204)
async def delete_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    neo4j: AsyncDriver | None = Depends(get_foreshadow_driver),
    db: AsyncSession = Depends(get_db),
    request_id: UUID = Header(..., alias='Idempotency-Key'),
    expected_version: str = Header(..., alias='If-Match', pattern='^[0-9a-f]{64}$'),
) -> None:
    await _run_author_write(db, neo4j, make_write_command(
        request_id, project_id, foreshadow_id, 'delete', expected_version, {}))


@router.post("/{foreshadow_id}/resolve", response_model=ForeshadowResponse)
async def resolve_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    body: ForeshadowResolveRequest,
    neo4j: AsyncDriver | None = Depends(get_foreshadow_driver),
    db: AsyncSession = Depends(get_db),
    request_id: UUID = Header(..., alias='Idempotency-Key'),
    expected_version: str = Header(..., alias='If-Match', pattern='^[0-9a-f]{64}$'),
) -> ForeshadowResponse:
    return await _run_author_write(db, neo4j, make_write_command(
        request_id, project_id, foreshadow_id, 'resolve', expected_version,
        dict(status='resolved', resolved_chapter=body.resolved_chapter, narrative_proximity=1.0)))
