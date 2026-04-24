"""LLM endpoint configuration endpoints.

v0.5: task routing has moved into PromptAsset (see /api/prompts). This module
now only manages LLM endpoints (add/edit/test/delete).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import LLMEndpoint
from app.services.model_router import reset_model_router

router = APIRouter(prefix="/api/model-config", tags=["model-config"])

VALID_PROVIDER_TYPES = {"anthropic", "openai", "openai_compatible", "nvidia"}


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
    provider_type: str = Field(
        ...,
        description="anthropic | openai | openai_compatible | nvidia",
    )
    base_url: str = Field("", max_length=1000)
    api_key: str = Field("", max_length=500)
    default_model: str = Field(..., max_length=200)
    # v1.4 — routing tier
    tier: str = Field(
        "standard",
        pattern=r"^(flagship|standard|small|distill|embedding)$",
    )


class EndpointUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    enabled: int | None = None
    # v1.4 — routing tier (optional on update)
    tier: str | None = Field(
        None,
        pattern=r"^(flagship|standard|small|distill|embedding)$",
    )


class EndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    provider_type: str
    base_url: str
    api_key_masked: str
    default_model: str
    enabled: int
    # v1.4 — routing tier
    tier: str = "standard"
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
            tier=getattr(ep, "tier", "standard") or "standard",
            last_test_ok=getattr(ep, "last_test_ok", 0) or 0,
            last_test_latency=getattr(ep, "last_test_latency", None),
            created_at=ep.created_at,
        )


class EndpointListResponse(BaseModel):
    endpoints: list[EndpointResponse]
    total: int


class TestResult(BaseModel):
    success: bool
    message: str
    latency_ms: float | None = None
    # v1.4 chunk-18 — endpoint-test visibility.
    # All fields below are populated on a best-effort basis so the frontend
    # can render the actual request and response instead of only a latency.
    sent_text: str | None = None
    """The literal prompt sent to the endpoint (``"hi"`` for chat models)."""
    request_summary: str | None = None
    """One-line human-readable summary of the outgoing request."""
    response_text: str | None = None
    """Full text of the model's reply (chat models only, may be long)."""
    response_preview: str | None = None
    """Short preview of the response (≤ 400 chars) safe for inline display."""
    embedding_dim: int | None = None
    """Dimensionality of the returned vector (embedding models only)."""
    response_first_floats: list[float] | None = None
    """First 3 floats of the returned vector (embedding models only)."""


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

    from app.utils.crypto import encrypt_api_key

    endpoint = LLMEndpoint(
        name=body.name,
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=encrypt_api_key(body.api_key),
        default_model=body.default_model,
        tier=body.tier,
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

    from app.utils.crypto import encrypt_api_key

    for field_name, value in update_data.items():
        if field_name == "api_key" and value:
            value = encrypt_api_key(value)
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
    """Probe an LLM endpoint and return request/response for visibility.

    v1.4 chunk-18 — the handler now sends the literal string ``"hi"`` to
    chat endpoints (or a short embedding probe for embedding endpoints) and
    returns the model's actual reply alongside latency. The frontend uses
    the ``sent_text`` / ``response_text`` / ``response_preview`` /
    ``embedding_dim`` / ``response_first_floats`` fields to render the full
    round-trip instead of only a pass/fail latency pill.

    NVIDIA embeddings endpoints (chunk-19) are routed through
    ``NvidiaEmbeddingProvider`` in ``app.services.model_router`` so this
    handler also covers ``provider_type == "nvidia"``.
    """
    import time

    result = await db.execute(
        select(LLMEndpoint).where(LLMEndpoint.id == endpoint_id)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    from app.utils.crypto import decrypt_api_key
    decrypted_key = decrypt_api_key(endpoint.api_key or "")

    probe_text = "hi"
    model_lower = (endpoint.default_model or "").lower()
    # Heuristic shared with ModelRouter: any model that mentions an
    # embedding family keyword is probed via the embeddings API.
    is_embedding_model = any(
        kw in model_lower
        for kw in ("embed", "embedding", "bge", "jina", "e5")
    )
    # NVIDIA provider is always embedding in v1.4 (chunk-19).
    if endpoint.provider_type == "nvidia":
        is_embedding_model = True

    response_text: str | None = None
    embedding_dim: int | None = None
    first_floats: list[float] | None = None
    request_summary: str

    start = time.monotonic()
    try:
        if endpoint.provider_type == "anthropic":
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=decrypted_key)
            msg = await client.messages.create(
                model=endpoint.default_model,
                max_tokens=32,
                messages=[{"role": "user", "content": probe_text}],
            )
            parts: list[str] = []
            for block in getattr(msg, "content", []) or []:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            response_text = "".join(parts).strip() or None
            request_summary = (
                f"POST anthropic.messages model={endpoint.default_model} "
                f"max_tokens=32 content={probe_text!r}"
            )
        elif endpoint.provider_type in ("openai", "openai_compatible"):
            import openai

            kwargs: dict[str, Any] = {"api_key": decrypted_key or "not-needed"}
            if endpoint.provider_type == "openai_compatible" and endpoint.base_url:
                kwargs["base_url"] = endpoint.base_url
            client = openai.AsyncOpenAI(**kwargs)

            if is_embedding_model:
                emb = await client.embeddings.create(
                    model=endpoint.default_model,
                    input=probe_text,
                )
                vec = list(emb.data[0].embedding) if emb.data else []
                embedding_dim = len(vec)
                first_floats = [float(x) for x in vec[:3]]
                request_summary = (
                    f"POST {endpoint.base_url or 'openai'}/embeddings "
                    f"model={endpoint.default_model} input={probe_text!r}"
                )
            else:
                chat = await client.chat.completions.create(
                    model=endpoint.default_model,
                    max_tokens=32,
                    messages=[{"role": "user", "content": probe_text}],
                )
                if chat.choices:
                    response_text = (
                        chat.choices[0].message.content or ""
                    ).strip() or None
                request_summary = (
                    f"POST {endpoint.base_url or 'openai'}/chat/completions "
                    f"model={endpoint.default_model} max_tokens=32 "
                    f"content={probe_text!r}"
                )
        elif endpoint.provider_type == "nvidia":
            # chunk-19: NVIDIA embeddings use their own HTTP contract.
            from app.services.model_router import NvidiaEmbeddingProvider

            provider = NvidiaEmbeddingProvider(
                base_url=endpoint.base_url
                or "https://integrate.api.nvidia.com/v1",
                api_key=decrypted_key,
                model=endpoint.default_model,
            )
            vec = await provider.embed_one(probe_text, input_type="query")
            embedding_dim = len(vec)
            first_floats = [float(x) for x in vec[:3]]
            request_summary = (
                f"POST {endpoint.base_url or 'https://integrate.api.nvidia.com/v1'}"
                f"/embeddings model={endpoint.default_model} "
                f"input={[probe_text]!r} modality=['text'] input_type='query'"
            )
        else:
            return TestResult(
                success=False,
                message=f"Unknown provider_type: {endpoint.provider_type}",
                sent_text=probe_text,
            )

        elapsed = (time.monotonic() - start) * 1000
        endpoint.last_test_ok = 1
        endpoint.last_test_latency = round(elapsed, 1)
        await db.flush()

        preview: str | None = None
        if response_text:
            preview = response_text[:400]
        elif embedding_dim is not None:
            preview = (
                f"embedding dim={embedding_dim}, first 3 floats={first_floats}"
            )
        return TestResult(
            success=True,
            message="Connection successful",
            latency_ms=round(elapsed, 1),
            sent_text=probe_text,
            request_summary=request_summary,
            response_text=response_text,
            response_preview=preview,
            embedding_dim=embedding_dim,
            response_first_floats=first_floats,
        )

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        endpoint.last_test_ok = 0
        endpoint.last_test_latency = round(elapsed, 1)
        await db.flush()
        return TestResult(
            success=False,
            message=f"Connection failed: {str(e)}",
            latency_ms=round(elapsed, 1),
            sent_text=probe_text,
        )
