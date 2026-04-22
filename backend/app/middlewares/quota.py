"""QuotaMiddleware -- 402 interceptor in front of LLM-generation routes.

Behaviour:
  - Runs *after* AuthMiddleware (so the JWT has already been validated) and
    *before* the actual route handler.
  - Only engages when env ``QUOTA_ENABLED`` is truthy ("1" / "true"). Default
    is off so the middleware is a no-op until explicitly turned on.
  - Only enforces on write methods (POST/PUT/PATCH) that target one of the
    configured LLM path prefixes (``QUOTA_PATHS`` -- comma separated, sensible
    defaults covering /api/generate, /api/pipeline, /api/rewrite,
    /api/knowledge, /api/reference-books, /api/styles).
  - Looks up ``usage_quotas`` for the request's ``user_id`` (JWT ``sub``).
    Returns HTTP 402 ``{"detail": "quota exceeded"}`` when
    ``quota_cents > 0`` and ``cost_cents >= quota_cents``.
  - Never raises: any failure (DB down, token malformed, service not seeded)
    is logged and the request is allowed through.
"""

from __future__ import annotations

import logging
import os

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.db.session import async_session_factory
from app.services.usage_service import check_quota

logger = logging.getLogger(__name__)

_DEFAULT_QUOTA_PATHS = (
    "/api/generate,"
    "/api/pipeline,"
    "/api/rewrite,"
    "/api/knowledge,"
    "/api/reference-books,"
    "/api/styles"
)

_ENFORCED_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _is_enabled() -> bool:
    return os.environ.get("QUOTA_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _quota_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("QUOTA_PATHS", _DEFAULT_QUOTA_PATHS)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _extract_username(auth_header: str) -> str | None:
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    try:
        # verify_signature=False because AuthMiddleware has already validated the
        # JWT signature + expiry. Re-verifying here would couple us to the secret
        # resolution and give no extra safety.
        payload = jwt.decode(token, options={"verify_signature": False})
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


class QuotaMiddleware(BaseHTTPMiddleware):
    """Block LLM-generation requests once the user's monthly quota is used up."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _is_enabled():
            return await call_next(request)

        if request.method not in _ENFORCED_METHODS:
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        prefixes = _quota_prefixes()
        if not any(path.startswith(p) for p in prefixes):
            return await call_next(request)

        username = _extract_username(request.headers.get("authorization", ""))
        if not username:
            # AuthMiddleware should have already rejected this; be permissive.
            return await call_next(request)

        try:
            async with async_session_factory() as db:
                allowed, row = await check_quota(db, user_id=username)
                await db.commit()
        except Exception as exc:
            logger.warning("quota check failed for user=%s path=%s: %s", username, path, exc)
            return await call_next(request)

        if not allowed:
            return JSONResponse(
                status_code=402,
                content={
                    "detail": "quota exceeded",
                    "user_id": username,
                    "month_ym": row.month_ym,
                    "used_cents": int(row.cost_cents or 0),
                    "quota_cents": int(row.quota_cents or 0),
                },
            )

        return await call_next(request)
