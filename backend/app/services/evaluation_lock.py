"""Dedicated PostgreSQL session mutex for duplicate evaluation deliveries."""
from contextlib import asynccontextmanager
import hashlib
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.production.locking import WorkerLease


def evaluation_lock_key(task_id):
    value = b"aiwrite:evaluation:" + UUID(str(task_id)).bytes
    return int.from_bytes(hashlib.blake2b(value, digest_size=8).digest(), "big", signed=True)


class EvaluationLease(WorkerLease):
    def session(self):
        # All writes use the lock-owning physical connection. A different pool
        # connection must not commit an old result after lock ownership is lost.
        return AsyncSession(bind=self.connection, expire_on_commit=False)


@asynccontextmanager
async def evaluation_worker_lock(task_id):
    from app.db.session import engine
    async with engine.connect() as connection:
        try:
            acquired = bool(await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),
                                                   {'key': evaluation_lock_key(task_id)}))
            await connection.commit()
            yield EvaluationLease(acquired, connection)
        finally:
            # Also discard on an ambiguous acquisition/commit failure: the
            # server may own the lock even if the client never got the reply.
            await connection.invalidate()
