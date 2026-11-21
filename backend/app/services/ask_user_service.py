"""AskUserService — lets LLM pause generation and wait for the author."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ask_user import AskUserPause

logger = logging.getLogger(__name__)

_TIMEOUT_SENTINEL = "(no reply — timed out)"


class AskUserService:
    async def ask(
        self,
        *,
        db: AsyncSession,
        project_id: Any,
        chapter_id: Any,
        run_id: UUID,
        question: str,
        timeout: int = 300,
    ) -> str:
        """Record a pause, publish to Redis, block until answer or timeout."""
        timeout_at = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        pause = AskUserPause(
            project_id=project_id,
            chapter_id=chapter_id,
            run_id=run_id,
            question=question,
            status="pending",
            timeout_at=timeout_at,
        )
        db.add(pause)
        await db.flush()
        await db.refresh(pause)

        channel = f"ask_user:{project_id}" if project_id else "ask_user:global"
        await self._publish(
            channel,
            json.dumps({"id": str(pause.id), "question": question}),
        )

        try:
            answer = await asyncio.wait_for(
                self._wait_for_answer(str(pause.id)),
                timeout=timeout,
            )
            pause.status = "answered"
            pause.answer = answer
            pause.answered_at = datetime.now(timezone.utc)
            await db.flush()
            return answer
        except asyncio.TimeoutError:
            pause.status = "timeout"
            await db.flush()
            return _TIMEOUT_SENTINEL

    async def _publish(self, channel: str, message: str) -> None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL)
        try:
            await client.publish(channel, message)
        finally:
            await client.close()

    async def _wait_for_answer(self, pause_id: str) -> str:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL)
        pubsub = client.pubsub()
        await pubsub.subscribe(f"ask_user:answer:{pause_id}")
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                return data or ""
            return ""
        finally:
            await pubsub.unsubscribe(f"ask_user:answer:{pause_id}")
            await client.close()
