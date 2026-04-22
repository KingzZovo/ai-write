"""Run bus SSE + history API (v1.0 chunk 9).

Endpoints:
  GET /api/runs/{run_id}/bus           — SSE stream following the Redis stream
  GET /api/runs/{run_id}/bus/history   — last 200 events (JSON)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.services import run_bus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["run-bus"])


def _sse_pack(evt: dict[str, Any]) -> str:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"


async def _sse_stream(run_id: str) -> AsyncIterator[bytes]:
    # 1) emit history first so late joiners have context
    try:
        history = await run_bus.read_history(run_id, count=200)
    except Exception as exc:
        logger.warning("run_bus history (pre-follow) failed: %s", exc)
        history = []
    for evt in history:
        yield _sse_pack(evt).encode("utf-8")
    # 2) follow the stream for new events
    try:
        async for evt in run_bus.follow(run_id, last_id="$", block_ms=15000):
            yield _sse_pack(evt).encode("utf-8")
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("run_bus follow (sse) failed: %s", exc)
        yield _sse_pack({"agent": "_bus", "event": "closed", "payload": {"reason": str(exc)[:200]}}).encode("utf-8")
        return


@router.get("/api/runs/{run_id}/bus")
async def run_bus_sse(run_id: str) -> StreamingResponse:
    """Server-Sent Events stream for run:{run_id}:bus."""
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _sse_stream(run_id),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/api/runs/{run_id}/bus/history")
async def run_bus_history(run_id: str) -> dict[str, Any]:
    events = await run_bus.read_history(run_id, count=200)
    return {"run_id": run_id, "events": events, "count": len(events)}
