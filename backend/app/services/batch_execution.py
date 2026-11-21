"""Batch session ownership and transaction fences, including salvage sessions.

A dedicated PG connection owns the batch across chapters and hooks. A separate
project mutex still protects each chapter. Recovery must acquire both keys;
neither a timestamp nor a reconnectable pool connection proves exclusivity.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from uuid import UUID

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.generation_task import GenerationTask
from app.models.project import Project
from app.services.generation_execution import (
    PhysicalConnectionGuard, GenerationExecutionUnavailable, _drain_cleanup, _execution_engine,
)

EXECUTION_VERSION = 1
HEARTBEAT_SECONDS = 15
STALE_SECONDS = 90
MAX_ITEM_ATTEMPTS = 3


class StaleBatchExecution(RuntimeError):
    pass


@dataclass
class BatchLease:
    request_id: str
    connection: object
    guard: PhysicalConnectionGuard
    active: bool = True

    def revoke(self):
        self.active = False
        self.guard.revoke()

    def check(self):
        if not self.active:
            raise StaleBatchExecution('批次执行锁已失效，请核对原批次。')
        try:
            self.guard.check()
        except GenerationExecutionUnavailable:
            self.revoke()
            raise StaleBatchExecution('批次执行连接已失效，已停止旧执行写入。') from None

    async def probe(self):
        self.check()
        try:
            await self.connection.execute(text('SELECT 1'))
            await self.connection.commit()
        except BaseException:
            self.revoke()
            raise


_lease = ContextVar('generation_batch_lease', default=None)


@contextmanager
def using_batch_lease(lease):
    token = _lease.set(lease)
    try:
        yield lease
    finally:
        lease.revoke()
        _lease.reset(token)


def require_batch_lease(request_id):
    lease = _lease.get()
    if lease is None or lease.request_id != str(request_id):
        raise StaleBatchExecution('本次操作未持有所属批次的执行锁。')
    lease.check()
    return lease


def revoke_batch_lease(request_id):
    lease = _lease.get()
    if lease is not None and lease.request_id == str(request_id):
        lease.revoke()


def batch_lock_key(request_id):
    raw = b'aiwrite:batch:' + UUID(str(request_id)).bytes
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), 'big', signed=True)


@asynccontextmanager
async def batch_execution_lease(db, request_id):
    engine = _execution_engine(db)
    async with engine.connect() as connection:
        guard = None
        try:
            if connection.dialect.name != 'postgresql':
                raise StaleBatchExecution('批次恢复需要 PostgreSQL 执行锁。')
            guard = PhysicalConnectionGuard(connection.sync_connection)
            acquired = bool(await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),
                {'key': batch_lock_key(request_id)}))
            await connection.commit()
            if not acquired:
                yield None
                return
            with using_batch_lease(BatchLease(str(request_id), connection, guard)) as lease:
                yield lease
        finally:
            if guard is not None:
                guard.revoke()
            try:
                # Discard even after ambiguous acquisition or repeated cancel.
                await _drain_cleanup(connection.invalidate())
            finally:
                if guard is not None:
                    guard.detach()


@dataclass
class BatchExecution:
    request_id: str
    worker_id: UUID
    task_id: str
    terminal: bool = False
    active: bool = True


_execution = ContextVar('generation_batch_execution', default=None)


@contextmanager
def batch_execution(request_id, worker_id, task_id, *, terminal=False):
    owner = BatchExecution(str(request_id), worker_id, str(task_id), terminal)
    token = _execution.set(owner)
    try:
        yield owner
    finally:
        owner.active = False
        _execution.reset(token)


def batch_execution_allows(task_id, params):
    owner = _execution.get()
    return (owner is not None and owner.active and not owner.terminal
        and owner.request_id == params.get('batch_request_id') and owner.task_id == str(task_id))


def current_batch_execution():
    return _execution.get()


def protect_batch_write(connection, task_id=None):
    """Lock project -> batch -> item -> task before ORM writes in this context.

    Called explicitly by the existing task/body fences too, so listener
    registration order cannot reverse the row-lock order.
    """
    owner = _execution.get()
    if owner is None:
        return None
    if not owner.active or (task_id is not None and owner.task_id != str(task_id)):
        raise StaleBatchExecution('批次章节执行已结束，已停止迟到写入。')
    require_batch_lease(owner.request_id)
    # Hooks also lock Project or insert rows with a Project FK. Lock the
    # parent first, matching recovery and author deletion, to avoid holding
    # Batch while waiting for a Project row owned by those transactions.
    project_id = connection.scalar(select(Project.id).join(GenerationBatch,
        GenerationBatch.project_id == Project.id).where(GenerationBatch.id == UUID(owner.request_id),
        Project.deleted_at.is_(None)).with_for_update(of=Project, read=True))
    if project_id is None:
        raise StaleBatchExecution('批次作品已删除或不可编辑，已停止写入。')
    batch = connection.execute(select(GenerationBatch.status, GenerationBatch.worker_id,
        GenerationBatch.execution_version, GenerationBatch.project_id)
        .where(GenerationBatch.id == UUID(owner.request_id)).with_for_update()).first()
    if (batch is None or batch.status != 'running' or batch.worker_id != owner.worker_id
            or batch.execution_version != EXECUTION_VERSION or batch.project_id != project_id):
        raise StaleBatchExecution('批次执行凭证已变化，已停止旧执行写入。')
    item = connection.execute(select(GenerationBatchItem.status).where(
        GenerationBatchItem.task_id == UUID(owner.task_id),
        GenerationBatchItem.batch_id == UUID(owner.request_id)).with_for_update()).first()
    allowed = ('completed', 'needs_review') if owner.terminal else ('generating',)
    if item is None or item.status not in allowed:
        raise StaleBatchExecution('当前章节不在本次批次执行范围。')
    task = connection.execute(select(GenerationTask.status, GenerationTask.params_json).where(
        GenerationTask.id == UUID(owner.task_id)).with_for_update()).first()
    params = task.params_json if task is not None and isinstance(task.params_json, dict) else {}
    if (task is None or task.status == 'cancelled' or params.get('cancelled_at')
            or params.get('batch_request_id') != owner.request_id):
        raise StaleBatchExecution('批次章节任务已取消、删除或改变来源。')
    return owner


def _before_flush(session, _flush_context, _instances):
    if _execution.get() is not None and (session.dirty or session.new or session.deleted):
        protect_batch_write(session.connection())


def _before_execute(state):
    # Includes Core DML submitted through Session.execute and explicit row
    # locks. Plain context reads do not hold batch rows across model calls.
    if _execution.get() is not None and (state.is_insert or state.is_update or state.is_delete
            or getattr(state.statement, '_for_update_arg', None) is not None):
        protect_batch_write(state.session.connection())


event.listen(Session, 'before_flush', _before_flush)
event.listen(Session, 'do_orm_execute', _before_execute)
