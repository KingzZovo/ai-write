"""
LLM Unified Access Layer - ModelRouter

Provides a unified interface for multiple LLM providers with:
- Task-based model routing (configurable per task from frontend UI)
- Multiple endpoints (different API keys / base URLs per task)
- Embedding endpoint independent from generation endpoint
- Fallback chains
- Streaming (SSE)
- Token usage tracking
- DB-first config (frontend-managed), .env fallback
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

logger = logging.getLogger(__name__)


# =========================================================================
# v1.4 — shared tier routing helpers
# =========================================================================

VALID_TIERS: frozenset[str] = frozenset(
    {"flagship", "standard", "small", "distill", "embedding"}
)
"""Canonical set of supported LLM tiers (v1.4).

Kept in sync with the DB CHECK constraints defined in alembic revision
``a1001400`` (``ck_llm_endpoints_tier`` and ``ck_prompt_assets_model_tier``).
"""


def is_valid_tier(tier: str | None) -> bool:
    """Return True iff ``tier`` is one of the five canonical v1.4 tiers.

    Empty strings and ``None`` are treated as invalid. Comparison is
    case-sensitive — ``"Flagship"`` is not accepted.
    """
    return bool(tier) and tier in VALID_TIERS


def compute_effective_tier(
    prompt_tier: str | None,
    endpoint_tier: str | None,
) -> str:
    """Three-level tier fallback: prompt ≫ endpoint ≫ ``"standard"``.

    The first valid tier wins. Invalid or empty inputs at either level
    are skipped silently so the result is always a valid tier string
    (never ``None``). This matches the contract documented in
    RELEASE_NOTES_v1.4.md and is exercised by
    ``tests/services/test_model_router_tier.py``.
    """
    if is_valid_tier(prompt_tier):
        return prompt_tier  # type: ignore[return-value]
    if is_valid_tier(endpoint_tier):
        return endpoint_tier  # type: ignore[return-value]
    return "standard"


@dataclass
class TaskRouteConfig:
    provider_key: str
    model_name: str
    temperature: float = 0.7
    max_tokens: int = 4096


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class GenerationResult:
    text: str
    usage: TokenUsage
    model: str
    provider: str


class BaseProvider(ABC):
    name: str

    @abstractmethod
    async def generate(self, messages: list[dict], model: str,
                       temperature: float = 0.7, max_tokens: int = 4096,
                       **kw) -> GenerationResult: ...

    @abstractmethod
    async def generate_stream(self, messages: list[dict], model: str,
                              temperature: float = 0.7, max_tokens: int = 4096,
                              **kw) -> AsyncIterator[str]: ...


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def generate(self, messages, model="claude-sonnet-4-20250514",
                       temperature=0.7, max_tokens=8192, **kw) -> GenerationResult:
        system_msg, chat = None, []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat.append(m)
        params: dict = {"model": model, "max_tokens": max_tokens,
                        "temperature": temperature, "messages": chat}
        if system_msg:
            params["system"] = system_msg
        resp = await self.client.messages.create(**params)
        text = resp.content[0].text if resp.content else ""
        usage = TokenUsage(resp.usage.input_tokens, resp.usage.output_tokens,
                           resp.usage.input_tokens + resp.usage.output_tokens)
        return GenerationResult(text=text, usage=usage, model=model, provider=self.name)

    async def generate_stream(self, messages, model="claude-sonnet-4-20250514",
                              temperature=0.7, max_tokens=8192, **kw):
        system_msg, chat = None, []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat.append(m)
        params: dict = {"model": model, "max_tokens": max_tokens,
                        "temperature": temperature, "messages": chat}
        if system_msg:
            params["system"] = system_msg
        async with self.client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text


class OpenAIProvider(BaseProvider):
    name = "openai"

    def __init__(self, api_key: str, base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import openai
            kw: dict = {"api_key": self.api_key or "not-needed"}
            if self.base_url:
                kw["base_url"] = self.base_url
            self._client = openai.AsyncOpenAI(**kw)
        return self._client

    async def generate(self, messages, model="gpt-4o",
                       temperature=0.7, max_tokens=8192, **kw) -> GenerationResult:
        # Some API proxies only return content via streaming.
        # Use stream mode and collect chunks for reliability.
        chunks: list[str] = []
        stream = await self.client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True, stream_options={"include_usage": True})
        usage = TokenUsage()
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)
            if hasattr(chunk, 'usage') and chunk.usage:
                u = chunk.usage
                usage = TokenUsage(
                    getattr(u, 'prompt_tokens', 0) or 0,
                    getattr(u, 'completion_tokens', 0) or 0,
                    getattr(u, 'total_tokens', 0) or 0)
        text = "".join(chunks)
        return GenerationResult(text=text, usage=usage, model=model, provider=self.name)

    async def generate_stream(self, messages, model="gpt-4o",
                              temperature=0.7, max_tokens=8192, **kw):
        stream = await self.client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, stream=True)
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class EmbeddingProvider:
    """Dedicated provider for embeddings (independent endpoint)."""

    def __init__(self, api_key: str, base_url: str = "",
                 model: str = "text-embedding-3-small"):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import openai
            kw: dict = {"api_key": self.api_key or "not-needed"}
            if self.base_url:
                kw["base_url"] = self.base_url
            self._client = openai.AsyncOpenAI(**kw)
        return self._client

    async def embed(self, text: str) -> list[float]:
        resp = await self.client.embeddings.create(
            model=self.model, input=text[:8000])
        return resp.data[0].embedding


class NvidiaEmbeddingProvider:
    """NVIDIA NIM / ``integrate.api.nvidia.com`` embeddings provider (v1.4 chunk-19).

    NVIDIA's embeddings endpoint speaks a superset of the OpenAI schema
    and requires four extra fields that the OpenAI SDK will not emit:

    - ``input`` — always an *array* of strings (not a plain string)
    - ``modality`` — e.g. ``["text"]``
    - ``input_type`` — ``"query"`` or ``"passage"``
    - ``encoding_format`` — ``"float"`` (base64 also accepted upstream)
    - ``truncate`` — ``"NONE"`` | ``"START"`` | ``"END"``

    We therefore drop the OpenAI SDK for this provider and POST the request
    directly via ``httpx``. The response shape matches OpenAI's
    ``{"data": [{"embedding": [float, ...]}]}`` so downstream callers can
    treat the returned vector identically.

    Reference (user-supplied curl):

        curl -X POST https://integrate.api.nvidia.com/v1/embeddings \
          -H "Authorization: Bearer $NVIDIA_API_KEY" \
          -d '{"input": ["..."], "model": "nvidia/llama-nemotron-embed-vl-1b-v2",
               "modality": ["text"], "input_type": "query",
               "encoding_format": "float", "truncate": "NONE"}'
    """

    DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model: str = "nvidia/llama-nemotron-embed-vl-1b-v2",
        modality: list[str] | None = None,
        encoding_format: str = "float",
        truncate: str = "NONE",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.modality = modality or ["text"]
        self.encoding_format = encoding_format
        self.truncate = truncate
        self.timeout = timeout

    async def _post(
        self, inputs: list[str], input_type: str
    ) -> dict:
        import httpx

        if not inputs:
            return {"data": []}
        # Soft-clip each input to 8k chars to avoid accidental massive
        # payloads; NVIDIA will also truncate per ``truncate`` policy.
        clipped = [s[:8000] for s in inputs]
        payload = {
            "input": clipped,
            "model": self.model,
            "modality": self.modality,
            "input_type": input_type,
            "encoding_format": self.encoding_format,
            "truncate": self.truncate,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/embeddings"
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            resp = await http.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            # Surface the server's error body to the caller — test_endpoint
            # re-wraps this in TestResult.message for UI visibility.
            snippet = resp.text[:400]
            raise RuntimeError(
                f"NVIDIA embeddings HTTP {resp.status_code}: {snippet}"
            )
        return resp.json()

    async def embed_one(
        self, text: str, *, input_type: str = "query"
    ) -> list[float]:
        """Embed a single string and return the raw float vector."""
        data = await self._post([text], input_type=input_type)
        rows = data.get("data") or []
        if not rows:
            return []
        return list(rows[0].get("embedding") or [])

    async def embed(self, text: str) -> list[float]:
        """Compat shim mirroring ``EmbeddingProvider.embed``."""
        return await self.embed_one(text, input_type="query")

    async def embed_many(
        self, texts: list[str], *, input_type: str = "passage"
    ) -> list[list[float]]:
        """Batch embedding — uses ``input_type="passage"`` by default."""
        data = await self._post(texts, input_type=input_type)
        rows = data.get("data") or []
        return [list(r.get("embedding") or []) for r in rows]


class ModelRouter:
    """
    Unified model access layer.
    Config loaded from DB (frontend UI) with .env fallback.
    Supports independent embedding endpoint.
    """

    def __init__(self):
        self.providers: dict[str, BaseProvider] = {}
        self.task_routing: dict[str, TaskRouteConfig] = {}
        self.embedding_provider: EmbeddingProvider | None = None
        self.total_usage = TokenUsage()
        self._db_loaded = False
        self._endpoint_defaults: dict[str, str] = {}  # provider_key -> default_model
        # v1.4 — endpoint tier registry (provider_key -> tier)
        self._endpoint_tiers: dict[str, str] = {}
        # v1.4 — endpoint display names (provider_key -> name)
        self._endpoint_names: dict[str, str] = {}

    def register_provider(self, key: str, provider: BaseProvider) -> None:
        self.providers[key] = provider

    def set_embedding(self, provider: EmbeddingProvider) -> None:
        self.embedding_provider = provider

    async def load_from_db(self) -> None:
        """Load endpoint configs and task routing from database.

        v0.5: task routing now comes from `PromptAsset.endpoint_id` + `model_name`,
        not from the deleted `model_configs` table.
        """
        try:
            from sqlalchemy import select
            from app.db.session import async_session_factory
            from app.models.project import LLMEndpoint
            from app.models.prompt import PromptAsset

            async with async_session_factory() as db:
                result = await db.execute(
                    select(LLMEndpoint).where(LLMEndpoint.enabled == 1))
                endpoints = result.scalars().all()

                from app.utils.crypto import decrypt_api_key

                for ep in endpoints:
                    key = str(ep.id)
                    self._endpoint_defaults[key] = ep.default_model or ""
                    # v1.4 — record tier + name per endpoint for tier-aware routing
                    self._endpoint_tiers[key] = getattr(ep, "tier", "standard") or "standard"
                    self._endpoint_names[key] = getattr(ep, "name", "") or ""
                    api_key = decrypt_api_key(ep.api_key or "")
                    # v1.4.1 hardening: NVIDIA endpoints are embedding-only.
                    # Never register them into the chat provider dict where they
                    # would otherwise be wrapped as OpenAIProvider. With an
                    # empty base_url the OpenAI SDK silently defaults to
                    # https://api.openai.com/v1, causing chat tasks that fell
                    # into the fallback branch to leak the nvapi- key to the
                    # public OpenAI API. Their embedding role is still handled
                    # via the dedicated embedding_provider path below.
                    if ep.provider_type == "nvidia":
                        continue
                    if ep.provider_type == "anthropic":
                        self.register_provider(key, AnthropicProvider(api_key=api_key))
                    else:
                        self.register_provider(key, OpenAIProvider(
                            api_key=api_key, base_url=ep.base_url or ""))

                # Load task routing from active PromptAssets (v0.5)
                result = await db.execute(
                    select(PromptAsset).where(PromptAsset.is_active == 1))
                prompts_rows = result.scalars().all()

                for p in prompts_rows:
                    if p.endpoint_id and str(p.endpoint_id) in self.providers:
                        self.task_routing[p.task_type] = TaskRouteConfig(
                            provider_key=str(p.endpoint_id),
                            model_name=p.model_name or "",
                            temperature=p.temperature if p.temperature is not None else 0.7,
                            max_tokens=p.max_tokens if p.max_tokens is not None else 8192,
                        )

                # Embedding endpoint — find prompt with task_type="embedding"
                emb_prompt = next(
                    (p for p in prompts_rows
                     if p.task_type == "embedding" and p.endpoint_id),
                    None,
                )
                if emb_prompt:
                    emb_ep = next(
                        (e for e in endpoints
                         if str(e.id) == str(emb_prompt.endpoint_id)),
                        None,
                    )
                    if emb_ep:
                        emb_api_key = decrypt_api_key(emb_ep.api_key or "")
                        emb_model = emb_prompt.model_name or emb_ep.default_model
                        if emb_ep.provider_type == "nvidia":
                            # v1.4.2 fix: NVIDIA embeddings require a different
                            # request schema (input=list, input_type, modality,
                            # encoding_format, truncate) and are not speakable
                            # via the OpenAI SDK. Using the generic OpenAI-style
                            # EmbeddingProvider here would either leak to
                            # api.openai.com (when base_url is empty) or be
                            # rejected by NVIDIA with 400/401. Route NVIDIA
                            # endpoints to the dedicated httpx-based provider.
                            self.embedding_provider = NvidiaEmbeddingProvider(
                                api_key=emb_api_key,
                                base_url=emb_ep.base_url or "",
                                model=emb_model,
                            )
                        else:
                            self.embedding_provider = EmbeddingProvider(
                                api_key=emb_api_key,
                                base_url=emb_ep.base_url or "",
                                model=emb_model,
                            )

                self._db_loaded = True
                logger.info("Loaded %d endpoints, %d task routes from DB",
                            len(endpoints), len(self.task_routing))
        except Exception as e:
            logger.warning("Failed to load model config from DB: %s", e)

    def _get_route(self, task_type: str) -> TaskRouteConfig:
        route = self.task_routing.get(task_type)
        if route:
            return route
        # v1.4.1 hardening: fall back only to chat-capable providers.
        # Skip endpoints whose tier is "embedding" so a chat task never gets
        # silently routed to an embeddings endpoint. If no chat-capable
        # provider is configured, raise an explicit error pointing the user
        # to the Prompt management UI instead of leaking requests.
        chat_keys = [
            k for k in self.providers
            if self._endpoint_tiers.get(k, "standard") != "embedding"
        ]
        if chat_keys:
            return TaskRouteConfig(provider_key=chat_keys[0], model_name="")
        if self.providers:
            raise ValueError(
                f"No chat-capable model configured for task '{task_type}'. "
                "All registered endpoints are embedding-tier. Open "
                "/prompts and bind a non-embedding endpoint to this prompt."
            )
        raise ValueError(
            f"No model configured for '{task_type}'. "
            "Configure at Settings > Model Configuration."
        )

    def _pick_endpoint_by_tier(self, tier: str) -> str | None:
        """v1.4 — return the first registered provider_key whose endpoint tier matches.

        Returns None if no endpoint for that tier is registered.
        """
        if not tier:
            return None
        for key, t in self._endpoint_tiers.items():
            if t == tier and key in self.providers:
                return key
        return None

    def list_routes_matrix(self) -> list[dict]:
        """v1.4 — flat snapshot of task routing + endpoint tier for the matrix UI/API.

        Each row: task_type, endpoint_id, endpoint_name, model, tier,
        temperature, max_tokens.
        """
        rows: list[dict] = []
        for task_type, route in self.task_routing.items():
            key = route.provider_key
            rows.append({
                "task_type": task_type,
                "endpoint_id": key,
                "endpoint_name": self._endpoint_names.get(key, ""),
                "model": self._resolve_model(route),
                "tier": self._endpoint_tiers.get(key, "standard"),
                "temperature": route.temperature,
                "max_tokens": route.max_tokens,
            })
        rows.sort(key=lambda r: (r["tier"], r["task_type"]))
        return rows

    def _get_provider(self, key: str) -> BaseProvider:
        if key in self.providers:
            return self.providers[key]
        if self.providers:
            fb = next(iter(self.providers))
            logger.warning("Provider %s not found, falling back to %s", key, fb)
            return self.providers[fb]
        raise ValueError("No LLM endpoints configured. Add one at Settings > Model Configuration.")

    def _track(self, usage: TokenUsage) -> None:
        self.total_usage.input_tokens += usage.input_tokens
        self.total_usage.output_tokens += usage.output_tokens
        self.total_usage.total_tokens += usage.total_tokens

    def _resolve_model(self, route: TaskRouteConfig) -> str:
        """Get the actual model name: route override > endpoint default."""
        if route.model_name:
            return route.model_name
        return self._endpoint_defaults.get(route.provider_key, "")

    async def generate(self, task_type: str, messages: list[dict],
                       temperature: float | None = None,
                       max_tokens: int | None = None,
                       _log_meta: dict | None = None, **kw) -> GenerationResult:
        route = self._get_route(task_type)
        provider = self._get_provider(route.provider_key)
        model = self._resolve_model(route)
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens
        if _log_meta is None:
            result = await provider.generate(
                messages=messages, model=model,
                temperature=eff_temp, max_tokens=eff_max, **kw)
            self._track(result.usage)
            return result
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", route.provider_key)
        async with async_session_factory() as db:
            async with log_llm_call(
                db=db, task_type=task_type, model=model,
                endpoint_id=endpoint_id, messages=messages,
                prompt_id=meta.pop("prompt_id", None),
                project_id=meta.pop("project_id", None),
                chapter_id=meta.pop("chapter_id", None),
                rag_hits=meta.pop("rag_hits", None),
            ) as ctx:
                result = await provider.generate(
                    messages=messages, model=model,
                    temperature=eff_temp, max_tokens=eff_max, **kw)
                ctx.add_chunk(result.text)
                ctx.set_usage(result.usage.input_tokens, result.usage.output_tokens)
            await db.commit()
        self._track(result.usage)
        return result

    async def generate_stream(self, task_type: str, messages: list[dict],
                              temperature: float | None = None,
                              max_tokens: int | None = None,
                              _log_meta: dict | None = None, **kw) -> AsyncIterator[str]:
        route = self._get_route(task_type)
        provider = self._get_provider(route.provider_key)
        model = self._resolve_model(route)
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens
        if _log_meta is None:
            async for chunk in provider.generate_stream(
                messages=messages, model=model,
                temperature=eff_temp, max_tokens=eff_max, **kw):
                yield chunk
            return
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", route.provider_key)
        async with async_session_factory() as db:
            async with log_llm_call(
                db=db, task_type=task_type, model=model,
                endpoint_id=endpoint_id, messages=messages,
                prompt_id=meta.pop("prompt_id", None),
                project_id=meta.pop("project_id", None),
                chapter_id=meta.pop("chapter_id", None),
                rag_hits=meta.pop("rag_hits", None),
            ) as ctx:
                async for chunk in provider.generate_stream(
                    messages=messages, model=model,
                    temperature=eff_temp, max_tokens=eff_max, **kw):
                    ctx.add_chunk(chunk)
                    yield chunk
            await db.commit()

    async def generate_by_route(self, route, messages: list[dict],
                                temperature: float | None = None,
                                max_tokens: int | None = None,
                                _log_meta: dict | None = None,
                                **kw) -> GenerationResult:
        """Generate using an explicit RouteSpec (v0.5 path)."""
        ep_key = str(route.endpoint_id)
        provider = self._get_provider(ep_key)
        model = route.model or self._endpoint_defaults.get(ep_key, "")
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens
        if _log_meta is None:
            result = await provider.generate(
                messages=messages, model=model,
                temperature=eff_temp, max_tokens=eff_max, **kw)
            self._track(result.usage)
            return result
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", ep_key)
        task_type = meta.pop("task_type", "by_route")
        async with async_session_factory() as db:
            async with log_llm_call(
                db=db, task_type=task_type, model=model,
                endpoint_id=endpoint_id, messages=messages,
                prompt_id=meta.pop("prompt_id", None),
                project_id=meta.pop("project_id", None),
                chapter_id=meta.pop("chapter_id", None),
                rag_hits=meta.pop("rag_hits", None),
            ) as ctx:
                result = await provider.generate(
                    messages=messages, model=model,
                    temperature=eff_temp, max_tokens=eff_max, **kw)
                ctx.add_chunk(result.text)
                ctx.set_usage(result.usage.input_tokens, result.usage.output_tokens)
            await db.commit()
        self._track(result.usage)
        return result

    async def stream_by_route(self, route, messages: list[dict],
                              temperature: float | None = None,
                              max_tokens: int | None = None,
                              _log_meta: dict | None = None,
                              **kw) -> AsyncIterator[str]:
        """Stream using an explicit RouteSpec (v0.5 path)."""
        ep_key = str(route.endpoint_id)
        provider = self._get_provider(ep_key)
        model = route.model or self._endpoint_defaults.get(ep_key, "")
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens
        if _log_meta is None:
            async for chunk in provider.generate_stream(
                messages=messages, model=model,
                temperature=eff_temp, max_tokens=eff_max, **kw):
                yield chunk
            return
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", ep_key)
        task_type = meta.pop("task_type", "by_route_stream")
        async with async_session_factory() as db:
            async with log_llm_call(
                db=db, task_type=task_type, model=model,
                endpoint_id=endpoint_id, messages=messages,
                prompt_id=meta.pop("prompt_id", None),
                project_id=meta.pop("project_id", None),
                chapter_id=meta.pop("chapter_id", None),
                rag_hits=meta.pop("rag_hits", None),
            ) as ctx:
                async for chunk in provider.generate_stream(
                    messages=messages, model=model,
                    temperature=eff_temp, max_tokens=eff_max, **kw):
                    ctx.add_chunk(chunk)
                    yield chunk
            await db.commit()

    async def generate_with_fallback(self, task_type: str, messages: list[dict],
                                     **kw) -> GenerationResult:
        route = self._get_route(task_type)
        model = self._resolve_model(route)
        to_try = [route.provider_key] + [k for k in self.providers if k != route.provider_key]
        last_err = None
        for key in to_try:
            if key not in self.providers:
                continue
            try:
                prov = self.providers[key]
                result = await prov.generate(
                    messages=messages, model=model,
                    temperature=kw.get("temperature", route.temperature),
                    max_tokens=kw.get("max_tokens", route.max_tokens))
                self._track(result.usage)
                return result
            except Exception as e:
                logger.warning("Provider %s failed: %s", key, e)
                last_err = e
        raise RuntimeError(f"All providers failed for {task_type}") from last_err

    async def embed(self, text: str) -> list[float]:
        """Generate embedding using dedicated embedding provider."""
        if self.embedding_provider:
            return await self.embedding_provider.embed(text)
        for prov in self.providers.values():
            if isinstance(prov, OpenAIProvider):
                try:
                    ep = EmbeddingProvider(api_key=prov.api_key, base_url=prov.base_url)
                    return await ep.embed(text)
                except Exception:
                    continue
        logger.warning("No embedding provider, returning zero vector")
        return [0.0] * 1536

    def get_usage_stats(self) -> dict:
        return {
            "total_input_tokens": self.total_usage.input_tokens,
            "total_output_tokens": self.total_usage.output_tokens,
            "total_tokens": self.total_usage.total_tokens,
        }


    # =====================================================================
    # v1.5.0 B1 — tier-aware fallback chain (real fallback, with per-attempt logging)
    # =====================================================================
    def _build_tier_attempts(
        self,
        *,
        route=None,
        preferred_tier: str | None = None,
        fallback_tiers: list[str] | None = None,
    ) -> list[tuple[str, str, str]]:
        """Return ordered list of (tier_label, endpoint_key, model_name) attempts.

        Order:
          1. If ``route`` is given and its endpoint is registered, that
             explicit endpoint is the first attempt (with its tier label).
          2. Else if ``preferred_tier`` is given, the first endpoint whose
             tier matches is the first attempt.
          3. Then walk ``fallback_tiers`` (default flagship→standard→small)
             skipping tiers already attempted and the ``embedding`` tier.
        Always de-duplicates endpoints; an endpoint is only tried once.
        """
        attempts: list[tuple[str, str, str]] = []
        seen_eps: set[str] = set()
        if route is not None:
            ep_key = str(route.endpoint_id)
            if ep_key in self.providers:
                tier = self._endpoint_tiers.get(ep_key, "standard")
                model = (getattr(route, "model", None) or
                         self._endpoint_defaults.get(ep_key, ""))
                attempts.append((tier, ep_key, model))
                seen_eps.add(ep_key)
        elif preferred_tier and preferred_tier in VALID_TIERS                 and preferred_tier != "embedding":
            ep_key = self._pick_endpoint_by_tier(preferred_tier)
            if ep_key and ep_key not in seen_eps:
                model = self._endpoint_defaults.get(ep_key, "")
                attempts.append((preferred_tier, ep_key, model))
                seen_eps.add(ep_key)
        chain = fallback_tiers or ["flagship", "standard", "small"]
        for t in chain:
            if not t or t == "embedding" or t not in VALID_TIERS:
                continue
            ep_key = self._pick_endpoint_by_tier(t)
            if not ep_key or ep_key in seen_eps:
                continue
            model = self._endpoint_defaults.get(ep_key, "")
            attempts.append((t, ep_key, model))
            seen_eps.add(ep_key)
        return attempts

    async def generate_with_tier_fallback(
        self,
        task_type: str,
        messages: list[dict],
        *,
        route=None,
        preferred_tier: str | None = None,
        fallback_tiers: list[str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        _log_meta: dict | None = None,
        **kw,
    ) -> GenerationResult:
        """Generate with a tier-aware fallback chain. Logs every attempt.

        On each attempt, an LLMCallLog row is inserted with ``tier_used``,
        ``attempt_index``, and (for attempts > 0) ``fallback_reason``.
        Raises only when *every* tier in the chain has been exhausted.
        """
        attempts = self._build_tier_attempts(
            route=route, preferred_tier=preferred_tier,
            fallback_tiers=fallback_tiers,
        )
        if not attempts:
            raise RuntimeError(
                f"No endpoints available for tier-aware fallback (task={task_type}). "
                "Configure at least one chat-capable endpoint at Settings > Model Configuration."
            )
        if route is not None:
            base_temp = getattr(route, "temperature", 0.7)
            base_max = getattr(route, "max_tokens", 8192)
        else:
            try:
                br = self._get_route(task_type)
                base_temp, base_max = br.temperature, br.max_tokens
            except Exception:
                base_temp, base_max = 0.7, 8192
        eff_temp = temperature if temperature is not None else base_temp
        eff_max = max_tokens if max_tokens is not None else base_max
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta or {})
        last_err: Exception | None = None
        fallback_reason: str | None = None
        for idx, (tier, ep_key, model) in enumerate(attempts):
            provider = self.providers[ep_key]
            try:
                if _log_meta is None:
                    result = await provider.generate(
                        messages=messages, model=model,
                        temperature=eff_temp, max_tokens=eff_max, **kw)
                    self._track(result.usage)
                    return result
                async with async_session_factory() as db:
                    async with log_llm_call(
                        db=db, task_type=task_type, model=model,
                        endpoint_id=ep_key, messages=messages,
                        prompt_id=meta.get("prompt_id"),
                        project_id=meta.get("project_id"),
                        chapter_id=meta.get("chapter_id"),
                        rag_hits=meta.get("rag_hits"),
                        tier_used=tier,
                        fallback_reason=fallback_reason if idx > 0 else None,
                        attempt_index=idx,
                    ) as ctx:
                        result = await provider.generate(
                            messages=messages, model=model,
                            temperature=eff_temp, max_tokens=eff_max, **kw)
                        ctx.add_chunk(result.text)
                        ctx.set_usage(result.usage.input_tokens,
                                      result.usage.output_tokens)
                    await db.commit()
                self._track(result.usage)
                return result
            except Exception as e:
                logger.warning(
                    "tier-fallback attempt %d (tier=%s ep=%s) failed: %s",
                    idx, tier, ep_key, e)
                last_err = e
                fallback_reason = f"{type(e).__name__}:{str(e)[:160]}"
                continue
        raise RuntimeError(
            f"All tier-fallback attempts failed for task '{task_type}': "
            f"{fallback_reason}"
        ) from last_err

    async def stream_with_tier_fallback(
        self,
        task_type: str,
        messages: list[dict],
        *,
        route=None,
        preferred_tier: str | None = None,
        fallback_tiers: list[str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        _log_meta: dict | None = None,
        **kw,
    ):
        """Stream with tier-aware fallback. Only falls back BEFORE first chunk
        is yielded; mid-stream errors propagate (cannot retry without
        corrupting consumer's accumulated buffer).
        """
        attempts = self._build_tier_attempts(
            route=route, preferred_tier=preferred_tier,
            fallback_tiers=fallback_tiers,
        )
        if not attempts:
            raise RuntimeError(
                f"No endpoints available for tier-aware fallback stream (task={task_type})"
            )
        if route is not None:
            base_temp = getattr(route, "temperature", 0.7)
            base_max = getattr(route, "max_tokens", 8192)
        else:
            try:
                br = self._get_route(task_type)
                base_temp, base_max = br.temperature, br.max_tokens
            except Exception:
                base_temp, base_max = 0.7, 8192
        eff_temp = temperature if temperature is not None else base_temp
        eff_max = max_tokens if max_tokens is not None else base_max
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta or {})
        last_err: Exception | None = None
        fallback_reason: str | None = None
        for idx, (tier, ep_key, model) in enumerate(attempts):
            provider = self.providers[ep_key]
            yielded_any = False
            try:
                if _log_meta is None:
                    async for chunk in provider.generate_stream(
                        messages=messages, model=model,
                        temperature=eff_temp, max_tokens=eff_max, **kw):
                        yielded_any = True
                        yield chunk
                    return
                async with async_session_factory() as db:
                    async with log_llm_call(
                        db=db, task_type=task_type, model=model,
                        endpoint_id=ep_key, messages=messages,
                        prompt_id=meta.get("prompt_id"),
                        project_id=meta.get("project_id"),
                        chapter_id=meta.get("chapter_id"),
                        rag_hits=meta.get("rag_hits"),
                        tier_used=tier,
                        fallback_reason=fallback_reason if idx > 0 else None,
                        attempt_index=idx,
                    ) as ctx:
                        async for chunk in provider.generate_stream(
                            messages=messages, model=model,
                            temperature=eff_temp, max_tokens=eff_max, **kw):
                            yielded_any = True
                            ctx.add_chunk(chunk)
                            yield chunk
                    await db.commit()
                return
            except Exception as e:
                logger.warning(
                    "stream tier-fallback attempt %d (tier=%s ep=%s yielded=%s) failed: %s",
                    idx, tier, ep_key, yielded_any, e)
                if yielded_any:
                    raise
                last_err = e
                fallback_reason = f"{type(e).__name__}:{str(e)[:160]}"
                continue
        raise RuntimeError(
            f"All stream tier-fallback attempts failed for task '{task_type}': "
            f"{fallback_reason}"
        ) from last_err


_router: ModelRouter | None = None
# v1.7 (Bug J residual): Avoid binding the lock to whatever event loop
# happens to import this module first. Each running loop gets its own
# Lock from this dict, keyed by id(loop). Stale entries are cleaned up
# when ``reset_model_router`` is called between celery tasks (see
# ``app.tasks._run_async_safe``).
_router_locks: "dict[int, asyncio.Lock]" = {}


def _get_router_lock() -> asyncio.Lock:
    """Return an ``asyncio.Lock`` bound to the *currently running* event loop.

    Lazily creates one per loop so that re-entry under a fresh celery loop
    (after ``_run_async_safe`` resets the singletons) does not blow up with
    ``Lock is bound to a different event loop``.
    """
    loop = asyncio.get_event_loop()
    key = id(loop)
    lock = _router_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _router_locks[key] = lock
    return lock


async def get_model_router_async() -> ModelRouter:
    global _router
    # Fast path: already loaded
    if _router is not None and _router._db_loaded:
        return _router
    async with _get_router_lock():
        if _router is None:
            _router = ModelRouter()
        if not _router._db_loaded:
            await _router.load_from_db()
            if not _router.providers:
                _load_from_env(_router)
    return _router


def get_model_router() -> ModelRouter:
    """Get the model router singleton.

    DB config is pre-loaded at app startup via lifespan handler.
    If not loaded yet (e.g. Celery worker), loads synchronously.
    """
    global _router
    if _router is None:
        _router = ModelRouter()
        _load_from_env(_router)
    if not _router._db_loaded:
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                # Already in async context — can't block, DB will load later
                pass
            else:
                asyncio.run(_router.load_from_db())
        except Exception as e:
            logger.warning("Sync DB load failed: %s", e)
    return _router


def reset_model_router() -> None:
    """Reset singleton (called when config changes via API)."""
    global _router
    _router = None
    # v1.7: drop any per-loop locks so the next caller (potentially under a
    # brand new event loop, as celery tasks do) starts from a clean slate.
    _router_locks.clear()


def _load_from_env(router: ModelRouter) -> None:
    from app.config import settings
    if settings.ANTHROPIC_API_KEY:
        router.register_provider("env_anthropic", AnthropicProvider(settings.ANTHROPIC_API_KEY))
    if settings.OPENAI_API_KEY:
        router.register_provider("env_openai", OpenAIProvider(settings.OPENAI_API_KEY))
    if settings.OPENAI_COMPATIBLE_BASE_URL:
        router.register_provider("env_compatible", OpenAIProvider(
            api_key=settings.OPENAI_COMPATIBLE_API_KEY or "",
            base_url=settings.OPENAI_COMPATIBLE_BASE_URL))
