"""FastAPI application entry-point."""

import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse

from app.api import ask_user, auth, call_logs, chapters, filter_words, foreshadows, generate, knowledge, lora, model_config, neo4j_settings, outlines, pipeline, projects, prompts, quality, rewrite, settings, styles, vector_store, versions, volumes
from app.api import volume_outlines
from app.api.auth import verify_token
from app.api import cognition
from app.api import admin_entities, admin_usage
from app.api import export as export_api
from app.api import compass as compass_api
from app.api import arc as arc_api
from app.api import arc_progress
from app.api import arc_planning
from app.api import usage
from app.middlewares.quota import QuotaMiddleware
from app.middlewares.request_logging import RequestLoggingMiddleware
from app.db.neo4j import close_neo4j, init_neo4j
from app.db.qdrant import close_qdrant, init_qdrant
from app.db.redis import close_redis, init_redis
from app.observability.logging import setup_logging
from app.observability.sentry_init import init_sentry
from app.config import settings as app_settings

# Initialize structured JSON logging before any other side-effecting import.
setup_logging()

logger = logging.getLogger(__name__)

# Initialize Sentry as early as possible (no-op without SENTRY_DSN)
init_sentry(component="backend")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup / shutdown of external connections."""
    logger.info("Initializing database connections...")
    try:
        await init_redis()
        logger.info("Redis connected")
    except Exception:
        logger.warning("Redis connection failed -- continuing without Redis")

    try:
        await init_neo4j()
        logger.info("Neo4j connected")
    except Exception:
        logger.warning("Neo4j connection failed -- continuing without Neo4j")

    try:
        await init_qdrant()
        logger.info("Qdrant connected")
    except Exception:
        logger.warning("Qdrant connection failed -- continuing without Qdrant")

    # Auto-create tables if they don't exist (safety net for fresh DB)
    try:
        from app.db.session import engine
        from app.models import Base as _Base
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        logger.info("Database tables verified")
    except Exception:
        logger.warning("Could not verify database tables")

    # Migrate plaintext API keys to encrypted format
    try:
        from app.utils.crypto import encrypt_api_key, is_encrypted
        from app.models.project import LLMEndpoint
        from sqlalchemy import select as sel
        async with engine.connect() as conn:
            from sqlalchemy.ext.asyncio import AsyncSession as _AS
            async with _AS(bind=conn) as migration_db:
                result = await migration_db.execute(sel(LLMEndpoint))
                migrated = 0
                for ep in result.scalars().all():
                    if ep.api_key and not is_encrypted(ep.api_key):
                        ep.api_key = encrypt_api_key(ep.api_key)
                        migrated += 1
                if migrated:
                    await migration_db.commit()
                    logger.info("Migrated %d API keys to encrypted storage", migrated)
    except Exception as e:
        logger.warning("API key migration skipped: %s", e)

    # Pre-load model router from DB so all services can use it
    try:
        from app.services.model_router import get_model_router_async
        router = await get_model_router_async()
        logger.info("Model router loaded: %d providers, %d task routes",
                     len(router.providers), len(router.task_routing))
    except Exception:
        logger.warning("Could not pre-load model router")

    # v0.6: ensure Qdrant collections exist (style_profiles / beat_sheets / style_samples_redacted + legacy ones)
    try:
        from app.db.qdrant import get_qdrant
        from app.services.qdrant_store import QdrantStore
        async for _qc in get_qdrant():
            await QdrantStore(_qc).ensure_collections()
            break
        logger.info("Qdrant collections ensured")
    except Exception as e:
        logger.warning("Could not ensure Qdrant collections: %s", e)

    # v0.6: seed built-in prompts incrementally (adds missing task_types without overwriting user edits)
    # 2026-11-17: 之后接一次**内置提示升级**。`seed_builtins()` 只补缺失的 task_type ——
    # 这意味着改 `app/services/prompts/*.py` 的默认内容,对已经存在该 task_type 的库永远不生效
    # (实测:评估库的 scene_planner 一直是 v1,缺少代码里新加的字段说明)。
    # `upgrade_builtins()` 只升级"库里仍与**已发布过的旧默认**一致(即运维从没改过)"的行,
    # 运维改过的行完全不动、只记为 needs_review;被顶替的旧行保留在表内,不删。
    try:
        from app.services.prompt_registry import PromptRegistry
        from app.db.session import async_session_factory
        async with async_session_factory() as seed_db:
            registry = PromptRegistry(seed_db)
            added = await registry.seed_builtins()
            upgraded = await registry.upgrade_builtins()
            changed = list(upgraded.get("upgraded") or [])
            if added or changed:
                await seed_db.commit()
                logger.info(
                    "Built-in prompts: seeded=%d upgraded=%s current=%d needs_review=%d",
                    added, changed, upgraded.get("current", 0),
                    len(upgraded.get("needs_review") or []))
            else:
                logger.info("Built-in prompts already up to date (revision=%s, needs_review=%d)",
                            upgraded.get("revision"),
                            len(upgraded.get("needs_review") or []))
    except Exception as e:
        logger.warning("Could not seed/upgrade built-in prompts: %s", e)
# 2026-11-17 审计:这里原本还有第二个 ``except Exception``,永远不可达,
# 掩盖了"seed 与 upgrade 是两条失败路径"的原始意图。

    # v1.5.0 C3: start the prompt-cache background flusher. The flusher
    # commits buffered ``track_result`` increments on a fresh session every
    # ``FLUSH_INTERVAL_SECONDS``, so the per-LLM-call UPDATE prompt_assets
    # never runs on the request session (eliminates the C2 deadlock class).
    try:
        from app.services import prompt_cache
        prompt_cache.start_flusher()
        # If we just seeded new built-ins, drop any (very small) negative
        # cache so the next read picks them up immediately.
        prompt_cache.invalidate()
        logger.info("Prompt cache flusher started")
    except Exception as e:
        logger.warning("Could not start prompt cache flusher: %s", e)

    # v0.8: seed writing-engine defaults (incremental, idempotent)
    try:
        from app.services.writing_engine_seed import seed_writing_engine
        from app.db.session import async_session_factory
        async with async_session_factory() as seed_db:
            added = await seed_writing_engine(seed_db)
            if any(v > 0 for v in added.values()):
                logger.info("Seeded writing-engine defaults: %s", added)
            else:
                logger.info("Writing-engine defaults already up to date")
    except Exception as e:
        logger.warning("Could not seed writing-engine defaults: %s", e)

    yield

    # Shutdown
    logger.info("Closing database connections...")
    # v1.5.0 C3: stop the flusher first so it can drain any pending counter
    # ticks before the engine is disposed.
    try:
        from app.services import prompt_cache
        await prompt_cache.stop_flusher()
    except Exception as e:
        logger.warning("Prompt cache flusher shutdown failed: %s", e)
    await close_qdrant()
    await close_neo4j()
    await close_redis()
    # 2026-11-17 审计:``engine`` 只在启动函数的 try 里 import。若 DB 模块导入
    # 失败,关机会在这里 UnboundLocalError(把真正的启动错误盖掉)。改为按需导入。
    try:
        from app.db.session import engine as _engine
        await _engine.dispose()
    except Exception as e:  # noqa: BLE001
        logger.warning("engine dispose failed: %s", e)
    logger.info("All connections closed")


def _app_env() -> str:
    try:
        from app.config import settings

        return (getattr(settings, "APP_ENV", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _disable_auth_allowed() -> bool:
    """``DISABLE_AUTH=1`` 只在非生产环境生效(2026-11-17)。"""
    return _app_env() not in {"production", "prod"}


app = FastAPI(
    title="AI Write Backend",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Authentication middleware
# ---------------------------------------------------------------------------
_PUBLIC_PATHS = frozenset({
    "/api/auth/login",
    "/api/health",
    "/api/version",
    "/metrics",
    "/docs",
    "/openapi.json",
})


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify JWT token for all /api/* requests except public paths."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Test/dev escape hatch: allow running without auth while keeping the
        # middleware in place for production-like environments.
        # 2026-11-17: production 下这个变量**不再生效** —— 它曾经一旦留在 env 里
        # 就等于全站匿名可写,而且没有任何启动告警。
        if os.getenv("DISABLE_AUTH", "0") == "1":
            if _disable_auth_allowed():
                return await call_next(request)
            logger.error("DISABLE_AUTH=1 ignored: APP_ENV=%s", _app_env())

        path = request.url.path

        # 2026-11-17: 预检不带 Authorization,必须放行(真实请求仍会被鉴权)。
        if request.method == "OPTIONS":
            return await call_next(request)

        # Skip auth for public paths and non-API routes
        if path in _PUBLIC_PATHS or not path.startswith("/api/"):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return StarletteJSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid authorization header"},
            )

        token = auth_header[7:]
        try:
            username = verify_token(token)
            request.state.auth_username = username
            # 2026-11-17: 让服务层的用量记账拿到真实用户(见 request_identity 模块)。
            from app.middlewares.request_identity import set_current_user

            set_current_user(username)
        except Exception:
            return StarletteJSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(AuthMiddleware)
app.add_middleware(QuotaMiddleware)
# RequestLoggingMiddleware is registered *last* via add_middleware so it ends
# up outermost (Starlette wraps middleware in reverse order). That way every
# request -- including 401s from AuthMiddleware -- gets one JSON access line.
app.add_middleware(RequestLoggingMiddleware)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(outlines.router)
app.include_router(volume_outlines.router)
app.include_router(chapters.router)
app.include_router(generate.router)
app.include_router(knowledge.router)
app.include_router(foreshadows.router)
app.include_router(cognition.router)
app.include_router(compass_api.router)
app.include_router(arc_api.router)
app.include_router(arc_progress.router)
app.include_router(arc_planning.router)
app.include_router(settings.router)
app.include_router(neo4j_settings.router)
app.include_router(versions.router)
app.include_router(rewrite.router)
app.include_router(lora.router)
app.include_router(volumes.router)
app.include_router(model_config.router)
app.include_router(quality.router)
app.include_router(filter_words.router)
app.include_router(styles.router)
app.include_router(prompts.router)
app.include_router(pipeline.router)
app.include_router(vector_store.router)
app.include_router(call_logs.router)
app.include_router(usage.router)
app.include_router(ask_user.router)
from app.api import llm_routing  # noqa: E402  v1.4 tier routing matrix
app.include_router(llm_routing.router)
from app.api import decompile  # noqa: E402
app.include_router(decompile.router)
from app.api import generation_runs  # noqa: E402
app.include_router(generation_runs.router)
from app.api import writing_engine  # noqa: E402
app.include_router(writing_engine.router)
from app.api import version  # noqa: E402
app.include_router(version.router)
from app.api import metrics as metrics_api  # noqa: E402
app.include_router(metrics_api.router)
from app.api import debug as debug_api  # noqa: E402
app.include_router(debug_api.router)
from app.api import variants as variants_api  # noqa: E402
app.include_router(variants_api.router)
from app.api import run_bus as run_bus_api  # noqa: E402
app.include_router(run_bus_api.router)
app.include_router(admin_usage.router)
app.include_router(admin_entities.router)
app.include_router(export_api.router)
from app.api import evaluate as evaluate_api  # noqa: E402  C2 Step D (v1.5.0)
app.include_router(evaluate_api.router)

from app.api import cascade as cascade_api  # noqa: E402  v1.7 X5: cascade_tasks UI surface
app.include_router(cascade_api.router)

# Prometheus HTTP duration middleware
# ---------------------------------------------------------------------------
import time as _time  # noqa: E402

from app.observability.metrics import (  # noqa: E402
    HTTP_REQUEST_DURATION,
    HTTP_REQUEST_TOTAL,
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Records request duration + count. Uses route.path_template to avoid
    cardinality blowup from path params."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        start = _time.monotonic()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            elapsed = _time.monotonic() - start
            route = request.scope.get("route")
            path_tpl = getattr(route, "path", None) or request.url.path
            # Collapse unmatched paths (e.g. 404) to avoid label explosion.
            if route is None and not path_tpl.startswith("/api/") and path_tpl not in ("/metrics", "/docs", "/openapi.json"):
                path_tpl = "__other__"
            method = request.method
            HTTP_REQUEST_DURATION.labels(method, path_tpl, status).observe(elapsed)
            HTTP_REQUEST_TOTAL.labels(method, path_tpl, status).inc()


app.add_middleware(PrometheusMiddleware)
# 2026-11-17:CORS 必须**最后**注册(Starlette 反向包装 → 最外层),否则
# AuthMiddleware 会先拒掉浏览器的 OPTIONS 预检,跨域请求在预检阶段就 401,
# 且 401/402 响应拿不到 Access-Control-Allow-* 头。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in app_settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from app.api import changelog  # noqa: E402
app.include_router(changelog.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["health"])
async def health_check() -> dict:
    """Basic liveness probe."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------
from app.services.entity_materialization_lock import MaterializationBusy


@app.exception_handler(MaterializationBusy)
async def materialization_busy_handler(request: Request, exc: MaterializationBusy) -> JSONResponse:
    return JSONResponse(status_code=503, headers={"Retry-After": "2"},
                        content={"detail": "该作品的资料正在同步，请稍后重试。"})


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc)},
    )


@app.exception_handler(PermissionError)
async def permission_error_handler(
    request: Request, exc: PermissionError
) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc)},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
