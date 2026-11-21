"""Synchronous SQLite transport for the real independent execution service.

Only PG/async transport is substituted. Transactions, source checks, nonce
guards, manuscript fences, cancellation and durable rows remain real.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from uuid import UUID

from sqlalchemy import Uuid
from sqlalchemy.orm import Session

from app.services import generation_task_execution as execution
from app.services.generation_execution import GenerationExecutionUnavailable, PhysicalConnectionGuard
from app.services.production.control import PipelineConflict
from tests.test_generation_execution import LocalSession


def install_runtime(monkeypatch, engine):
    runtime = NS(held=set(), entries=[], sessions=[])
    original_uuid_bind = Uuid.bind_processor

    def uuid_bind(column_type, dialect):
        process = original_uuid_bind(column_type, dialect)
        if dialect.name != 'sqlite' or not column_type.as_uuid or process is None:
            return process
        # asyncpg accepts UUID strings as well as UUID objects. Match that
        # transport behavior without changing stored identity or the guards.
        return lambda value: process(UUID(value) if isinstance(value, str) else value)

    monkeypatch.setattr(Uuid, 'bind_processor', uuid_bind)

    class OwnedSession(LocalSession):
        def __init__(self, session, connection, guard):
            super().__init__(session)
            self.bind, self.guard = connection, guard
            runtime.sessions.append(self)
        async def commit(self):
            self.guard.check()
            try:
                await super().commit()
            except Exception as exc:
                self.guard.revoke()
                # Keep the SQLite failure diagnosable in test output. The
                # production session deliberately hides driver details.
                raise GenerationExecutionUnavailable() from exc

    @asynccontextmanager
    async def lease(_selector, project_id):
        project_id = UUID(str(project_id))
        if project_id in runtime.held:
            raise PipelineConflict('本书已有执行者。')
        runtime.held.add(project_id)
        runtime.entries.append(project_id)
        try:
            with engine.connect() as connection:
                guard = PhysicalConnectionGuard(connection)
                guard.project_id = str(project_id)
                try:
                    with Session(connection, expire_on_commit=False) as db:
                        yield OwnedSession(db, connection, guard)
                finally:
                    guard.revoke()
                    guard.detach()
        finally:
            runtime.held.remove(project_id)

    def owned_sessions(parent):
        @asynccontextmanager
        async def sessions():
            parent.guard.check()
            if parent.bind.in_transaction():
                raise execution.TaskExecutionLost('前一个事务尚未结束。')
            with Session(parent.bind, expire_on_commit=False) as db:
                yield OwnedSession(db, parent.bind, parent.guard)
        return sessions

    monkeypatch.setattr(execution, 'generation_execution_session', lease)
    monkeypatch.setattr(execution, 'owned_task_sessions', owned_sessions)
    runtime.OwnedSession = OwnedSession
    return runtime
