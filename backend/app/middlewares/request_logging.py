"""Request-ID + structured-access-log middleware.

Chunk-24 (v1.2.0 B-series observability).

For every HTTP request:
  1. Ensure there's an ``X-Request-ID`` -- reuse the incoming header if the
     client supplied one, otherwise generate a UUID4.
  2. Stash it on ``request.state.request_id`` so downstream code can include
     it in its own log lines.
  3. After the response is produced, emit one JSON line via
     :func:`app.observability.logging.log_http_request` containing
     method / path / status / latency_ms / user_id / request_id.
  4. Echo the id back on the response as ``X-Request-ID``.

Sensitive headers / bodies are never read here -- only the authorization
header is peeked (without signature verification) to recover ``sub`` for
the user_id field, exactly like QuotaMiddleware does.
"""
from __future__ import annotations

import time
import uuid

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.observability.logging import log_http_request

_REQUEST_ID_HEADER = "X-Request-ID"


def _peek_user_id(auth_header: str) -> str | None:
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(auth_header[7:], options={"verify_signature": False})
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def _path_template(request: Request) -> str:
    route = request.scope.get("route")
    tpl = getattr(route, "path", None)
    return tpl or request.url.path


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Attach request_id + emit one structured JSON log line per request."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(_REQUEST_ID_HEADER)
        request_id = incoming.strip() if incoming else uuid.uuid4().hex
        request.state.request_id = request_id

        start = time.monotonic()
        status = 500
        response: Response | None = None
        try:
            response = await call_next(request)
            status = int(response.status_code)
            return response
        finally:
            latency_ms = (time.monotonic() - start) * 1000.0
            user_id = _peek_user_id(request.headers.get("authorization", ""))
            try:
                log_http_request(
                    method=request.method,
                    path=_path_template(request),
                    status=status,
                    latency_ms=latency_ms,
                    user_id=user_id,
                    request_id=request_id,
                )
            except Exception:
                # Never let logging crash the request path.
                pass
            if response is not None:
                response.headers.setdefault(_REQUEST_ID_HEADER, request_id)
