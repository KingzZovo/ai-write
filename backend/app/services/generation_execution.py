"""Own the legacy runner's execution and every durable write on one connection.

The advisory key is shared with production workers and author write commands.
Pinning an AsyncSession alone is insufficient: SQLAlchemy can reconnect an
invalidated Connection. The SQL hooks below reject that replacement before it
can read or write on behalf of an actor whose session lock has been lost.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.services.production.control import PipelineConflict
from app.services.production.locking import project_lock_key


class GenerationExecutionUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('本次生成执行未获数据库确认，请刷新并核对已保存的检查点。')
        self.run_id: str | None = None


class GenerationExecutionLost(GenerationExecutionUnavailable):
    pass


class GenerationRunUnavailable(LookupError):
    pass


class GenerationRunInvalid(ValueError):
    pass


class PhysicalConnectionGuard:
    """Permanently reject business SQL once the original connection is lost."""

    def __init__(self, connection):
        self.connection = connection
        self.original = connection.connection.dbapi_connection
        self.revoked = False
        self.project_id = None  # Set only after the project lease is acquired.
        self.listeners = (
            ('before_execute', self._before_execute),
            ('before_cursor_execute', self._before_cursor_execute),
            ('begin', self.check),
            ('commit', self.check),
        )
        for name, listener in self.listeners:
            event.listen(connection, name, listener)

    def revoke(self):
        self.revoked = True

    def check(self, connection=None):
        connection = self.connection if connection is None else connection
        if (self.revoked or connection is not self.connection
                or connection.closed or connection.invalidated):
            self.revoke()
            raise GenerationExecutionLost()
        # Check invalidated first: accessing .connection may otherwise create
        # another DBAPI connection. Raw driver SQL also gets the cursor hook.
        if connection.connection.dbapi_connection is not self.original:
            self.revoke()
            raise GenerationExecutionLost()

    def _before_execute(self, connection, clauseelement, multiparams, params, execution_options):
        self.check(connection)

    def _before_cursor_execute(self, connection, cursor, statement, parameters, context, executemany):
        self.check(connection)

    def detach(self):
        for name, listener in self.listeners:
            event.remove(self.connection, name, listener)


class GenerationExecutionSession(AsyncSession):
    def __init__(self, *, guard, **kwargs):
        super().__init__(**kwargs)
        self.guard = guard

    async def commit(self):
        self.guard.check()
        try:
            await super().commit()
        except asyncio.CancelledError:
            self.guard.revoke()
            raise
        except Exception:
            # The server might have committed before the reply was lost. Do
            # not reconnect or overwrite that outcome with a failed state.
            self.guard.revoke()
            raise GenerationExecutionUnavailable() from None


def _execution_engine(db: AsyncSession) -> AsyncEngine:
    bind = db.bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    if isinstance(bind, AsyncEngine):
        return bind
    raise GenerationExecutionUnavailable()


@asynccontextmanager
async def generation_read_session(db: AsyncSession):
    """A short independent read never commits or rolls back the caller's work."""
    try:
        async with AsyncSession(bind=_execution_engine(db), expire_on_commit=False) as reader:
            yield reader
    except SQLAlchemyError:
        raise GenerationExecutionUnavailable() from None


async def _drain_cleanup(operation):
    """Delay repeated cancellation until this cleanup has actually finished."""
    pending = asyncio.create_task(operation)
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
    pending.result()
    if cancelled:
        raise asyncio.CancelledError()


@asynccontextmanager
async def generation_execution_session(db: AsyncSession, project_id):
    engine = _execution_engine(db)
    try:
        async with engine.connect() as connection:
            guard = None
            session = None
            try:
                # No in-process or SQLite fallback can protect real workers.
                if connection.dialect.name != 'postgresql':
                    raise GenerationExecutionUnavailable()
                guard = PhysicalConnectionGuard(connection.sync_connection)
                try:
                    acquired = bool(await connection.scalar(
                        text('SELECT pg_try_advisory_lock(:key)'),
                        {'key': project_lock_key(project_id)},
                    ))
                    await connection.commit()
                except Exception:
                    raise GenerationExecutionUnavailable() from None
                if not acquired:
                    raise PipelineConflict('本书已有生成或写入任务，请稍后再试。')
                guard.project_id = str(project_id)
                session = GenerationExecutionSession(
                    bind=connection, expire_on_commit=False, guard=guard,
                )
                yield session
            finally:
                if guard is not None:
                    guard.revoke()
                try:
                    if session is not None:
                        await _drain_cleanup(session.close())
                finally:
                    try:
                        # Covers ambiguous acquisition/commit and repeated
                        # cancellation. Never return a locked socket to a pool.
                        await _drain_cleanup(connection.invalidate())
                    finally:
                        if guard is not None:
                            guard.detach()
    except SQLAlchemyError:
        raise GenerationExecutionUnavailable() from None
