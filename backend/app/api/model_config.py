"""LLM endpoint and task routing configuration endpoints.

Allows users to configure API endpoints and assign them to task types
from the frontend UI instead of .env files.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.project import LLMEndpoint, ModelConfig as ModelConfigModel
from app.services.model_router import reset_model_router

router = APIRouter(prefix="/api/model-config", tags=["model-config"])

VALID_PROVIDER_TYPES = {"anthropic", "openai", "openai_compatible"}
VALID_TASK_TYPES = {
    "generation",
    "polishing",
    "outline",
    "extraction",
    "evaluation",
    "summary",
    "embedding",
}


# =========================================================================
# Schemas
# =========================================================================


def _mask_key(key: str) -> str:
    """Mask an API key, showing only last 4 chars."""
    if not key or len(key) <= 4:
        return "****"
    return "*" * (len(key) - 4) + key[-4:]


class EndpointCreate(BaseModel):
    name: str = Field(..., max_length=200)
    provider_type: str = Field(..., description="anthropic | openai | openai_compatible")
    base_url: str = Field("", max_length=1000)
    api_key: str = Field("", max_length=500)
    default_model: str = Field(..., max_length=200)


class EndpointUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    enabled: int | None = None


class EndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    provider_type: str
    base_url: str
    api_key_masked: str
    default_model: str
    enabled: int
    last_test_ok: int
    last_test_latency: float | None
    created_at: Any

    @classmethod
    def from_orm_endpoint(cls, ep: LLMEndpoint) -> "EndpointResponse":
        return cls(
            id=ep.id,
            name=ep.name,
            provider_type=ep.provider_type,
            base_url=ep.base_url or "",
            api_key_masked=_mask_key(ep.api_key or ""),
            default_model=ep.default_model,
            enabled=ep.enabled,
            last_test_ok=getattr(ep, "last_test_ok", 0) or 0,
            last_test_latency=getattr(ep, "last_test_latency", None),
            created_at=ep.created_at,
        )


class EndpointListResponse(BaseModel):
    endpoints: list[EndpointResponse]
    total: int


class TaskConfigUpdate(BaseModel):
    endpoint_id: UUID | None = None
    model_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class TaskConfigResponse(BaseModel):
    task_type: str
    endpoint: EndpointResponse | None = None
    model_name: str
    temperature: float
    max_tokens: int


class TaskConfigListResponse(BaseModel):
    tasks: list[TaskConfigResponse]


class TestResult(BaseModel):
    success: bool
    message: str
    latency_ms: float | None = None


class PresetResponse(BaseModel):
    name: str
    description: str
    tasks: dict[str, dict[str, Any]]


# =========================================================================
# LLM Endpoint CRUD
# =========================================================================


@router.get("/endpoints", response_model=EndpointListResponse)
async def list_endpoints(
    db: AsyncSession = Depends(get_db),
) -> EndpointListResponse:
    """List all configured LLM endpoints (API keys masked)."""
    result = await db.execute(
        select(LLMEndpoint).order_by(LLMEndpoint.created_at)
    )
    endpoints = list(result.scalars().all())
    return EndpointListResponse(
        endpoints=[EndpointResponse.from_orm_endpoint(ep) for ep in endpoints],
        total=len(endpoints),
    )


@router.post("/endpoints", response_model=EndpointResponse, status_code=201)
async def create_endpoint(
    body: EndpointCreate,
    db: AsyncSession = Depends(get_db),
) -> EndpointResponse:
    """Create a new LLM endpoint configuration."""
    if body.provider_type not in VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider_type. Must be one of: {', '.join(VALID_PROVIDER_TYPES)}",
        )
    if body.provider_type == "openai_compatible" and not body.base_url:
        raise HTTPException(
            status_code=400,
            detail="base_url is required for openai_compatible provider",
        )

    endpoint = LLMEndpoint(
        name=body.name,
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=body.api_key,
        default_model=body.default_model,
    )
    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    reset_model_router()
    return EndpointResponse.from_orm_endpoint(endpoint)


@router.put("/endpoints/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(
    endpoint_id: UUID,
    body: EndpointUpdate,
    db: AsyncSession = Depends(get_db),
) -> EndpointResponse:
    """Update an existing LLM endpoint."""
    result = await db.execute(
        select(LLMEndpoint).where(LLMEndpoint.id == endpoint_id)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    update_data = body.model_dump(exclude_unset=True)
    if "provider_type" in update_data and update_data["provider_type"] not in VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider_type. Must be one of: {', '.join(VALID_PROVIDER_TYPES)}",
        )

    for field_name, value in update_data.items():
        setattr(endpoint, field_name, value)

    await db.flush()
    await db.refresh(endpoint)
    reset_model_router()
    return EndpointResponse.from_orm_endpoint(endpoint)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an LLM endpoint."""
    result = await db.execute(
        select(LLMEndpoint).where(LLMEndpoint.id == endpoint_id)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    await db.delete(endpoint)
    await db.flush()
    reset_model_router()


@router.post("/endpoints/{endpoint_id}/test", response_model=TestResult)
async def test_endpoint(
    endpoint_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> TestResult:
    """Test connectivity for an LLM endpoint by making a minimal API call."""
    import time

    result = await db.execute(
        select(LLMEndpoint).where(LLMEndpoint.id == endpoint_id)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    start = time.monotonic()
    try:
        if endpoint.provider_type == "anthropic":
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=endpoint.api_key)
            await client.messages.create(
                model=endpoint.default_model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Say hi"}],
            )
        elif endpoint.provider_type in ("openai", "openai_compatible"):
            import openai

            kwargs: dict[str, Any] = {"api_key": endpoint.api_key or "not-needed"}
            if endpoint.provider_type == "openai_compatible" and endpoint.base_url:
                kwargs["base_url"] = endpoint.base_url

            client = openai.AsyncOpenAI(**kwargs)

            # Detect embedding models — use embeddings API instead of chat
            model_lower = (endpoint.default_model or "").lower()
            is_embedding = any(kw in model_lower for kw in ["embed", "embedding", "bge", "jina", "e5"])
            if is_embedding:
                await client.embeddings.create(
                    model=endpoint.default_model,
                    input="test",
                )
            else:
                await client.chat.completions.create(
                    model=endpoint.default_model,
                    max_tokens=10,
                    messages=[{"role": "user", "content": "Say hi"}],
                )
        else:
            return TestResult(
                success=False,
                message=f"Unknown provider_type: {endpoint.provider_type}",
            )

        elapsed = (time.monotonic() - start) * 1000
        endpoint.last_test_ok = 1
        endpoint.last_test_latency = round(elapsed, 1)
        await db.flush()
        return TestResult(success=True, message="Connection successful", latency_ms=round(elapsed, 1))

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        endpoint.last_test_ok = 0
        endpoint.last_test_latency = round(elapsed, 1)
        await db.flush()
        return TestResult(
            success=False,
            message=f"Connection failed: {str(e)}",
            latency_ms=round(elapsed, 1),
        )


# =========================================================================
# Task Routing
# =========================================================================


@router.get("/tasks", response_model=TaskConfigListResponse)
async def list_task_configs(
    db: AsyncSession = Depends(get_db),
) -> TaskConfigListResponse:
    """List all task type configurations with their assigned endpoints."""
    result = await db.execute(
        select(ModelConfigModel)
        .options(selectinload(ModelConfigModel.endpoint))
        .order_by(ModelConfigModel.task_type)
    )
    configs = {cfg.task_type: cfg for cfg in result.scalars().all()}

    tasks = []
    for task_type in sorted(VALID_TASK_TYPES):
        cfg = configs.get(task_type)
        if cfg and cfg.endpoint:
            endpoint_resp = EndpointResponse.from_orm_endpoint(cfg.endpoint)
        else:
            endpoint_resp = None

        tasks.append(
            TaskConfigResponse(
                task_type=task_type,
                endpoint=endpoint_resp,
                model_name=cfg.model_name if cfg else "",
                temperature=cfg.temperature if cfg else 0.7,
                max_tokens=cfg.max_tokens if cfg else 4096,
            )
        )

    return TaskConfigListResponse(tasks=tasks)


@router.put("/tasks/{task_type}", response_model=TaskConfigResponse)
async def update_task_config(
    task_type: str,
    body: TaskConfigUpdate,
    db: AsyncSession = Depends(get_db),
) -> TaskConfigResponse:
    """Assign an endpoint and model settings to a task type."""
    if task_type not in VALID_TASK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid task_type. Must be one of: {', '.join(sorted(VALID_TASK_TYPES))}",
        )

    # Verify endpoint exists if provided
    endpoint_obj = None
    if body.endpoint_id:
        ep_result = await db.execute(
            select(LLMEndpoint).where(LLMEndpoint.id == body.endpoint_id)
        )
        endpoint_obj = ep_result.scalar_one_or_none()
        if endpoint_obj is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")

    # Upsert the task config
    result = await db.execute(
        select(ModelConfigModel).where(ModelConfigModel.task_type == task_type)
    )
    cfg = result.scalar_one_or_none()

    if cfg is None:
        cfg = ModelConfigModel(task_type=task_type)
        db.add(cfg)

    if body.endpoint_id is not None:
        cfg.endpoint_id = body.endpoint_id
    if body.model_name is not None:
        cfg.model_name = body.model_name
    if body.temperature is not None:
        cfg.temperature = body.temperature
    if body.max_tokens is not None:
        cfg.max_tokens = body.max_tokens

    await db.flush()
    await db.refresh(cfg)

    # Reload with endpoint
    result = await db.execute(
        select(ModelConfigModel)
        .options(selectinload(ModelConfigModel.endpoint))
        .where(ModelConfigModel.id == cfg.id)
    )
    cfg = result.scalar_one()

    reset_model_router()

    endpoint_resp = EndpointResponse.from_orm_endpoint(cfg.endpoint) if cfg.endpoint else None
    return TaskConfigResponse(
        task_type=cfg.task_type,
        endpoint=endpoint_resp,
        model_name=cfg.model_name or "",
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )


# =========================================================================
# Presets
# =========================================================================


@router.get("/presets", response_model=list[PresetResponse])
async def list_presets() -> list[PresetResponse]:
    """Return sensible default presets for common setups."""
    return [
        PresetResponse(
            name="cloud_anthropic",
            description="All tasks use Anthropic Claude",
            tasks={
                "generation": {"model_name": "claude-sonnet-4-20250514", "temperature": 0.8, "max_tokens": 4096},
                "polishing": {"model_name": "claude-sonnet-4-20250514", "temperature": 0.7, "max_tokens": 4096},
                "outline": {"model_name": "claude-sonnet-4-20250514", "temperature": 0.8, "max_tokens": 8192},
                "extraction": {"model_name": "claude-haiku-4-5-20251001", "temperature": 0.3, "max_tokens": 2048},
                "evaluation": {"model_name": "claude-sonnet-4-20250514", "temperature": 0.3, "max_tokens": 2048},
                "summary": {"model_name": "claude-haiku-4-5-20251001", "temperature": 0.3, "max_tokens": 1024},
                "embedding": {"model_name": "claude-haiku-4-5-20251001", "temperature": 0.0, "max_tokens": 1024},
            },
        ),
        PresetResponse(
            name="cloud_openai",
            description="All tasks use OpenAI GPT",
            tasks={
                "generation": {"model_name": "gpt-4o", "temperature": 0.8, "max_tokens": 4096},
                "polishing": {"model_name": "gpt-4o", "temperature": 0.7, "max_tokens": 4096},
                "outline": {"model_name": "gpt-4o", "temperature": 0.8, "max_tokens": 8192},
                "extraction": {"model_name": "gpt-4o-mini", "temperature": 0.3, "max_tokens": 2048},
                "evaluation": {"model_name": "gpt-4o", "temperature": 0.3, "max_tokens": 2048},
                "summary": {"model_name": "gpt-4o-mini", "temperature": 0.3, "max_tokens": 1024},
                "embedding": {"model_name": "text-embedding-3-small", "temperature": 0.0, "max_tokens": 1024},
            },
        ),
        PresetResponse(
            name="hybrid",
            description="Generation on cloud, extraction/summary on local, embedding separate",
            tasks={
                "generation": {"model_name": "claude-sonnet-4-20250514", "temperature": 0.8, "max_tokens": 4096},
                "polishing": {"model_name": "claude-sonnet-4-20250514", "temperature": 0.7, "max_tokens": 4096},
                "outline": {"model_name": "claude-sonnet-4-20250514", "temperature": 0.8, "max_tokens": 8192},
                "extraction": {"model_name": "default", "temperature": 0.3, "max_tokens": 2048},
                "evaluation": {"model_name": "default", "temperature": 0.3, "max_tokens": 2048},
                "summary": {"model_name": "default", "temperature": 0.3, "max_tokens": 1024},
                "embedding": {"model_name": "text-embedding-3-small", "temperature": 0.0, "max_tokens": 1024},
            },
        ),
        PresetResponse(
            name="local_only",
            description="All tasks use one OpenAI-compatible local endpoint",
            tasks={
                "generation": {"model_name": "default", "temperature": 0.8, "max_tokens": 4096},
                "polishing": {"model_name": "default", "temperature": 0.7, "max_tokens": 4096},
                "outline": {"model_name": "default", "temperature": 0.8, "max_tokens": 8192},
                "extraction": {"model_name": "default", "temperature": 0.3, "max_tokens": 2048},
                "evaluation": {"model_name": "default", "temperature": 0.3, "max_tokens": 2048},
                "summary": {"model_name": "default", "temperature": 0.3, "max_tokens": 1024},
                "embedding": {"model_name": "default", "temperature": 0.0, "max_tokens": 1024},
            },
        ),
    ]
