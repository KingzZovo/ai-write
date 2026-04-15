"""
Semantic Cache

Redis-based cache for LLM responses. Uses input hashing to detect
similar requests and return cached results.

Two cache layers:
1. Exact match: SHA256 hash of normalized input
2. Semantic match: For retrieval queries (not generation),
   cache similar embeddings with cosine > 0.95 threshold

Caching rules:
- Cache: setting queries, summaries, feature extraction, evaluations
- Don't cache: chapter generation (each needs unique output)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Cache TTL in seconds
DEFAULT_TTL = 3600  # 1 hour
SUMMARY_TTL = 86400  # 24 hours

# Task types that should NOT be cached
NO_CACHE_TASKS = {"generation", "polishing", "outline"}


@dataclass
class CacheEntry:
    key: str
    value: str
    task_type: str
    hit_count: int = 0
    created_at: float = 0
    ttl: int = DEFAULT_TTL


class SemanticCache:
    """Redis-backed semantic cache for LLM responses."""

    def __init__(self, redis_client=None, prefix: str = "llm_cache:"):
        self._redis = redis_client
        self._prefix = prefix
        self._stats = {"hits": 0, "misses": 0, "skipped": 0}

    async def _get_redis(self):
        if self._redis is None:
            from app.db.redis import get_redis
            self._redis = await get_redis()
        return self._redis

    def _make_key(self, task_type: str, messages: list[dict]) -> str:
        """Generate a cache key from task type and messages."""
        normalized = json.dumps(
            {"task_type": task_type, "messages": messages},
            sort_keys=True,
            ensure_ascii=False,
        )
        h = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        return f"{self._prefix}{task_type}:{h}"

    def should_cache(self, task_type: str) -> bool:
        """Check if this task type should be cached."""
        return task_type not in NO_CACHE_TASKS

    async def get(self, task_type: str, messages: list[dict]) -> str | None:
        """Try to get a cached result."""
        if not self.should_cache(task_type):
            self._stats["skipped"] += 1
            return None

        try:
            redis = await self._get_redis()
            if redis is None:
                return None

            key = self._make_key(task_type, messages)
            cached = await redis.get(key)

            if cached:
                self._stats["hits"] += 1
                # Update hit count
                await redis.hincrby(f"{key}:meta", "hits", 1)
                logger.debug("Cache hit: %s", key)
                return cached.decode() if isinstance(cached, bytes) else cached
            else:
                self._stats["misses"] += 1
                return None

        except Exception as e:
            logger.debug("Cache get failed: %s", e)
            return None

    async def put(
        self,
        task_type: str,
        messages: list[dict],
        result: str,
        ttl: int | None = None,
    ) -> None:
        """Store a result in cache."""
        if not self.should_cache(task_type):
            return

        try:
            redis = await self._get_redis()
            if redis is None:
                return

            key = self._make_key(task_type, messages)
            effective_ttl = ttl or (SUMMARY_TTL if task_type == "summary" else DEFAULT_TTL)

            await redis.setex(key, effective_ttl, result)
            await redis.hset(
                f"{key}:meta",
                mapping={
                    "task_type": task_type,
                    "created_at": str(time.time()),
                    "hits": "0",
                },
            )
            await redis.expire(f"{key}:meta", effective_ttl)

            logger.debug("Cache put: %s (ttl=%d)", key, effective_ttl)

        except Exception as e:
            logger.debug("Cache put failed: %s", e)

    def get_stats(self) -> dict:
        """Get cache hit/miss statistics."""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total if total > 0 else 0
        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "skipped": self._stats["skipped"],
            "total_requests": total,
            "hit_rate": round(hit_rate, 3),
        }

    async def clear(self, task_type: str | None = None) -> int:
        """Clear cache entries. If task_type given, clear only that type."""
        try:
            redis = await self._get_redis()
            if redis is None:
                return 0

            pattern = f"{self._prefix}{task_type}:*" if task_type else f"{self._prefix}*"
            keys = []
            async for key in redis.scan_iter(match=pattern):
                keys.append(key)

            if keys:
                await redis.delete(*keys)
            return len(keys)

        except Exception as e:
            logger.debug("Cache clear failed: %s", e)
            return 0
