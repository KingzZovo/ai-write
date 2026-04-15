"""
LLM Unified Access Layer - ModelRouter

Provides a unified interface for multiple LLM providers with:
- Task-based model routing (heavy tasks → large models, light tasks → small models)
- Fallback chains (if primary model fails, try next)
- Streaming support (SSE)
- Token usage tracking
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    provider: str
    model_name: str
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 1.0
    extra_params: dict = field(default_factory=dict)


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
    """Abstract base class for LLM providers."""

    name: str

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> GenerationResult:
        ...

    @abstractmethod
    async def generate_stream(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[str]:
        ...


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

    async def generate(
        self,
        messages: list[dict],
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> GenerationResult:
        system_msg = None
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                chat_messages.append(msg)

        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_msg:
            params["system"] = system_msg

        response = await self.client.messages.create(**params)
        text = response.content[0].text if response.content else ""
        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
        )
        return GenerationResult(text=text, usage=usage, model=model, provider=self.name)

    async def generate_stream(
        self,
        messages: list[dict],
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[str]:
        system_msg = None
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                chat_messages.append(msg)

        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_msg:
            params["system"] = system_msg

        async with self.client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text


class OpenAIProvider(BaseProvider):
    name = "openai"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(api_key=self.api_key)
        return self._client

    async def generate(
        self,
        messages: list[dict],
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> GenerationResult:
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = response.choices[0].message.content or ""
        usage = TokenUsage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            total_tokens=response.usage.total_tokens if response.usage else 0,
        )
        return GenerationResult(text=text, usage=usage, model=model, provider=self.name)

    async def generate_stream(
        self,
        messages: list[dict],
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[str]:
        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class OpenAICompatibleProvider(BaseProvider):
    """Provider for any OpenAI-compatible API endpoint (e.g., local models, OpenRouter)."""

    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key or "not-needed",
            )
        return self._client

    async def generate(
        self,
        messages: list[dict],
        model: str = "default",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> GenerationResult:
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = response.choices[0].message.content or ""
        usage = TokenUsage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            total_tokens=response.usage.total_tokens if response.usage else 0,
        )
        return GenerationResult(text=text, usage=usage, model=model, provider=self.name)

    async def generate_stream(
        self,
        messages: list[dict],
        model: str = "default",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[str]:
        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


# Default task → model routing
DEFAULT_TASK_ROUTING: dict[str, ModelConfig] = {
    "generation": ModelConfig(provider="anthropic", model_name="claude-sonnet-4-20250514", temperature=0.8, max_tokens=4096),
    "polishing": ModelConfig(provider="anthropic", model_name="claude-sonnet-4-20250514", temperature=0.7, max_tokens=4096),
    "outline": ModelConfig(provider="anthropic", model_name="claude-sonnet-4-20250514", temperature=0.8, max_tokens=8192),
    "extraction": ModelConfig(provider="anthropic", model_name="claude-haiku-4-5-20251001", temperature=0.3, max_tokens=2048),
    "evaluation": ModelConfig(provider="openai", model_name="gpt-4o", temperature=0.3, max_tokens=2048),
    "summary": ModelConfig(provider="anthropic", model_name="claude-haiku-4-5-20251001", temperature=0.3, max_tokens=1024),
    "embedding": ModelConfig(provider="openai", model_name="text-embedding-3-small"),
}


class ModelRouter:
    """
    Unified model access layer.

    Routes tasks to appropriate models, supports fallback chains,
    and tracks token usage.
    """

    def __init__(self):
        self.providers: dict[str, BaseProvider] = {}
        self.task_routing: dict[str, ModelConfig] = dict(DEFAULT_TASK_ROUTING)
        self.total_usage = TokenUsage()

    def register_provider(self, provider: BaseProvider) -> None:
        self.providers[provider.name] = provider

    def set_task_routing(self, task_type: str, config: ModelConfig) -> None:
        self.task_routing[task_type] = config

    def _get_config(self, task_type: str) -> ModelConfig:
        config = self.task_routing.get(task_type)
        if not config:
            raise ValueError(f"No model config for task type: {task_type}")
        return config

    def _get_provider(self, provider_name: str) -> BaseProvider:
        provider = self.providers.get(provider_name)
        if not provider:
            available = list(self.providers.keys())
            if available:
                logger.warning(
                    "Provider %s not available, falling back to %s",
                    provider_name,
                    available[0],
                )
                return self.providers[available[0]]
            raise ValueError(f"No providers registered. Requested: {provider_name}")
        return provider

    def _track_usage(self, usage: TokenUsage) -> None:
        self.total_usage.input_tokens += usage.input_tokens
        self.total_usage.output_tokens += usage.output_tokens
        self.total_usage.total_tokens += usage.total_tokens

    async def generate(
        self,
        task_type: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> GenerationResult:
        config = self._get_config(task_type)
        provider = self._get_provider(config.provider)

        result = await provider.generate(
            messages=messages,
            model=config.model_name,
            temperature=temperature if temperature is not None else config.temperature,
            max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
            **kwargs,
        )
        self._track_usage(result.usage)
        logger.info(
            "Generated [%s] via %s/%s: %d tokens",
            task_type,
            result.provider,
            result.model,
            result.usage.total_tokens,
        )
        return result

    async def generate_stream(
        self,
        task_type: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        config = self._get_config(task_type)
        provider = self._get_provider(config.provider)

        async for chunk in provider.generate_stream(
            messages=messages,
            model=config.model_name,
            temperature=temperature if temperature is not None else config.temperature,
            max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
            **kwargs,
        ):
            yield chunk

    async def generate_with_fallback(
        self,
        task_type: str,
        messages: list[dict],
        fallback_providers: list[str] | None = None,
        **kwargs,
    ) -> GenerationResult:
        """Try primary provider, then fallback providers in order."""
        config = self._get_config(task_type)
        providers_to_try = [config.provider]
        if fallback_providers:
            providers_to_try.extend(fallback_providers)
        else:
            providers_to_try.extend(
                name for name in self.providers if name != config.provider
            )

        last_error: Exception | None = None
        for provider_name in providers_to_try:
            if provider_name not in self.providers:
                continue
            try:
                provider = self.providers[provider_name]
                result = await provider.generate(
                    messages=messages,
                    model=config.model_name if provider_name == config.provider else self._default_model_for(provider_name),
                    temperature=kwargs.get("temperature", config.temperature),
                    max_tokens=kwargs.get("max_tokens", config.max_tokens),
                )
                self._track_usage(result.usage)
                return result
            except Exception as e:
                logger.warning("Provider %s failed: %s", provider_name, e)
                last_error = e
                continue

        raise RuntimeError(f"All providers failed for task {task_type}") from last_error

    def _default_model_for(self, provider_name: str) -> str:
        defaults = {
            "anthropic": "claude-sonnet-4-20250514",
            "openai": "gpt-4o",
            "openai_compatible": "default",
        }
        return defaults.get(provider_name, "default")

    def get_usage_stats(self) -> dict:
        return {
            "total_input_tokens": self.total_usage.input_tokens,
            "total_output_tokens": self.total_usage.output_tokens,
            "total_tokens": self.total_usage.total_tokens,
        }


def create_model_router() -> ModelRouter:
    """Factory function to create ModelRouter with configured providers."""
    from app.config import settings

    router = ModelRouter()

    if settings.ANTHROPIC_API_KEY:
        router.register_provider(AnthropicProvider(settings.ANTHROPIC_API_KEY))
        logger.info("Registered Anthropic provider")

    if settings.OPENAI_API_KEY:
        router.register_provider(OpenAIProvider(settings.OPENAI_API_KEY))
        logger.info("Registered OpenAI provider")

    if settings.OPENAI_COMPATIBLE_BASE_URL:
        router.register_provider(
            OpenAICompatibleProvider(
                base_url=settings.OPENAI_COMPATIBLE_BASE_URL,
                api_key=settings.OPENAI_COMPATIBLE_API_KEY or "",
            )
        )
        logger.info("Registered OpenAI-compatible provider at %s", settings.OPENAI_COMPATIBLE_BASE_URL)

    if not router.providers:
        logger.warning("No LLM providers configured! Set API keys in .env")

    return router


# Singleton instance
_router: ModelRouter | None = None


def get_model_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = create_model_router()
    return _router
