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

from app.api import auth, chapters, filter_words, foreshadows, generate, knowledge, lora, model_config, outlines, pipeline, projects, prompts, quality, rewrite, settings, styles, vector_store, versions, volumes
from app.api.auth import verify_token
from app.db.neo4j import close_neo4j, init_neo4j
from app.db.qdrant import close_qdrant, init_qdrant
from app.db.redis import close_redis, init_redis
from app.db.session import engine

logger = logging.getLogger(__name__)


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
            from sqlalchemy.orm import Session as _S
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

    yield

    # Shutdown
    logger.info("Closing database connections...")
    await close_qdrant()
    await close_neo4j()
    await close_redis()
    await engine.dispose()
    logger.info("All connections closed")


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
    "/docs",
    "/openapi.json",
})


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify JWT token for all /api/* requests except public paths."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path

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
            verify_token(token)
        except Exception:
            return StarletteJSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        os.environ.get("CORS_ORIGIN", "http://localhost:3100"),
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(outlines.router)
app.include_router(chapters.router)
app.include_router(generate.router)
app.include_router(knowledge.router)
app.include_router(foreshadows.router)
app.include_router(settings.router)
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
