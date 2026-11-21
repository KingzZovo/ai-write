"""Run event bus (v1.0 chunk 9). Redis Stream wrapper for run:{id}:bus."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

from app.db.redis import get_redis

logger = logging.getLogger(__name__)

_STREAM_PREFIX = "run:"
_STREAM_SUFFIX = ":bus"
_MAXLEN = 2000


def stream_key(run_id: str) -> str:
    return f"{_STREAM_PREFIX}{run_id}{_STREAM_SUFFIX}"


async def _redis():
    async for r in get_redis():
        return r
    raise RuntimeError("Redis not initialized")


async def publish(run_id: str, *, agent: str, event: str, payload: dict[str, Any] | None = None) -> str | None:
    try:
        r = await _redis()
        body = {
            "ts": str(int(time.time() * 1000)),
            "agent": agent,
            "event": event,
            "payload": json.dumps(payload or {}, ensure_ascii=False),
        }
        return await r.xadd(stream_key(run_id), body, maxlen=_MAXLEN, approximate=True)
    except Exception as exc:
        logger.warning("run_bus publish failed: %s", exc)
        return None


async def read_history(run_id: str, *, count: int = 200) -> list[dict[str, Any]]:
    try:
        r = await _redis()
        entries = await r.xrevrange(stream_key(run_id), count=count)
    except Exception as exc:
        logger.warning("run_bus history failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for mid, fields in reversed(entries):
        out.append(_decode_entry(mid, fields))
    return out


async def follow(run_id: str, *, last_id: str = "$", block_ms: int = 15000) -> AsyncIterator[dict[str, Any]]:
    r = await _redis()
    key = stream_key(run_id)
    cursor = last_id
    while True:
        try:
            resp = await r.xread({key: cursor}, block=block_ms, count=50)
        except Exception as exc:
            logger.warning("run_bus follow read failed: %s", exc)
            return
        if not resp:
            yield {"id": cursor, "agent": "_bus", "event": "heartbeat", "payload": {}, "ts": int(time.time() * 1000)}
            continue
        for _k, entries in resp:
            for mid, fields in entries:
                cursor = mid
                yield _decode_entry(mid, fields)


def _decode_entry(mid: str, fields: dict[str, Any]) -> dict[str, Any]:
    raw_payload = fields.get("payload", "{}")
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except json.JSONDecodeError:
        payload = {"_raw": raw_payload}
    try:
        ts = int(fields.get("ts", 0))
    except (TypeError, ValueError):
        ts = 0
    return {
        "id": mid,
        "ts": ts,
        "agent": fields.get("agent", ""),
        "event": fields.get("event", ""),
        "payload": payload,
    }
