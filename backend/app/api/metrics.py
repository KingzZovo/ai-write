"""Prometheus scrape endpoint (public, intranet-only)."""
from __future__ import annotations

from fastapi import APIRouter, Response

from app.observability.metrics import render_latest

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)
