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
import json
import logging
import math
import time
from collections.abc import Sequence
from contextlib import aclosing
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator

from app.services.provider_response_guard import guard_chat_stream, reject_relay_error
from app.observability.metrics import time_llm_call

logger = logging.getLogger(__name__)

# Each process re-reads committed configuration before starting a new call
# once this monotonic cache lifetime expires. In-flight calls keep their router.
ROUTER_TTL_SECONDS = 30.0


async def _commit_llm_call_log(
    db,
    *,
    operation: str,
    task_type: str,
    model: str,
) -> None:
    """Best-effort commit for LLM call telemetry sessions.

    LLM call logging must not turn a successful provider response into a
    failed generation. Long streaming responses can outlive a DB connection;
    when that happens, drop the telemetry row and let the caller continue.
    """
    try:
        await db.commit()
    except Exception as exc:
        logger.warning(
            "LLM call log commit failed (operation=%s task_type=%s model=%s): %s",
            operation,
            task_type,
            model,
            exc,
        )
        try:
            await db.rollback()
        except Exception as rollback_exc:
            logger.warning(
                "LLM call log rollback failed after commit error "
                "(operation=%s task_type=%s model=%s): %s",
                operation,
                task_type,
                model,
                rollback_exc,
            )


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


def _model_matches_setting(model: str, setting_name: str) -> bool:
    """True when ``model`` contains any comma-separated substring pattern
    stored in ``settings.<setting_name>`` (e.g. ``"claude-"``)."""
    from app.config import settings

    raw = getattr(settings, setting_name, "") or ""
    patterns = [p.strip() for p in raw.split(",") if p.strip()]
    return bool(model) and any(p in model for p in patterns)


def _should_merge_system_for_model(model: str) -> bool:
    """True when this model name matches settings.LLM_MERGE_SYSTEM_INTO_USER_MODELS.

    Comma-separated substring patterns (e.g. ``"claude-"``). Workaround for
    relay channels that drop/replace the system role (observed 2026-07-26 on
    relay claude-* — an agent shell swallowed system messages, so scene_planner
    / scene_writer / evaluators lost all story context).
    """
    return _model_matches_setting(model, "LLM_MERGE_SYSTEM_INTO_USER_MODELS")


def _should_force_internal_stream_for_model(model: str) -> bool:
    """True when this model name matches settings.LLM_FORCE_INTERNAL_STREAM_MODELS.

    Comma-separated substring patterns (same matching style as
    ``LLM_MERGE_SYSTEM_INTO_USER_MODELS``). Workaround for relay channels whose
    NON-streaming completions burn the whole output budget on hidden thinking
    and return empty text with ``output_tokens == max_tokens`` (observed
    2026-07-26 on relay claude-* — streaming the identical request works).
    Matching models have ``OpenAIProvider.generate`` open a stream internally
    and assemble the full text before returning, so callers that requested a
    non-streaming completion (evaluation / drafter / rewrite) still receive a
    plain completed ``GenerationResult``.
    """
    return _model_matches_setting(model, "LLM_FORCE_INTERNAL_STREAM_MODELS")


def merge_system_into_user(messages: list[dict]) -> list[dict]:
    """Fold all system-role content into the first user message (non-mutating).

    The system block is prefixed under a 【系统指令】 header ahead of the user
    ask so instruction priority reads top-down. History order of user /
    assistant turns is preserved. No system messages → returned unchanged.
    """
    sys_parts = [
        (m.get("content") or "")
        for m in messages
        if m.get("role") == "system" and (m.get("content") or "").strip()
    ]
    if not sys_parts:
        return messages
    sys_block = "\n\n".join(sys_parts)
    rest = [m for m in messages if m.get("role") != "system"]
    out: list[dict] = []
    merged = False
    for m in rest:
        if not merged and m.get("role") == "user":
            out.append(
                {
                    **m,
                    "content": (
                        f"【系统指令（必须严格遵守）】\n{sys_block}\n\n"
                        f"【用户输入】\n{m.get('content') or ''}"
                    ),
                }
            )
            merged = True
        else:
            out.append(m)
    if not merged:
        out.insert(0, {"role": "user", "content": f"【系统指令（必须严格遵守）】\n{sys_block}"})
    return out


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
    finish_reason: str | None = None


def _env_extra_body() -> dict:
    """平台级额外请求体(JSON 对象),默认空。

    用途:给"先思考后输出"的模型在当前网关上关掉思考。实测(2026-11-17)
    ``deepseek-v4-flash`` 在长 prompt 下首段正文要等 **26.0s**(期间只有
    ``reasoning_content``),而本网关的流式请求约 60s 就被关闭、客户端看到
    ``upstream stream closed before any chunk carried finish_reason`` —— 规划与正文
    步骤直接失败;把思考关掉后首正文 **2.5s**、总量 10.9s,链路正常。

    运维用法(不改代码):

        LLM_EXTRA_BODY_JSON='{"thinking":{"type":"disabled"}}'

    非法 JSON 忽略并告警:一个拼错的变量绝不该影响所有模型的所有调用。
    """
    raw = (_os_cache.getenv("LLM_EXTRA_BODY_JSON") or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        logger.warning("LLM_EXTRA_BODY_JSON is not valid JSON; ignored")
        return {}
    if not isinstance(value, dict):
        logger.warning("LLM_EXTRA_BODY_JSON must be a JSON object; ignored")
        return {}
    return value

# ---------------------------------------------------------------------------
# 2026-11-17: 按任务类型强制非流式(``LLM_NONSTREAM_TASKS``)
# ---------------------------------------------------------------------------
#: 环境变量名;读取顺序 = 环境变量 > ``settings.LLM_NONSTREAM_TASKS``。
NONSTREAM_TASKS_ENV = "LLM_NONSTREAM_TASKS"
#: 通配符:命中**全部**任务(空任务名也算命中)。
NONSTREAM_TASKS_ALL = "*"


def _nonstream_tasks() -> frozenset[str]:
    """返回"一律走非流式"的任务类型集合(小写、去空格;空串被丢弃)。

    读取顺序:环境变量 ``LLM_NONSTREAM_TASKS`` > ``settings.LLM_NONSTREAM_TASKS``;
    环境变量为空/空白时视为未设置(回落配置项);两者都给不出内容 = 空集 = 关闭。

    容忍性:空格、空项(``"a, ,b,"``)、大小写;非法或未知任务名不报错 ——
    它只是永远匹配不上任何 ``task_type``。与 ``_env_extra_body`` 同一风格:
    一个拼错的开关绝不该影响所有模型的所有调用。集合里可能包含 ``"*"``。
    """
    raw = _os_cache.getenv(NONSTREAM_TASKS_ENV)
    if raw is None or not raw.strip():
        from app.config import settings

        raw = getattr(settings, "LLM_NONSTREAM_TASKS", "") or ""
    names = {part.strip().lower() for part in raw.split(",")}
    names.discard("")
    return frozenset(names)


def _should_force_nonstream(task_type: str | None) -> bool:
    """这条调用是否必须走非流式(默认 False ⇒ 行为与改动前完全一致)。

    ``*`` 命中所有任务(含任务名为空/未知);否则按任务名精确匹配(忽略大小写
    与首尾空格)。未知任务名不会抛错,只是不命中。
    """
    tasks = _nonstream_tasks()
    if not tasks:
        return False
    if NONSTREAM_TASKS_ALL in tasks:
        return True
    name = (task_type or "").strip().lower()
    return bool(name) and name in tasks


def _log_forced_nonstream(task_type: str | None, where: str) -> None:
    """命中开关时打一条 INFO:事后能确认开关真的生效(含任务名与原因)。"""
    logger.info(
        "LLM_NONSTREAM_TASKS hit: forcing non-streaming call "
        "(task_type=%r at %s; reason=%s)",
        task_type,
        where,
        "task configured to avoid gateways that close long SSE streams mid-thinking",
    )


def _apply_nonstream_override(kw: dict, task_type: str | None, where: str) -> bool:
    """非流式生成入口用:命中开关时把 ``stream=False`` 写进 provider 调用参数。

    返回是否命中。``OpenAIProvider.generate`` 的默认值是 ``stream=True``
    (即"默认流式"),所以只有显式传 ``stream=False`` 才能拿到一次性响应。
    """
    if not _should_force_nonstream(task_type):
        return False
    _log_forced_nonstream(task_type, where)
    kw["stream"] = False
    return True




class EmbeddingUnavailableError(RuntimeError):
    """没有任何可用的 embedding 端点(配置缺失/全部候选失败)。

    2026-11-17 审计 F3:此前 ``ModelRouter.embed`` 在这种情况下返回 1536 维
    零向量。零向量与任何查询等距,检索退化成随机顺序,且维度与当前模型
    (2048)不符会让 Qdrant 写入直接失败 —— 必须显式失败。
    """
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



# ---- v1.6.0 X1/Y1/Y2: prompt cache helpers (env-gated) ----
import os as _os_cache

_ANTHROPIC_CACHE_ENABLED = _os_cache.getenv("ANTHROPIC_PROMPT_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
_ANTHROPIC_CACHE_MIN_CHARS = int(_os_cache.getenv("ANTHROPIC_CACHE_MIN_CHARS", "4096"))  # ~1024 tokens
_OPENAI_CACHE_ENABLED = _os_cache.getenv("OPENAI_PROMPT_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
_OPENAI_COMPAT_RATE_LIMIT_ENABLED = _os_cache.getenv("OPENAI_COMPAT_RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes")
_OPENAI_COMPAT_MIN_INTERVAL_SECONDS = float(_os_cache.getenv("OPENAI_COMPAT_MIN_INTERVAL_SECONDS", "8"))
_OPENAI_COMPAT_EMPTY_COOLDOWN_SECONDS = float(_os_cache.getenv("OPENAI_COMPAT_EMPTY_COOLDOWN_SECONDS", "25"))
_OPENAI_COMPAT_CLIENT_MAX_RETRIES = int(_os_cache.getenv("OPENAI_COMPAT_CLIENT_MAX_RETRIES", "0"))

_OPENAI_COMPAT_RATE_LOCKS: dict[str, asyncio.Lock] = {}
_OPENAI_COMPAT_NEXT_AT: dict[str, float] = {}
_OPENAI_COMPAT_REGISTRY_LOCK = asyncio.Lock()

# Streaming usage probe: base_urls (rstripped, "" = native OpenAI) that
# rejected ``stream_options={"include_usage": True}`` with a 400 naming the
# field. Per-process negative cache — those endpoints keep streaming without
# usage instead of failing every call. See OpenAIProvider._create_chat_stream.
_STREAM_OPTIONS_UNSUPPORTED: set[str] = set()


def _openai_compat_rate_key(base_url: str, model: str) -> str:
    return f"{(base_url or 'openai').rstrip('/')}::{model or 'default'}"


async def _get_openai_compat_lock(key: str) -> asyncio.Lock:
    async with _OPENAI_COMPAT_REGISTRY_LOCK:
        lock = _OPENAI_COMPAT_RATE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _OPENAI_COMPAT_RATE_LOCKS[key] = lock
        return lock


async def _wait_openai_compat_turn(base_url: str, model: str, task_type: str) -> None:
    """Serialize OpenAI-compatible endpoints that may soft-limit rapid calls."""
    if not _OPENAI_COMPAT_RATE_LIMIT_ENABLED or not base_url:
        return
    key = _openai_compat_rate_key(base_url, model)
    lock = await _get_openai_compat_lock(key)
    async with lock:
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait_for = max((_OPENAI_COMPAT_NEXT_AT.get(key, 0.0) - now), 0.0)
        if wait_for > 0:
            logger.info(
                "OpenAI-compatible endpoint throttle sleeping %.1fs (task=%s model=%s base_url=%s)",
                wait_for,
                task_type,
                model,
                base_url,
            )
            await asyncio.sleep(wait_for)
        _OPENAI_COMPAT_NEXT_AT[key] = loop.time() + max(_OPENAI_COMPAT_MIN_INTERVAL_SECONDS, 0.0)


async def _cooldown_openai_compat_empty(base_url: str, model: str, task_type: str) -> None:
    """Back off after HTTP-200 empty completions, common with soft rate limits."""
    if not _OPENAI_COMPAT_RATE_LIMIT_ENABLED or not base_url:
        return
    key = _openai_compat_rate_key(base_url, model)
    lock = await _get_openai_compat_lock(key)
    async with lock:
        loop = asyncio.get_running_loop()
        cooldown_until = loop.time() + max(_OPENAI_COMPAT_EMPTY_COOLDOWN_SECONDS, 0.0)
        _OPENAI_COMPAT_NEXT_AT[key] = max(_OPENAI_COMPAT_NEXT_AT.get(key, 0.0), cooldown_until)
    logger.warning(
        "OpenAI-compatible endpoint returned empty text; cooling down %.1fs (task=%s model=%s base_url=%s)",
        _OPENAI_COMPAT_EMPTY_COOLDOWN_SECONDS,
        task_type,
        model,
        base_url,
    )


def _record_cache_tokens(task_type: str, provider: str, model: str, *, create: int = 0, read: int = 0, uncached: int = 0) -> None:
    """Best-effort: record cache token splits to Prometheus. Safe if observability missing."""
    try:
        from app.observability.metrics import LLM_CACHE_TOKEN_TOTAL
        if create:
            LLM_CACHE_TOKEN_TOTAL.labels(task_type=task_type or "unknown", provider=provider, model=model, kind="cache_create").inc(create)
        if read:
            LLM_CACHE_TOKEN_TOTAL.labels(task_type=task_type or "unknown", provider=provider, model=model, kind="cache_read").inc(read)
        if uncached:
            LLM_CACHE_TOKEN_TOTAL.labels(task_type=task_type or "unknown", provider=provider, model=model, kind="cache_uncached").inc(uncached)
    except Exception:
        pass


def _cached_tokens_of(provider_usage) -> int:
    """a1001960: prompt-cache hits reported by an OpenAI-style usage object.

    ``prompt_tokens_details.cached_tokens`` is optional (relays often omit
    it), so a missing/None value means "unknown", i.e. 0 — never a guess.
    """
    details = getattr(provider_usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    try:
        return max(int(getattr(details, "cached_tokens", 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _fill_usage_box(box: dict, usage: "TokenUsage", cached_tokens: int) -> None:
    """Mirror a call's token usage into ``usage_box`` for LLMCallLog.

    Only bookkeeping: the box is a plain dict handed down by
    ``ModelRouter`` so ``log_llm_call`` can persist what the provider
    actually billed. It never influences retries or fallback decisions.
    """
    try:
        box["input_tokens"] = int(usage.input_tokens or 0)
        box["output_tokens"] = int(usage.output_tokens or 0)
        # a1001960: written only when there actually were hits, so the box
        # keeps its old two-key shape for every caller that does not care
        # about cache. Missing key means "no hits", never "unknown".
        cached = max(int(cached_tokens or 0), 0)
        if cached:
            box["cached_input_tokens"] = cached
    except Exception:
        pass


# ---------------------------------------------------------------------------
# a1001961: token estimation for providers that report no usage at all
# ---------------------------------------------------------------------------
#: 字符/token 的默认比例。2026-11-15 对拍实测:中文正文约 1.43–1.48
#: 汉字/token。取 1.5 落在实测区间的"每 token 更多字"一端,即估算值比实测略
#: 低几个百分点 —— 宁可略低估,也不把估算当成"供应商确实用了这么多"的证据。
#: 换模型 / 换语种可经 ``TOKEN_ESTIMATE_CHARS_PER_TOKEN`` 覆盖。
CHARS_PER_TOKEN = 1.5
_CHARS_PER_TOKEN_ENV = "TOKEN_ESTIMATE_CHARS_PER_TOKEN"
_chars_per_token_warned = False


def _chars_per_token() -> float:
    """解析 ``TOKEN_ESTIMATE_CHARS_PER_TOKEN``;非法值回落默认并只告警一次。

    每次调用都读环境(便于运维不重启就改比例,也便于离线用例用
    ``monkeypatch.setenv`` 覆盖),解析失败只会让日志里出现一条 warning。
    """
    global _chars_per_token_warned
    raw = _os_cache.getenv(_CHARS_PER_TOKEN_ENV)
    if raw is None or not raw.strip():
        return CHARS_PER_TOKEN
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        if not _chars_per_token_warned:
            _chars_per_token_warned = True
            logger.warning(
                "Invalid %s=%r; falling back to %.2f chars/token",
                _CHARS_PER_TOKEN_ENV, raw, CHARS_PER_TOKEN)
        return CHARS_PER_TOKEN
    return value


def estimate_tokens(chars: int) -> int:
    """按字符数估 token:非正数 → 0;否则最少 1(有字就至少一个 token)。"""
    try:
        count = int(chars)
    except (TypeError, ValueError):
        return 0
    if count <= 0:
        return 0
    return max(1, round(count / _chars_per_token()))


def _prompt_texts(messages) -> list[str]:
    """Prompt 里的纯文本片段(多模态 content 数组只取 text 部分)。"""
    texts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    texts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return texts


def _estimate_from_texts(
    prompt_texts: Sequence[str], output_text: str
) -> tuple[int, int]:
    """(估算输入 token, 估算输出 token):两侧各自按字符数算,互不混算。"""
    prompt_chars = sum(len(text) for text in prompt_texts or () if isinstance(text, str))
    output_chars = len(output_text) if isinstance(output_text, str) else 0
    return estimate_tokens(prompt_chars), estimate_tokens(output_chars)


def _resolve_usage_box(
    box: dict,
    usage: "TokenUsage | None",
    prompt_texts: Sequence[str],
    output_text: str,
) -> None:
    """让 ``usage_box`` 带上"这条调用花了多少 token",优先级从高到低:

    1. 供应商自己报的数(适配器已镜像进 box)→ 原样保留,不打估算标记;
    2. ``GenerationResult.usage`` 已知的真实数(box 没被适配器填)→ 抄进去;
    3. **两边都是 0 且输出非空** → 按字符数估算,并置 ``usage_estimated``。

    刻意只写 box,不碰 ``TokenUsage`` / ``GenerationResult.usage``:截断判定
    (``output_tokens >= max_tokens * 0.95``)必须继续看真实用量,拿估算值去
    喂它会产生假截断告警。
    """
    try:
        box_in = int(box.get("input_tokens") or 0)
        box_out = int(box.get("output_tokens") or 0)
        if box_in or box_out:
            return
        if usage is not None:
            real_in = int(getattr(usage, "input_tokens", 0) or 0)
            real_out = int(getattr(usage, "output_tokens", 0) or 0)
        else:
            real_in = real_out = 0
        if real_in or real_out:
            # 供应商报了数,只是没经过 _fill_usage_box:照抄,绝不当估算。
            box["input_tokens"] = real_in
            box["output_tokens"] = real_out
            box["usage_estimated"] = False
            return
        if not output_text:
            # 空输出无从估算(空回执 / 思考耗尽那条路径),保持 0。
            return
        est_in, est_out = _estimate_from_texts(prompt_texts, output_text)
        box["input_tokens"] = est_in
        box["output_tokens"] = est_out
        box["usage_estimated"] = True
    except Exception:
        pass


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
        # a1001960: optional LLMCallLog side-channel (see OpenAIProvider).
        usage_box = kw.pop("usage_box", None)
        system_msg, chat = None, []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat.append(m)
        params: dict = {"model": model, "max_tokens": max_tokens,
                        "temperature": temperature, "messages": chat}
        # v1.6.0 Y1: prompt cache via cache_control on long system block
        cache_used = False
        if system_msg:
            if _ANTHROPIC_CACHE_ENABLED and len(system_msg) >= _ANTHROPIC_CACHE_MIN_CHARS:
                params["system"] = [{"type": "text", "text": system_msg, "cache_control": {"type": "ephemeral"}}]
                cache_used = True
            else:
                params["system"] = system_msg
        resp = await self.client.messages.create(**params)
        text_parts = []
        for block in resp.content or []:
            if getattr(block, "type", None) != "text":
                continue
            value = getattr(block, "text", None)
            if not isinstance(value, str):
                raise ValueError("invalid_provider_text_block")
            text_parts.append(value)
        text = "".join(text_parts)
        u = resp.usage
        in_tok = getattr(u, "input_tokens", 0) or 0
        out_tok = getattr(u, "output_tokens", 0) or 0
        cache_create = getattr(u, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        usage = TokenUsage(in_tok + cache_create + cache_read, out_tok,
                           in_tok + cache_create + cache_read + out_tok)
        if cache_used or cache_create or cache_read:
            _record_cache_tokens(kw.get("task_type", "unknown"), self.name, model,
                                  create=cache_create, read=cache_read, uncached=in_tok)
        if usage_box is not None:
            # cache_read is already part of usage.input_tokens (see above), so
            # it is persisted as a subset, never added on top.
            _fill_usage_box(usage_box, usage, cache_read)
        return GenerationResult(text=text, usage=usage, model=model, provider=self.name,
                                finish_reason=getattr(resp, "stop_reason", None))

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
        # v1.6.0 Y1: cache_control on system for stream too
        if system_msg:
            if _ANTHROPIC_CACHE_ENABLED and len(system_msg) >= _ANTHROPIC_CACHE_MIN_CHARS:
                params["system"] = [{"type": "text", "text": system_msg, "cache_control": {"type": "ephemeral"}}]
            else:
                params["system"] = system_msg
        async with self.client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text
            # Completion is part of the result contract, not optional telemetry.
            # Propagate failure after yielded text so callers retain a failed
            # candidate instead of accepting a truncated chapter.
            from app.services.provider_response_guard import require_normal_finish
            final = await stream.get_final_message()
            require_normal_finish(getattr(final, 'stop_reason', None))
            # Best-effort: cache metrics must not affect valid prose delivery.
            try:
                fu = getattr(final, "usage", None)
                if fu is not None:
                    create = getattr(fu, "cache_creation_input_tokens", 0) or 0
                    read = getattr(fu, "cache_read_input_tokens", 0) or 0
                    uncached = getattr(fu, "input_tokens", 0) or 0
                    _record_cache_tokens(
                        kw.get("task_type", "unknown"), self.name, model,
                        create=create, read=read, uncached=uncached,
                    )
                    # 2026-11-17: 这条路径此前只把用量写进 Prometheus,数据库日志
                    # 一直是 0/0 —— 用页面看不到 Anthropic 流式的任何消耗。
                    # 口径与非流式一致:input = 未缓存 + 缓存写入 + 缓存命中。
                    box = kw.get("usage_box")
                    if isinstance(box, dict):
                        box["input_tokens"] = int(uncached) + int(create) + int(read)
                        box["output_tokens"] = int(getattr(fu, "output_tokens", 0) or 0)
                        if int(read):
                            box["cached_input_tokens"] = int(read)
                        box["usage_estimated"] = False
            except Exception:
                pass


def _resolve_nonstream_timeout(request_timeout, task_type: str) -> float:
    """Evaluation reads ~13K chars of Chinese prose; 45s was causing silent
    timeouts that surfaced as all-zero scores. Default is env-tunable."""
    if request_timeout is not None:
        return request_timeout
    if task_type in {"evaluation", "chapter_evaluation"}:
        return float(_os_cache.getenv("EVALUATION_REQUEST_TIMEOUT", "120"))
    # 2026-11-17: 非流式默认 45s,对"先思考后输出"的模型(规划步常 60–120s)太短。
    # 运维可用 LLM_NONSTREAM_TIMEOUT_SECONDS 调大;不设置时行为不变(45s)。
    override = (_os_cache.getenv("LLM_NONSTREAM_TIMEOUT_SECONDS") or "").strip()
    if override:
        try:
            value = float(override)
            if value > 0:
                return value
        except ValueError:
            logger.warning("LLM_NONSTREAM_TIMEOUT_SECONDS is not a number; ignored")
    return 45


def _raise_if_empty_thinking_burn(
    usage: "TokenUsage", max_tokens: int, *, model: str, task_type: str
) -> None:
    """Empty text + output budget exhausted ⇒ raise a retryable error.

    E2E evidence (2026-07-26): relay claude-* returned HTTP-200 completions
    with empty content and ``output_tokens == max_tokens`` — the upstream spent
    the whole budget on hidden thinking. Returning that as success poisons the
    pipeline (evaluator JSON parse fails → all-zero scores; rewrites come back
    empty → chapter permanently blocked). Raising ``LLMEmptyCompletionError``
    (recognized by ``llm_retry._is_retryable``) lets the existing attempt
    budget retry in place, and lets tier fallback move endpoints when the
    budget is spent — instead of success-with-empty. The 95% threshold
    tolerates relays whose token accounting is slightly off; an empty response
    below it keeps the old behavior (cooldown, empty result returned).
    """
    if max_tokens > 0 and usage.output_tokens >= int(max_tokens * 0.95):
        from app.services.llm_retry import LLMEmptyCompletionError

        raise LLMEmptyCompletionError(
            f"empty completion with output budget exhausted "
            f"(model={model} task={task_type} "
            f"output_tokens={usage.output_tokens} max_tokens={max_tokens})"
        )


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
            kw: dict = {
                "api_key": self.api_key or "not-needed",
                "max_retries": max(_OPENAI_COMPAT_CLIENT_MAX_RETRIES, 0),
            }
            if self.base_url:
                kw["base_url"] = self.base_url
            self._client = openai.AsyncOpenAI(**kw)
        return self._client

    async def generate(self, messages, model="gpt-4o",
                       temperature=0.7, max_tokens=8192, **kw) -> GenerationResult:
        # v1.6.0 Y2: prompt_cache_key boosts hit-rate on long stable prefixes
        # Only for native OpenAI API — compatible endpoints (xAI, etc.) reject unknown fields
        extra_body: dict = _env_extra_body()
        task_type = kw.get("task_type", "unknown")
        stream_mode = kw.pop("stream", True)
        # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务即使用户传了 stream=True,
        # 也走非流式一次性响应(这类任务的模型思考很重,网关会提前关掉 SSE)。
        forced_nonstream = _should_force_nonstream(task_type)
        if forced_nonstream and stream_mode is not False:
            _log_forced_nonstream(task_type, "OpenAIProvider.generate")
            stream_mode = False
        request_timeout = kw.pop("request_timeout", None)
        retry_attempts = kw.pop("retry_attempts", None)
        # a1001960: optional usage side-channel for LLMCallLog
        # (input/output/cached_input_tokens). Absent for every caller that
        # does not want the numbers mirrored — behavior unchanged either way.
        usage_box = kw.pop("usage_box", None)
        # Relay workaround: some channels drop the system role for claude-*
        # models; fold system into the first user message before sending.
        if _should_merge_system_for_model(model):
            messages = merge_system_into_user(messages)
        if _OPENAI_CACHE_ENABLED and not self.base_url:
            extra_body["prompt_cache_key"] = f"{task_type}:{model}"

        # v1.14: wrap the streaming chat call with bounded retries for
        # transient transport blips (httpx ConnectError, ReadTimeout,
        # openai APIConnectionError / RateLimitError / 5xx). A failed
        # attempt discards its partial chunks and starts fresh next try.
        from app.services.llm_retry import call_with_retry

        # Relay thinking-burn workaround: for matching models, a non-streaming
        # caller (evaluation / stream=False) is served via the internally
        # streamed branch below — the full text is assembled before returning,
        # so caller semantics are unchanged.
        caller_nonstream = (
            task_type in {"evaluation", "chapter_evaluation"}
            or stream_mode is False
            or forced_nonstream
        )
        # ``LLM_NONSTREAM_TASKS`` 是运维明确的"别用流式"要求,优先级高于按模型
        # 内部流式的兜底(``LLM_FORCE_INTERNAL_STREAM_MODELS``):否则命中开关的
        # 任务又被绕回内部流式,还是会撞上"流被提前关闭"。
        force_internal_stream = (
            caller_nonstream
            and not forced_nonstream
            and _should_force_internal_stream_for_model(model)
        )

        async def _one_attempt() -> GenerationResult:
            # Evaluation expects one bounded JSON object. For direct chapter
            # generation we now honor `stream=False` so callers can opt into a
            # full non-streaming completion and avoid transport-side SSE issues.
            await _wait_openai_compat_turn(self.base_url, model, task_type)
            if caller_nonstream and not force_internal_stream:
                timeout = _resolve_nonstream_timeout(request_timeout, task_type)
                resp = await self.client.chat.completions.create(
                    model=model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens,
                    stream=False,
                    extra_body=extra_body or None,
                    timeout=timeout,
                )
                text = ""
                if resp.choices and resp.choices[0].message:
                    text = resp.choices[0].message.content or ""
                reject_relay_error(text)
                u = getattr(resp, "usage", None)
                usage = TokenUsage(
                    getattr(u, 'prompt_tokens', 0) or 0,
                    getattr(u, 'completion_tokens', 0) or 0,
                    getattr(u, 'total_tokens', 0) or 0,
                ) if u else TokenUsage()
                cached_tokens = _cached_tokens_of(u)
                if usage_box is not None:
                    _fill_usage_box(usage_box, usage, cached_tokens)
                    # a1001961: relay may report no usage at all; estimate then,
                    # never over the provider's own numbers.
                    _resolve_usage_box(usage_box, usage, _prompt_texts(messages), text)
                if not text.strip():
                    await _cooldown_openai_compat_empty(self.base_url, model, task_type)
                    _raise_if_empty_thinking_burn(
                        usage, max_tokens, model=model, task_type=task_type)
                return GenerationResult(text=text, usage=usage, model=model, provider=self.name,
                                        finish_reason=getattr(resp.choices[0], "finish_reason", None) if resp.choices else None)

            # Internally-streamed completion. Reached when the caller streams
            # (historical path) or when force_internal_stream rerouted a
            # non-streaming caller; the latter keeps its non-stream timeout
            # semantics (httpx applies it per read gap on a stream).
            stream_timeout = request_timeout
            if force_internal_stream:
                stream_timeout = _resolve_nonstream_timeout(request_timeout, task_type)
            chunks: list[str] = []
            stream = await self._create_chat_stream(dict(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens,
                extra_body=extra_body or None,
                timeout=stream_timeout,
            ))
            usage = TokenUsage()
            cached_tokens = 0
            finish_reason = None
            try:
                async for chunk in stream:
                    if chunk.choices and getattr(chunk.choices[0], "finish_reason", None) is not None:
                        finish_reason = chunk.choices[0].finish_reason
                    _delta = getattr(chunk.choices[0], "delta", None) if chunk.choices else None
                    _content = getattr(_delta, "content", None)
                    if _content:
                        chunks.append(_content)
                    if hasattr(chunk, 'usage') and chunk.usage:
                        u = chunk.usage
                        usage = TokenUsage(
                            getattr(u, 'prompt_tokens', 0) or 0,
                            getattr(u, 'completion_tokens', 0) or 0,
                            getattr(u, 'total_tokens', 0) or 0)
                        ptd = getattr(u, 'prompt_tokens_details', None)
                        if ptd is not None:
                            cached_tokens = getattr(ptd, 'cached_tokens', 0) or 0
            finally:
                # AsyncStream owns an HTTP response even when evaluation uses
                # this internal streaming path. Release it on cancel/error too.
                close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as close_error:
                        logger.warning("Failed to close model stream: %s", type(close_error).__name__)
            if _OPENAI_CACHE_ENABLED and usage.input_tokens:
                uncached = max(usage.input_tokens - cached_tokens, 0)
                _record_cache_tokens(task_type, self.name, model,
                                     read=cached_tokens, uncached=uncached)
            text = "".join(chunks)
            if usage_box is not None:
                _fill_usage_box(usage_box, usage, cached_tokens)
                # a1001961: estimate only when the provider reported nothing.
                _resolve_usage_box(usage_box, usage, _prompt_texts(messages), text)
            reject_relay_error(text)
            if not text.strip():
                await _cooldown_openai_compat_empty(self.base_url, model, task_type)
                _raise_if_empty_thinking_burn(
                    usage, max_tokens, model=model, task_type=task_type)
            return GenerationResult(text=text, usage=usage, model=model, provider=self.name,
                                    finish_reason=finish_reason)

        # 2026-11-17 审计 F8 复核:非流式仍是单发 —— 这是**有意设计**
        # (见 force_internal_stream 分支与 test_force_internal_stream:
        # 真非流式只跑一轮,重试交给 tier fallback / 任务级重试,避免把
        # LLM_NONSTREAM_TIMEOUT_SECONDS 成倍叠加)。因此不改这里的默认值,
        # 只保留这条说明,免得下次又被当成漏改。
        attempts = 1 if caller_nonstream else 4
        if retry_attempts is not None and int(retry_attempts) > 0:
            attempts = int(retry_attempts)
        if force_internal_stream:
            # Floor at 2: non-streaming callers default to (or explicitly pass)
            # attempts=1 to bound long non-stream timeouts, but that single
            # attempt would defeat the retryable empty-thinking-burn fix — the
            # first burned response would propagate instead of healing in
            # place. Internally-streamed empty attempts fail fast (no long
            # timeout to stack), so one in-place retry is safe.
            attempts = max(attempts, 2)
        return await call_with_retry(
            _one_attempt, label=f"openai_chat[{task_type}:{model}]",
            attempts=attempts,
        )

    async def _create_chat_stream(self, create_kw: dict):
        """Open a chat completions stream, probing ``stream_options.include_usage``.

        Streaming calls only get token usage when the final chunk is requested
        via ``stream_options={"include_usage": True}``. Native OpenAI and most
        relays accept the field, but some OpenAI-compatible endpoints reject
        unknown fields with HTTP 400 (which is why it was previously only sent
        when ``base_url`` was unset). Probe-safe approach: always try with the
        field; on a 400 raised while it was present, retry once without it,
        and — when the error message names the field — stop sending it to this
        base_url for the rest of the process. Endpoints that silently ignore
        the field keep working (usage simply stays 0).
        """
        create_kw = dict(create_kw, stream=True)
        ep_key = (self.base_url or "").rstrip("/")
        if ep_key in _STREAM_OPTIONS_UNSUPPORTED:
            return await self.client.chat.completions.create(**create_kw)
        try:
            return await self.client.chat.completions.create(
                stream_options={"include_usage": True}, **create_kw)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 400:
                raise
            msg = str(exc).lower()
            if "stream_options" in msg or "include_usage" in msg:
                _STREAM_OPTIONS_UNSUPPORTED.add(ep_key)
                logger.warning(
                    "Endpoint %s rejected stream_options.include_usage; "
                    "disabling it for this process (streamed usage will be 0): %s",
                    ep_key or "api.openai.com", exc)
            # A 400 while the probe field was present may be an opaque
            # rejection of the field itself — retry once without it before the
            # retry policy (llm_retry) classifies the failure.
            return await self.client.chat.completions.create(**create_kw)

    async def generate_stream(self, messages, model="gpt-4o",
                              temperature=0.7, max_tokens=8192, **kw):
        # v1.6.0 Y2: prompt_cache_key on stream too (native OpenAI only)
        extra_body = _env_extra_body()
        task_type = kw.get('task_type', 'unknown')
        usage_box = kw.pop('usage_box', None)
        retry_attempts = kw.pop('retry_attempts', None)
        # Relay workaround: fold system into first user message (see generate)
        if _should_merge_system_for_model(model):
            messages = merge_system_into_user(messages)
        if _OPENAI_CACHE_ENABLED and not self.base_url:
            extra_body["prompt_cache_key"] = f"{task_type}:{model}"

        # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务不开 SSE —— 内部走一次非流式
        # 调用,把完整正文作为**单个 chunk** yield 出去。调用方仍是 ``async for``,
        # 依旧是可关闭的异步生成器;usage_box / usage_estimated 的口径由
        # ``generate`` 负责(真实用量优先,供应商不报且正文非空才标记估算)。
        if _should_force_nonstream(task_type):
            _log_forced_nonstream(task_type, "OpenAIProvider.generate_stream")
            forward_kw = {
                key: value
                for key, value in kw.items()
                if key not in ("task_type", "stream", "usage_box", "retry_attempts")
            }
            result = await self.generate(
                messages=messages, model=model,
                temperature=temperature, max_tokens=max_tokens,
                task_type=task_type, usage_box=usage_box,
                retry_attempts=retry_attempts, stream=False, **forward_kw,
            )
            from app.services.provider_response_guard import require_normal_finish

            require_normal_finish(result.finish_reason)
            if result.text:
                yield result.text
            return

        # Same retry envelope as the streaming branch of `generate` (attempts=4
        # unless overridden): failures before the first chunk retry in place on
        # this endpoint with backoff; after the first chunk errors propagate —
        # the consumer's accumulated buffer cannot be rewound.
        from app.services.llm_retry import stream_with_retry

        async def _open():
            await _wait_openai_compat_turn(self.base_url, model, task_type)
            stream = await self._create_chat_stream(dict(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens,
                extra_body=extra_body or None,
            ))

            return guard_chat_stream(stream)

        attempts = 4
        if retry_attempts is not None and int(retry_attempts) > 0:
            attempts = int(retry_attempts)
        saw_content = False
        # a1001961: keep the text around so a usage-less stream can be
        # estimated by character count after the last chunk arrives.
        output_chunks: list[str] = []
        finish_reason = None
        usage = TokenUsage()
        cached_tokens = 0
        async with aclosing(stream_with_retry(
            _open, label=f"openai_stream[{task_type}:{model}]",
            attempts=attempts,
        )) as response_stream:
            async for chunk in response_stream:
                if chunk.choices and getattr(chunk.choices[0], 'finish_reason', None) is not None:
                    finish_reason = chunk.choices[0].finish_reason
                _delta = getattr(chunk.choices[0], "delta", None) if chunk.choices else None
                _content = getattr(_delta, "content", None)
                if _content:
                    saw_content = True
                    output_chunks.append(_content)
                    yield _content
                if hasattr(chunk, 'usage') and chunk.usage:
                    u = chunk.usage
                    usage = TokenUsage(
                        getattr(u, 'prompt_tokens', 0) or 0,
                        getattr(u, 'completion_tokens', 0) or 0,
                        getattr(u, 'total_tokens', 0) or 0)
                    ptd = getattr(u, 'prompt_tokens_details', None)
                    if ptd is not None:
                        cached_tokens = getattr(ptd, 'cached_tokens', 0) or 0
        if usage_box is not None:
            usage_box['input_tokens'] = usage.input_tokens
            usage_box['output_tokens'] = usage.output_tokens
            # a1001960: cache hits were metrics-only until now. Only present
            # when non-zero so the box keeps its old shape otherwise.
            if cached_tokens:
                usage_box['cached_input_tokens'] = max(int(cached_tokens), 0)
            # a1001961: relay ignored stream_options.include_usage ⇒ usage is
            # 0/0 while a full response was streamed. Estimate (flagged) instead
            # of logging 0; real numbers above are never overwritten.
            _resolve_usage_box(usage_box, usage, _prompt_texts(messages),
                               "".join(output_chunks))
        if _OPENAI_CACHE_ENABLED and usage.input_tokens:
            uncached = max(usage.input_tokens - cached_tokens, 0)
            _record_cache_tokens(task_type, self.name, model,
                                 read=cached_tokens, uncached=uncached)
        from app.services.provider_response_guard import require_normal_finish
        require_normal_finish(finish_reason)
        if not saw_content:
            await _cooldown_openai_compat_empty(self.base_url, model, task_type)


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
        from app.services.llm_retry import call_with_retry

        async def _one_attempt() -> list[float]:
            resp = await self.client.embeddings.create(
                model=self.model, input=text[:8000])
            return resp.data[0].embedding

        return await call_with_retry(
            _one_attempt, label=f"openai_embed[{self.model}]"
        )

    async def embed_batch(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        """Strict, untruncated batch API for source-bound retrieval indexes."""
        from app.services.embedding_space import ordered_embeddings
        from app.services.llm_retry import call_with_retry
        async def request():
            response = await self.client.embeddings.create(model=self.model, input=texts)
            return ordered_embeddings(response.data, len(texts))
        return await call_with_retry(request, label=f'openai_embed_batch[{self.model}]')


# NVIDIA 要求 ``modality`` 与 ``input`` 等长:``input`` 传数组时只发一个
# ``["text"]`` 会被拒(HTTP 400 "Modality must be a list of the same length as
# input")。批量向量化(认知索引按 32 条一批 passages)因此全军覆没,而单条调用
# 看不出问题——线上表现为"语义召回不可用"。这里按输入条数展开;调用方若显式给出
# 逐条 modality,则原样保留。
def nvidia_embedding_payload(*, model: str, inputs: list[str], input_type: str,
                             modality: list[str] | None, encoding_format: str,
                             truncate: str) -> dict:
    base = [m.strip() for m in (modality or [])
            if isinstance(m, str) and m.strip()] or ["text"]
    per_input = base if len(base) == len(inputs) else [base[0]] * len(inputs)
    return {"input": inputs, "model": model, "modality": per_input,
            "input_type": input_type, "encoding_format": encoding_format,
            "truncate": truncate}


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
        api_key: str | list[str] = "",
        base_url: str = "",
        model: str = "nvidia/llama-nemotron-embed-vl-1b-v2",
        modality: list[str] | None = None,
        encoding_format: str = "float",
        truncate: str = "NONE",
        timeout: float | None = None,
        api_keys: list[str] | None = None,
    ) -> None:
        # PR-NVIDIA-MULTIKEY: accept either a single key (str) or a list of
        # keys for round-robin concurrent dispatch. Empty/None entries pruned.
        keys: list[str] = []
        if api_keys:
            keys.extend([k for k in api_keys if k])
        if isinstance(api_key, list):
            keys.extend([k for k in api_key if k])
        elif isinstance(api_key, str) and api_key:
            keys.append(api_key)
        # Deduplicate while preserving order
        seen: set[str] = set()
        self.api_keys: list[str] = []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                self.api_keys.append(k)
        # Backwards-compat single-key attribute (used by callers that read it).
        self.api_key = self.api_keys[0] if self.api_keys else ""
        self._key_idx = 0
        import asyncio as _asyncio
        self._key_lock = _asyncio.Lock()
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.modality = modality or ["text"]
        self.encoding_format = encoding_format
        self.truncate = truncate
        # 嵌入服务偶发 ReadTimeout(实测 NVIDIA 端点 30s 不够,还会吐 CUDA 错误)。
        # 默认仍是 30s,可用 NVIDIA_EMBEDDING_TIMEOUT_SECONDS 调大;不设就没有行为变化。
        self.timeout = (float(timeout) if timeout is not None else
                        float(_os_cache.getenv("NVIDIA_EMBEDDING_TIMEOUT_SECONDS", "30")))

    async def _next_key(self) -> str:
        """Round-robin pick the next API key (thread-safe via asyncio lock)."""
        if not self.api_keys:
            return ""
        if len(self.api_keys) == 1:
            return self.api_keys[0]
        async with self._key_lock:
            k = self.api_keys[self._key_idx % len(self.api_keys)]
            self._key_idx += 1
            return k

    async def _post(
        self, inputs: list[str], input_type: str
    ) -> dict:
        import httpx

        if not inputs:
            return {"data": []}
        # Soft-clip each input to 8k chars to avoid accidental massive
        # payloads; NVIDIA will also truncate per ``truncate`` policy.
        clipped = [s[:8000] for s in inputs]
        payload = nvidia_embedding_payload(
            model=self.model, inputs=clipped, input_type=input_type,
            modality=list(self.modality or []),
            encoding_format=self.encoding_format, truncate=self.truncate)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self.base_url}/embeddings"

        # v1.14: wrap a single NVIDIA POST with bounded retries for transient
        # transport blips. 5xx and 429 raise as ``RuntimeError"... HTTP 5xx/429"``
        # which the shared llm_retry helper recognizes; 4xx (validation/auth)
        # raises immediately and short-circuits the retry loop.
        from app.services.llm_retry import call_with_retry

        async def _one_attempt() -> dict:
            # PR-NVIDIA-MULTIKEY: pick next key per request (per-attempt:
            # if key A is rate-limited, the next attempt round-robins to B).
            active_key = (
                await self._next_key() if hasattr(self, "_next_key") else self.api_key
            )
            attempt_headers = dict(headers)
            if active_key:
                attempt_headers["Authorization"] = f"Bearer {active_key}"
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                resp = await http.post(url, json=payload, headers=attempt_headers)
            if resp.status_code >= 400:
                # Surface the server's error body to the caller —
                # test_endpoint re-wraps this in TestResult.message for UI.
                snippet = resp.text[:400]
                raise RuntimeError(
                    f"NVIDIA embeddings HTTP {resp.status_code}: {snippet}"
                )
            return resp.json()

        return await call_with_retry(
            _one_attempt, label=f"nvidia_embed[{self.model}]"
        )

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

    async def embed_batch(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        """Strict indexed response; the caller already bounds complete slices."""
        from app.services.embedding_space import ordered_embeddings, MAX_EMBEDDING_CHARS
        if any(len(t) > MAX_EMBEDDING_CHARS for t in texts):
            raise ValueError('embedding_input_invalid')
        response = await self._post(texts, input_type=input_type)
        return ordered_embeddings(response.get('data'), len(texts))


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
        self._managed = False
        self._loaded_at: float | None = None
        self._has_db_endpoints = False
        self._provider_configs: dict[str, tuple] = {}
        self._embedding_config: tuple | None = None
        self._prompt_snapshots: tuple = ()
        self._endpoint_defaults: dict[str, str] = {}  # provider_key -> default_model
        # v1.4 — endpoint tier registry (provider_key -> tier)
        self._endpoint_tiers: dict[str, str] = {}
        # v1.4 — endpoint display names (provider_key -> name)
        self._endpoint_names: dict[str, str] = {}

    def register_provider(self, key: str, provider: BaseProvider) -> None:
        self.providers[key] = provider

    def set_embedding(self, provider: EmbeddingProvider) -> None:
        self.embedding_provider = provider

    async def _current(self) -> ModelRouter:
        """Refresh even when a service retained an old singleton reference."""
        return await get_model_router_async() if getattr(self, "_managed", False) else self

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
            from app.services.prompt_cache import _snapshot_from_asset

            async with async_session_factory() as db:
                # Endpoints and prompt bindings must describe one committed
                # snapshot, even if an administrator edits between the SELECTs.
                await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                result = await db.execute(select(LLMEndpoint))
                configured = result.scalars().all()
                self._has_db_endpoints = bool(configured)
                endpoints = [ep for ep in configured if ep.enabled == 1]

                from app.utils.crypto import decrypt_api_key

                for ep in endpoints:
                    key = str(ep.id)
                    self._endpoint_defaults[key] = ep.default_model or ""
                    # v1.4 — record tier + name per endpoint for tier-aware routing
                    self._endpoint_tiers[key] = getattr(ep, "tier", "standard") or "standard"
                    self._endpoint_names[key] = getattr(ep, "name", "") or ""
                    try:
                        api_key = decrypt_api_key(ep.api_key or "")
                    except Exception as exc:  # ApiKeyDecryptError
                        logger.error(
                            "endpoint %s api_key cannot be decrypted (%s); endpoint skipped",
                            getattr(ep, "name", ep.id), type(exc).__name__,
                        )
                        continue
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
                    self._provider_configs[key] = (ep.provider_type, api_key, ep.base_url or "")
                    if ep.provider_type == "anthropic":
                        self.register_provider(key, AnthropicProvider(api_key=api_key))
                    else:
                        self.register_provider(key, OpenAIProvider(
                            api_key=api_key, base_url=ep.base_url or ""))

                # Load task routing from active PromptAssets (v0.5)
                result = await db.execute(
                    select(PromptAsset).where(PromptAsset.is_active == 1)
                    .order_by(PromptAsset.task_type, PromptAsset.version.desc(), PromptAsset.id))
                active = {}
                for row in result.scalars().all():
                    active.setdefault(row.task_type, row)
                prompts_rows = list(active.values())
                self._prompt_snapshots = tuple(_snapshot_from_asset(p) for p in prompts_rows)

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
                        # 密钥解不开时**只禁用 embedding**,不能让整次配置加载失败。
                        # 实测:llm_endpoints 里残留的历史行(用旧 SECRET_KEY 加密)会在这里
                        # 抛 ApiKeyDecryptError,被外层 except 吞成"模型配置整体加载失败",
                        # 于是连正文生成一起挂掉 —— 一条坏的历史行不该有这种杀伤力。
                        try:
                            emb_api_key = decrypt_api_key(emb_ep.api_key or "")
                            _emb_key2_raw = getattr(emb_ep, "api_key_2", "") or ""
                            _emb_key2 = decrypt_api_key(_emb_key2_raw) if _emb_key2_raw else ""
                        except Exception as exc:  # noqa: BLE001 - ApiKeyDecryptError 等
                            logger.warning(
                                "embedding endpoint %s api_key cannot be decrypted (%s); "
                                "embedding disabled for this load",
                                emb_ep.id, type(exc).__name__,
                            )
                            emb_ep = None
                    if emb_ep:
                        emb_model = emb_prompt.model_name or emb_ep.default_model
                        if emb_ep.provider_type == "nvidia":
                            # v1.4.2 fix: NVIDIA embeddings require a different
                            # request schema (input=list, input_type, modality,
                            # encoding_format, truncate) and are not speakable
                            # via the OpenAI SDK.
                            # PR-NVIDIA-MULTIKEY: also forward api_key_2 if set
                            # so the provider can round-robin across two keys.
                            _key_list = [k for k in (emb_api_key, _emb_key2) if k]
                            self._embedding_config = (emb_ep.provider_type, tuple(_key_list),
                                                      emb_ep.base_url or "", emb_model)
                            self.embedding_provider = NvidiaEmbeddingProvider(
                                api_keys=_key_list,
                                base_url=emb_ep.base_url or "",
                                model=emb_model,
                            )
                        else:
                            self._embedding_config = (emb_ep.provider_type, emb_api_key,
                                                      emb_ep.base_url or "", emb_model)
                            self.embedding_provider = EmbeddingProvider(
                                api_key=emb_api_key,
                                base_url=emb_ep.base_url or "",
                                model=emb_model,
                            )

                self._db_loaded = True
                logger.info("Loaded %d endpoints, %d task routes from DB",
                            len(endpoints), len(self.task_routing))
        except Exception as e:
            # Do not publish a partial snapshot or keep using revoked keys.
            logger.warning("Failed to load model config from DB (%s)", type(e).__name__)
            raise RuntimeError("Model configuration could not be loaded") from e

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

    def _pick_endpoints_by_tier(self, tier: str) -> list[str]:
        """v1.5 — return ALL registered provider_keys whose endpoint tier matches.

        Used by tier-aware fallback to enumerate every candidate within a tier
        (e.g. two distinct standard endpoints), not just the first match.
        Excludes the embedding tier (embeddings are not chat-capable).
        """
        if not tier or tier == "embedding":
            return []
        return [k for k, t in self._endpoint_tiers.items()
                if t == tier and k in self.providers]

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
        self = await self._current()
        route = self._get_route(task_type)
        provider = self._get_provider(route.provider_key)
        model = self._resolve_model(route)
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens
        # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务显式 stream=False ——
        # ``OpenAIProvider.generate`` 默认是流式(```stream``` 缺省 True),不显式
        # 传就会继续开 SSE,进而撞上"网关提前关闭流"。
        _apply_nonstream_override(kw, task_type, "ModelRouter.generate")
        if _log_meta is None:
            with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                result = await provider.generate(
                    messages=messages, model=model,
                    temperature=eff_temp, max_tokens=eff_max, task_type=task_type, **kw)
                _mbox["input_tokens"] = result.usage.input_tokens
                _mbox["output_tokens"] = result.usage.output_tokens
            self._track(result.usage)
            return result
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", route.provider_key)
        async with async_session_factory() as db:
            try:
                async with log_llm_call(
                    db=db, task_type=task_type, model=model,
                    endpoint_id=endpoint_id, messages=messages,
                    prompt_id=meta.pop("prompt_id", None),
                    project_id=meta.pop("project_id", None),
                    chapter_id=meta.pop("chapter_id", None),
                    rag_hits=meta.pop("rag_hits", None),
                ) as ctx:
                    # a1001960: the provider mirrors what it billed (incl.
                    # prompt-cache hits) into _ubox; cached_input_tokens is a
                    # subset of input_tokens that was previously metrics-only.
                    _ubox: dict = {}
                    with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                        result = await provider.generate(
                            messages=messages, model=model,
                            temperature=eff_temp, max_tokens=eff_max, task_type=task_type,
                            usage_box=_ubox, **kw)
                        _mbox["input_tokens"] = result.usage.input_tokens
                        _mbox["output_tokens"] = result.usage.output_tokens
                    ctx.add_chunk(result.text)
                    # a1001961: box values win when present — the provider may
                    # have put an *estimate* there because it reported no usage
                    # at all. result.usage stays the untouched fallback (and is
                    # never estimated: truncation checks read it).
                    ctx.set_usage(
                        int(_ubox.get("input_tokens") or 0) or int(result.usage.input_tokens or 0),
                        int(_ubox.get("output_tokens") or 0) or int(result.usage.output_tokens or 0),
                        _ubox.get("cached_input_tokens", 0),
                        usage_estimated=bool(_ubox.get("usage_estimated")),
                    )
            finally:
                await _commit_llm_call_log(
                    db, operation="generate", task_type=task_type, model=model
                )
        self._track(result.usage)
        return result

    async def generate_stream(self, task_type: str, messages: list[dict],
                              temperature: float | None = None,
                              max_tokens: int | None = None,
                              _log_meta: dict | None = None, **kw) -> AsyncIterator[str]:
        self = await self._current()
        route = self._get_route(task_type)
        provider = self._get_provider(route.provider_key)
        model = self._resolve_model(route)
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens
        if _log_meta is None:
            _ubox: dict = {}
            with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                async with aclosing(provider.generate_stream(messages=messages, model=model, temperature=eff_temp, max_tokens=eff_max, task_type=task_type, usage_box=_ubox, **kw)) as owned_stream:
                    async for chunk in owned_stream:
                        yield chunk
                _mbox["input_tokens"] = _ubox.get("input_tokens", 0)
                _mbox["output_tokens"] = _ubox.get("output_tokens", 0)
            return
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", route.provider_key)
        async with async_session_factory() as db:
            try:
                async with log_llm_call(
                    db=db, task_type=task_type, model=model,
                    endpoint_id=endpoint_id, messages=messages,
                    prompt_id=meta.pop("prompt_id", None),
                    project_id=meta.pop("project_id", None),
                    chapter_id=meta.pop("chapter_id", None),
                    rag_hits=meta.pop("rag_hits", None),
                ) as ctx:
                    _ubox = {}
                    with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                        async with aclosing(provider.generate_stream(messages=messages, model=model, temperature=eff_temp, max_tokens=eff_max, task_type=task_type, usage_box=_ubox, **kw)) as owned_stream:
                            async for chunk in owned_stream:
                                ctx.add_chunk(chunk)
                                yield chunk
                        _mbox["input_tokens"] = _ubox.get("input_tokens", 0)
                        _mbox["output_tokens"] = _ubox.get("output_tokens", 0)
                    # a1001961: the box is the only usage source on this path,
                    # so its estimate flag must travel with it — an estimated
                    # 1200 must never be logged as if the provider billed it.
                    ctx.set_usage(_ubox.get("input_tokens", 0),
                                  _ubox.get("output_tokens", 0),
                                  _ubox.get("cached_input_tokens", 0),
                                  usage_estimated=bool(_ubox.get("usage_estimated")))
            finally:
                await _commit_llm_call_log(
                    db, operation="generate_stream", task_type=task_type, model=model
                )

    async def generate_by_route(self, route, messages: list[dict],
                                temperature: float | None = None,
                                max_tokens: int | None = None,
                                _log_meta: dict | None = None,
                                task_type: str = "by_route",
                                **kw) -> GenerationResult:
        """Generate using an explicit RouteSpec (v0.5 path)."""
        self = await self._current()
        ep_key = str(route.endpoint_id)
        provider = self._get_provider(ep_key)
        model = route.model or self._endpoint_defaults.get(ep_key, "")
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens
        _apply_nonstream_override(kw, task_type, "ModelRouter.generate_by_route")
        if _log_meta is None:
            with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                result = await provider.generate(
                    messages=messages, model=model,
                    temperature=eff_temp, max_tokens=eff_max, task_type=task_type, **kw)
                _mbox["input_tokens"] = result.usage.input_tokens
                _mbox["output_tokens"] = result.usage.output_tokens
            self._track(result.usage)
            return result
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", ep_key)
        task_type = meta.pop("task_type", "by_route")
        # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务显式 stream=False。
        _apply_nonstream_override(kw, task_type, "ModelRouter.generate_by_route")
        async with async_session_factory() as db:
            try:
                async with log_llm_call(
                    db=db, task_type=task_type, model=model,
                    endpoint_id=endpoint_id, messages=messages,
                    prompt_id=meta.pop("prompt_id", None),
                    project_id=meta.pop("project_id", None),
                    chapter_id=meta.pop("chapter_id", None),
                    rag_hits=meta.pop("rag_hits", None),
                ) as ctx:
                    _ubox: dict = {}
                    with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                        result = await provider.generate(
                            messages=messages, model=model,
                            temperature=eff_temp, max_tokens=eff_max, task_type=task_type,
                            usage_box=_ubox, **kw)
                        _mbox["input_tokens"] = result.usage.input_tokens
                        _mbox["output_tokens"] = result.usage.output_tokens
                    ctx.add_chunk(result.text)
                    # a1001961: idempotent when the provider already mirrored
                    # real numbers; estimates only when nothing was reported.
                    _resolve_usage_box(_ubox, result.usage, _prompt_texts(messages),
                                       result.text)
                    ctx.set_usage(
                        int(_ubox.get("input_tokens") or 0) or int(result.usage.input_tokens or 0),
                        int(_ubox.get("output_tokens") or 0) or int(result.usage.output_tokens or 0),
                        _ubox.get("cached_input_tokens", 0),
                        usage_estimated=bool(_ubox.get("usage_estimated")),
                    )
            finally:
                await _commit_llm_call_log(
                    db, operation="generate_by_route", task_type=task_type, model=model
                )
        self._track(result.usage)
        return result

    async def stream_by_route(self, route, messages: list[dict],
                              temperature: float | None = None,
                              max_tokens: int | None = None,
                              _log_meta: dict | None = None,
                              task_type: str = "by_route_stream",
                              **kw) -> AsyncIterator[str]:
        """Stream using an explicit RouteSpec (v0.5 path)."""
        self = await self._current()
        ep_key = str(route.endpoint_id)
        provider = self._get_provider(ep_key)
        model = route.model or self._endpoint_defaults.get(ep_key, "")
        eff_temp = temperature if temperature is not None else route.temperature
        eff_max = max_tokens if max_tokens is not None else route.max_tokens

        if _log_meta is None and _should_force_nonstream(task_type):
            # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务不开 SSE,内部走一次
            # 非流式调用,把完整正文作为单个 chunk yield 出去(仍是异步生成器)。
            _log_forced_nonstream(task_type, "ModelRouter.stream_by_route")
            gen_kw = dict(kw)
            gen_kw["stream"] = False
            result = await provider.generate(
                messages=messages, model=model, temperature=eff_temp,
                max_tokens=eff_max, task_type=task_type, **gen_kw)
            if result.text:
                yield result.text
            return
        if _log_meta is None:
            async with aclosing(provider.generate_stream(messages=messages, model=model, temperature=eff_temp, max_tokens=eff_max, task_type=task_type, **kw)) as owned_stream:
                async for chunk in owned_stream:
                    yield chunk
            return
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta)
        endpoint_id = meta.pop("endpoint_id", ep_key)
        task_type = meta.pop("task_type", task_type)
        async with async_session_factory() as db:
            try:
                async with log_llm_call(
                    db=db, task_type=task_type, model=model,
                    endpoint_id=endpoint_id, messages=messages,
                    prompt_id=meta.pop("prompt_id", None),
                    project_id=meta.pop("project_id", None),
                    chapter_id=meta.pop("chapter_id", None),
                    rag_hits=meta.pop("rag_hits", None),
                ) as ctx:
                    # 2026-11-17: 这条路径此前既不传 usage_box 也不回填用量,日志恒为 0。
                    # provider 若支持,会在流结束前把真实用量写进 box(Anthropic 流式也会);
                    # 不支持时 box 保持空,ctx.set_usage 仍按 0 记录(与改动前一致)。
                    usage_box: dict = {}
                    stream_kw = dict(kw)
                    stream_kw.setdefault("usage_box", usage_box)
                    if _should_force_nonstream(task_type):
                        # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务改走非流式,
                        # 把完整正文作为单个 chunk 交出(调用方仍是 async for,
                        # 仍可 aclose;usage / committed 语义与流式路径一致)。
                        _log_forced_nonstream(task_type, "ModelRouter.stream_by_route")
                        # 复用上面那份 kwargs(含"调用方自带 usage_box 不被覆盖"的语义)。
                        stream_kw["stream"] = False
                        result = await provider.generate(
                            messages=messages, model=model, temperature=eff_temp,
                            max_tokens=eff_max, task_type=task_type, **stream_kw)
                        ctx.add_chunk(result.text)
                        _resolve_usage_box(usage_box, result.usage,
                                           _prompt_texts(messages), result.text)
                        ctx.set_usage(
                            int(usage_box.get("input_tokens", 0) or 0),
                            int(usage_box.get("output_tokens", 0) or 0),
                            int(usage_box.get("cached_input_tokens", 0) or 0),
                            usage_box.get("usage_estimated"),
                        )
                        if result.text:
                            yield result.text
                        return
                    async with aclosing(provider.generate_stream(messages=messages, model=model, temperature=eff_temp, max_tokens=eff_max, task_type=task_type, **stream_kw)) as owned_stream:
                        async for chunk in owned_stream:
                            ctx.add_chunk(chunk)
                            yield chunk
                    if usage_box:
                        ctx.set_usage(
                            int(usage_box.get("input_tokens", 0) or 0),
                            int(usage_box.get("output_tokens", 0) or 0),
                            int(usage_box.get("cached_input_tokens", 0) or 0),
                            usage_box.get("usage_estimated"),
                        )
            finally:
                await _commit_llm_call_log(
                    db, operation="stream_by_route", task_type=task_type, model=model
                )

    async def generate_with_fallback(self, task_type: str, messages: list[dict],
                                     **kw) -> GenerationResult:
        self = await self._current()
        route = self._get_route(task_type)
        model = self._resolve_model(route)
        to_try = [route.provider_key] + [k for k in self.providers if k != route.provider_key]
        last_err = None
        for key in to_try:
            if key not in self.providers:
                continue
            # 备用端点必须用"它自己的"默认模型名。主路由的 model 属于另一个供应商,
            # 直接透传会 404,或被中转静默映射到别的模型却仍算成功。
            # (2026-11-17 审计 F6)
            endpoint_model = (
                model if key == route.provider_key
                else (self._endpoint_defaults.get(key) or model)
            )
            try:
                prov = self.providers[key]
                result = await prov.generate(
                    messages=messages, model=endpoint_model,
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
        self = await self._current()
        if self.embedding_provider:
            return await self.embedding_provider.embed(text)
        for prov in self.providers.values():
            if isinstance(prov, OpenAIProvider):
                try:
                    ep = EmbeddingProvider(api_key=prov.api_key, base_url=prov.base_url)
                    return await ep.embed(text)
                except Exception:
                    continue
        # 2026-11-17 审计 F3:原先返回 1536 维零向量。零向量在余弦/点积下与
        # 任何查询"等距",检索顺序等于随机;且维度与当前 embedding 模型
        # (nemotron-3-embed-1b = 2048)不匹配,写 Qdrant 也会直接失败。
        # 无可用嵌入必须显式失败,让上层走既有的"降级/阻断"分支。
        raise EmbeddingUnavailableError("no_embedding_provider")

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
            # v1.5 B1': enumerate ALL endpoints in this tier, not just first match.
            for ep_key in self._pick_endpoints_by_tier(t):
                if ep_key in seen_eps:
                    continue
                model = self._endpoint_defaults.get(ep_key, "")
                attempts.append((t, ep_key, model))
                seen_eps.add(ep_key)
        # v1.5 B1' final safety net: any remaining chat-capable endpoint not yet
        # tried, regardless of tier (embedding always excluded). Ensures upstream
        # transient stream errors on a single endpoint never abort the whole call
        # if any other configured endpoint can serve the same task.
        for ep_key, t in self._endpoint_tiers.items():
            if t == "embedding" or ep_key in seen_eps or ep_key not in self.providers:
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
        self = await self._current()
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
        # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务显式 stream=False(见
        # ``_apply_nonstream_override``;不传的话 provider 默认仍会走流式)。
        _apply_nonstream_override(
            kw, task_type, "ModelRouter.generate_with_tier_fallback"
        )
        from app.db.session import async_session_factory
        from app.services.llm_call_logger import log_llm_call
        meta = dict(_log_meta or {})
        last_err: Exception | None = None
        fallback_reason: str | None = None
        for idx, (tier, ep_key, model) in enumerate(attempts):
            provider = self.providers[ep_key]
            try:
                if _log_meta is None:
                    with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                        result = await provider.generate(
                            messages=messages, model=model,
                            temperature=eff_temp, max_tokens=eff_max, task_type=task_type, **kw)
                        _mbox["input_tokens"] = result.usage.input_tokens
                        _mbox["output_tokens"] = result.usage.output_tokens
                    self._track(result.usage)
                    return result
                async with async_session_factory() as db:
                    try:
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
                            # a1001960: same usage mirror as generate(); the
                            # retry/fallback decisions below are untouched.
                            _ubox: dict = {}
                            with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                                result = await provider.generate(
                                    messages=messages, model=model,
                                    temperature=eff_temp, max_tokens=eff_max,
                                    task_type=task_type, usage_box=_ubox, **kw)
                                _mbox["input_tokens"] = result.usage.input_tokens
                                _mbox["output_tokens"] = result.usage.output_tokens
                            ctx.add_chunk(result.text)
                            # a1001961: real usage wins; estimate only when the
                            # provider reported none at all (flagged).
                            _resolve_usage_box(_ubox, result.usage,
                                               _prompt_texts(messages), result.text)
                            ctx.set_usage(
                                int(_ubox.get("input_tokens") or 0) or int(result.usage.input_tokens or 0),
                                int(_ubox.get("output_tokens") or 0) or int(result.usage.output_tokens or 0),
                                _ubox.get("cached_input_tokens", 0),
                                usage_estimated=bool(_ubox.get("usage_estimated")),
                            )
                    finally:
                        await _commit_llm_call_log(
                            db,
                            operation="generate_with_tier_fallback",
                            task_type=task_type,
                            model=model,
                        )
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
        self = await self._current()
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
                    _ubox: dict = {}
                    if _should_force_nonstream(task_type):
                        # 命中 ``LLM_NONSTREAM_TASKS``:非流式取全文,yield 一次。
                        _log_forced_nonstream(
                            task_type, "ModelRouter.stream_with_tier_fallback")
                        gen_kw = dict(kw)
                        gen_kw.setdefault("usage_box", _ubox)
                        gen_kw["stream"] = False
                        with time_llm_call(task_type, provider.__class__.__name__,
                                           model) as _mbox:
                            result = await provider.generate(
                                messages=messages, model=model,
                                temperature=eff_temp, max_tokens=eff_max,
                                task_type=task_type, **gen_kw)
                            _mbox["input_tokens"] = result.usage.input_tokens
                            _mbox["output_tokens"] = result.usage.output_tokens
                        yielded_any = True
                        if result.text:
                            yield result.text
                        return
                    with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                        async with aclosing(provider.generate_stream(messages=messages, model=model, temperature=eff_temp, max_tokens=eff_max, task_type=task_type, usage_box=_ubox, **kw)) as owned_stream:
                            async for chunk in owned_stream:
                                yielded_any = True
                                yield chunk
                        _mbox["input_tokens"] = _ubox.get("input_tokens", 0)
                        _mbox["output_tokens"] = _ubox.get("output_tokens", 0)
                    return
                async with async_session_factory() as db:
                    try:
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
                            _ubox = {}
                            if _should_force_nonstream(task_type):
                                # 2026-11-17 ``LLM_NONSTREAM_TASKS``:命中任务改走
                                # 非流式(仍在同一次尝试/回退语义内:失败且尚未
                                # yield 才会回退到下一个端点)。
                                _log_forced_nonstream(
                                    task_type,
                                    "ModelRouter.stream_with_tier_fallback")
                                gen_kw = dict(kw)
                                gen_kw.setdefault("usage_box", _ubox)
                                gen_kw["stream"] = False
                                with time_llm_call(task_type,
                                                   provider.__class__.__name__,
                                                   model) as _mbox:
                                    result = await provider.generate(
                                        messages=messages, model=model,
                                        temperature=eff_temp, max_tokens=eff_max,
                                        task_type=task_type, **gen_kw)
                                    _mbox["input_tokens"] = result.usage.input_tokens
                                    _mbox["output_tokens"] = result.usage.output_tokens
                                ctx.add_chunk(result.text)
                                _resolve_usage_box(_ubox, result.usage,
                                                   _prompt_texts(messages),
                                                   result.text)
                                ctx.set_usage(_ubox.get("input_tokens", 0),
                                              _ubox.get("output_tokens", 0),
                                              _ubox.get("cached_input_tokens", 0),
                                              usage_estimated=bool(
                                                  _ubox.get("usage_estimated")))
                                yielded_any = True
                                if result.text:
                                    yield result.text
                                return
                            with time_llm_call(task_type, provider.__class__.__name__, model) as _mbox:
                                async with aclosing(provider.generate_stream(messages=messages, model=model, temperature=eff_temp, max_tokens=eff_max, task_type=task_type, usage_box=_ubox, **kw)) as owned_stream:
                                    async for chunk in owned_stream:
                                        yielded_any = True
                                        ctx.add_chunk(chunk)
                                        yield chunk
                                _resolve_usage_box(_ubox, None,
                                                   _prompt_texts(messages),
                                                   ctx.response_text)
                                _mbox["input_tokens"] = _ubox.get("input_tokens", 0)
                                _mbox["output_tokens"] = _ubox.get("output_tokens", 0)
                            ctx.set_usage(_ubox.get("input_tokens", 0),
                                          _ubox.get("output_tokens", 0),
                                          _ubox.get("cached_input_tokens", 0),
                                          usage_estimated=bool(_ubox.get("usage_estimated")))
                    finally:
                        await _commit_llm_call_log(
                            db,
                            operation="stream_with_tier_fallback",
                            task_type=task_type,
                            model=model,
                        )
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
    while True:
        if _router_fresh(_router):
            return _router
        async with _get_router_lock():
            if _router_fresh(_router):
                return _router
            previous = _router
            _router = ModelRouter()
            _router._managed = True
            candidate = _router
            read_started = time.monotonic()
            try:
                await candidate.load_from_db()
                # A committed configuration change may have reset the singleton
                # during the database read. Never publish that stale candidate
                # or dereference the now-reset global after an await.
                if _router is not candidate:
                    continue
                if not candidate._db_loaded:
                    raise RuntimeError("Model configuration could not be loaded")
                if not candidate.providers and not candidate._has_db_endpoints:
                    _load_from_env(candidate)
                if previous is not None:
                    # Reuse SDK clients only when their credentials and address
                    # are unchanged. Never close a client used by an active call.
                    for key, config in candidate._provider_configs.items():
                        if previous._provider_configs.get(key) == config:
                            candidate.providers[key] = previous.providers[key]
                    if (candidate._embedding_config is not None
                            and candidate._embedding_config == previous._embedding_config):
                        candidate.embedding_provider = previous.embedding_provider
                    candidate.total_usage = previous.total_usage
                from app.services import prompt_cache
                if previous is None or candidate._prompt_snapshots != previous._prompt_snapshots:
                    prompt_cache.invalidate()
                candidate._loaded_at = read_started
            except BaseException:
                if _router is candidate:
                    _router = None
                raise
            return candidate


def _router_fresh(router: ModelRouter | None) -> bool:
    return (router is not None and router._db_loaded
            and router._loaded_at is not None
            and time.monotonic() - router._loaded_at < ROUTER_TTL_SECONDS)


def get_model_router() -> ModelRouter:
    """Get the model router singleton.

    Async callers receive a managed reference; its model-call methods refresh
    before dispatch. Callers outside an event loop can load immediately.
    """
    global _router
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get_model_router_async())
    if _router is None:
        _router = ModelRouter()
        _router._managed = True
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
