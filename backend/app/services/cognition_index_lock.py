"""Serialize one book's derived cognition index work without an open DB tx."""
from contextlib import asynccontextmanager
import hashlib
from uuid import UUID

from sqlalchemy import text

from app.services.production.locking import WorkerLease


def cognition_index_key(project_id):
    value = b'aiwrite:cognition-index:' + UUID(str(project_id)).bytes
    return int.from_bytes(hashlib.blake2b(value, digest_size=8).digest(), 'big', signed=True)


@asynccontextmanager
async def cognition_index_lock(project_id, *, wait=False):
    """Callers bound the total deadline; cleanup skips a book already in use.

    The dedicated physical connection is always discarded, including an
    ambiguous acquisition/commit or cancellation, so a pool cannot retain the
    session lock. Immutable writes arriving after disconnection are handled by
    a later sweep; the disconnected owner must not return usable context.
    """
    from app.db.session import engine
    async with engine.connect() as connection:
        try:
            statement = ('SELECT pg_advisory_lock(:key)' if wait
                         else 'SELECT pg_try_advisory_lock(:key)')
            result = await connection.scalar(text(statement), {'key': cognition_index_key(project_id)})
            acquired = True if wait else bool(result)
            await connection.commit()
            yield WorkerLease(acquired, connection)
        finally:
            await connection.invalidate()
