"""FastAPI application entry-point."""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import chapters, generate, knowledge, outlines, projects
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
# Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(projects.router)
app.include_router(outlines.router)
app.include_router(chapters.router)
app.include_router(generate.router)
app.include_router(knowledge.router)


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
