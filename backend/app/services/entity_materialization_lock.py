"""Transaction-scoped mutex for graph snapshot -> PostgreSQL projection."""
import hashlib
from uuid import UUID

from sqlalchemy import text


class MaterializationBusy(RuntimeError):
    pass


def materialization_key(project_id):
    raw = b'aiwrite:materialization:' + UUID(str(project_id)).bytes
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), 'big', signed=True)


async def require_materialization_lock(db, project_id):
    """Acquire before any graph snapshot read; hold on the write transaction.

    Separate namespace from entity worker and production locks: those callers
    may already hold their own locks on different PostgreSQL connections.
    Fail fast so callers holding author row locks cannot wait in a lock cycle.
    """
    if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),
                           {'key': materialization_key(project_id)}):
        raise MaterializationBusy('该作品的资料正在同步，请稍后重试。')
