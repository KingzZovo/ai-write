"""PostgreSQL project mutex shared by commands and generation workers.

The worker uses a dedicated connection without an open transaction. Command
transactions use the same advisory key. A heartbeat timeout alone never grants
permission to overlap a worker that still owns its PostgreSQL connection.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession


@dataclass
class WorkerLease:
    acquired: bool
    connection: AsyncConnection

    def __bool__(self):
        return self.acquired

    async def check(self):
        if not self.acquired or self.connection.invalidated or self.connection.closed:
            raise RuntimeError("项目执行锁已断开")
        await self.connection.execute(text("SELECT 1"))
        await self.connection.commit()


def project_lock_key(project_id: str | UUID) -> int:
    raw = b"aiwrite:production:" + UUID(str(project_id)).bytes
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


async def try_command_lock(db: AsyncSession, project_id: str | UUID) -> bool:
    return bool(await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": project_lock_key(project_id)},
    ))


@asynccontextmanager
async def project_worker_lock(engine: AsyncEngine, project_id: str | UUID):
    async with engine.connect() as connection:
        acquired = bool(await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": project_lock_key(project_id)},
        ))
        await connection.commit()
        try:
            yield WorkerLease(acquired, connection)
        finally:
            if acquired:
                # Session-level advisory locks must never leak into a pool.
                # Closing this dedicated physical connection releases the lock
                # even if cleanup happens after an exception/cancellation.
                await connection.invalidate()
