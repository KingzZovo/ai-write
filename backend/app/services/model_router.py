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
                       temperature=0.7, max_tokens=4096, **kw) -> GenerationResult:
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
                              temperature=0.7, max_tokens=4096, **kw):
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
                       temperature=0.7, max_tokens=4096, **kw) -> GenerationResult:
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
                              temperature=0.7, max_tokens=4096, **kw):
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
                            max_tokens=p.max_tokens if p.max_tokens is not None else 4096,
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
                        self.embedding_provider = EmbeddingProvider(
                            api_key=decrypt_api_key(emb_ep.api_key or ""),
                            base_url=emb_ep.base_url or "",
                            model=emb_prompt.model_name or emb_ep.default_model,
                        )

                self._db_loaded = True
                logger.info("Loaded %d endpoints, %d task routes from DB",
                            len(endpoints), len(self.task_routing))
        except Exception as e:
            logger.warning("Failed to load model config from DB: %s", e)

    def _get_route(self, task_type: str) -> TaskRouteConfig:
        route = self.task_routing.get(task_type)
        if not route and self.providers:
            first = next(iter(self.providers))
            return TaskRouteConfig(provider_key=first, model_name="")
        if not route:
            raise ValueError(
                f"No model configured for '{task_type}'. "
                "Configure at Settings > Model Configuration.")
        return route

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
                       max_tokens: int | None = None, **kw) -> GenerationResult:
        route = self._get_route(task_type)
        provider = self._get_provider(route.provider_key)
        model = self._resolve_model(route)
        result = await provider.generate(
            messages=messages, model=model,
            temperature=temperature if temperature is not None else route.temperature,
            max_tokens=max_tokens if max_tokens is not None else route.max_tokens, **kw)
        self._track(result.usage)
        return result

    async def generate_stream(self, task_type: str, messages: list[dict],
                              temperature: float | None = None,
                              max_tokens: int | None = None, **kw) -> AsyncIterator[str]:
        route = self._get_route(task_type)
        provider = self._get_provider(route.provider_key)
        model = self._resolve_model(route)
        async for chunk in provider.generate_stream(
            messages=messages, model=model,
            temperature=temperature if temperature is not None else route.temperature,
            max_tokens=max_tokens if max_tokens is not None else route.max_tokens, **kw):
            yield chunk

    async def generate_by_route(self, route, messages: list[dict],
                                temperature: float | None = None,
                                max_tokens: int | None = None,
                                **kw) -> GenerationResult:
        """Generate using an explicit RouteSpec (v0.5 path)."""
        ep_key = str(route.endpoint_id)
        provider = self._get_provider(ep_key)
        model = route.model or self._endpoint_defaults.get(ep_key, "")
        result = await provider.generate(
            messages=messages, model=model,
            temperature=temperature if temperature is not None else route.temperature,
            max_tokens=max_tokens if max_tokens is not None else route.max_tokens, **kw)
        self._track(result.usage)
        return result

    async def stream_by_route(self, route, messages: list[dict],
                              temperature: float | None = None,
                              max_tokens: int | None = None,
                              **kw) -> AsyncIterator[str]:
        """Stream using an explicit RouteSpec (v0.5 path)."""
        ep_key = str(route.endpoint_id)
        provider = self._get_provider(ep_key)
        model = route.model or self._endpoint_defaults.get(ep_key, "")
        async for chunk in provider.generate_stream(
            messages=messages, model=model,
            temperature=temperature if temperature is not None else route.temperature,
            max_tokens=max_tokens if max_tokens is not None else route.max_tokens, **kw):
            yield chunk

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


_router: ModelRouter | None = None
_router_lock = asyncio.Lock()


async def get_model_router_async() -> ModelRouter:
    global _router
    # Fast path: already loaded
    if _router is not None and _router._db_loaded:
        return _router
    async with _router_lock:
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
