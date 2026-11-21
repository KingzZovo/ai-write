"""Independent task ownership, bound write sessions and live cancellation.

Every canonical write uses the physical connection that owns the project's
advisory lock. Opening a new Session after a model error cannot reconnect a
revoked actor. Read-only context preparation may use separate connections.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import Context, ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine

from app.models.generation_task import GenerationTask
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, Project, Volume
from app.services.generation_execution import (
    GenerationExecutionSession, GenerationExecutionUnavailable, _drain_cleanup, generation_execution_session,
)
from app.services.production.control import ACTIVE_STATES, PipelineConflict

EXECUTION_VERSION = 1
HEARTBEAT_SECONDS = 15
TERMINAL_STATUSES = {'completed', 'needs_review', 'needs_repair', 'failed', 'cancelled'}
CANONICAL_TABLES = {'generation_tasks', 'projects', 'volumes', 'chapters', 'outlines',
                    'chapter_versions', 'chapter_evaluations', 'chapter_variants'}


class TaskExecutionLost(RuntimeError):
    pass


@dataclass
class TaskExecution:
    task_id: UUID
    project_id: UUID
    execution_id: UUID
    guard: object
    active: bool = True
    verified_transaction: object = None

    def check(self):
        if not self.active:
            raise TaskExecutionLost('独立任务执行已结束，已停止迟到写入。')
        self.guard.check()

    def revoke(self):
        self.active = False
        self.guard.revoke()


_owner = ContextVar('independent_generation_task_execution', default=None)


def current_task_execution():
    return _owner.get()


@contextmanager
def task_execution(owner):
    token = _owner.set(owner)
    try:
        yield owner
    finally:
        owner.active = False
        _owner.reset(token)


def protect_task_write(connection):
    owner = current_task_execution()
    if owner is None:
        return None
    owner.check()
    if connection is not owner.guard.connection:
        raise TaskExecutionLost('任务写入没有使用持锁连接，已停止旁路保存。')
    if connection.get_nested_transaction() is not None:
        # Rolling back a savepoint can release row locks while leaving its
        # outer transaction alive. No current task writer needs savepoints;
        # refuse them instead of reusing a no-longer-locked verification.
        raise TaskExecutionLost('任务保存不支持嵌套事务，请先结束当前事务。')
    transaction = connection.get_transaction()
    if owner.verified_transaction is transaction and transaction is not None:
        return owner
    # The parent and task row stay locked for this write transaction. Cancel
    # or a future takeover cannot change the nonce between check and commit.
    project = connection.scalar(select(Project.id).where(Project.id == owner.project_id,
        Project.deleted_at.is_(None)).with_for_update(read=True, key_share=True))
    row = connection.execute(select(GenerationTask.project_id, GenerationTask.execution_id,
        GenerationTask.execution_version, GenerationTask.status, GenerationTask.params_json)
        .where(GenerationTask.id == owner.task_id).with_for_update()).first()
    params = row.params_json if row is not None and isinstance(row.params_json, dict) else {}
    if (project is None or row is None or row.project_id != owner.project_id
            or row.execution_id != owner.execution_id or row.execution_version != EXECUTION_VERSION
            or row.status == 'cancelled' or params.get('cancelled_at')):
        raise TaskExecutionLost('任务已取消、删除或执行凭证已变化，已停止旧执行写入。')
    owner.verified_transaction = connection.get_transaction()
    return owner


def protect_task_flush(session):
    if current_task_execution() is not None and any(
            getattr(row, '__tablename__', None) in CANONICAL_TABLES
            for row in session.dirty | session.new | session.deleted):
        protect_task_write(session.connection())


def _protect_core(connection, clause, _multiparams, _params, _options):
    if current_task_execution() is not None and getattr(clause, 'is_dml', False):
        if getattr(getattr(clause, 'table', None), 'name', None) in CANONICAL_TABLES:
            protect_task_write(connection)


event.listen(Engine, 'before_execute', _protect_core)


def owned_task_sessions(execution):
    """Fresh identity maps, one owned connection, no overlapping transactions."""
    @asynccontextmanager
    async def sessions():
        execution.guard.check()
        connection = execution.bind
        if connection.in_transaction():
            raise TaskExecutionLost('前一个任务事务尚未结束，不能在同一连接开启另一个保存会话。')
        db = GenerationExecutionSession(bind=connection, expire_on_commit=False, guard=execution.guard)
        try:
            yield db
        finally:
            # AsyncSession.__aexit__ shields close but can itself return on
            # cancellation while close is still running. Drain it before the
            # shared connection is reused or the project lease is released.
            await _drain_cleanup(db.close())
    return sessions


def _valid_submission(params):
    source = params.get('submission') if isinstance(params, dict) else None
    digest = source.get('fingerprint') if isinstance(source, dict) else None
    return (isinstance(source, dict) and type(source.get('version')) is int and source['version'] == 1
        and isinstance(digest, str) and len(digest) == 64 and all(char in '0123456789abcdef' for char in digest))


async def prepare_task_execution(db, task_id, project_id, *, recover=False):
    """Validate target then commit the first execution before any model work."""
    from app.services.generation_submissions import chapter_submission_source, valid_chapter_submission_source
    from app.services.production.fencing import StaleChapterWrite
    from app.services.volume_outline_writes import VolumeOutlineError, require_volume_task_source
    from app.services.generation_task_sources import capture_task_source, source_digest

    project = await db.scalar(select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        .with_for_update(read=True, key_share=True).execution_options(populate_existing=True))
    if project is None:
        raise StaleChapterWrite('作品不存在或已删除，未开始生成。')
    task = await db.scalar(select(GenerationTask).where(GenerationTask.id == task_id)
        .with_for_update().execution_options(populate_existing=True))
    if task is not None and task.status != 'pending' and recover:
        from app.services.generation_task_recovery import recover_task_execution
        return await recover_task_execution(db, task.id, project_id)
    if task is None or task.status != 'pending':
        await db.commit()
        return None
    params = deepcopy(task.params_json) if isinstance(task.params_json, dict) else {}
    if task.project_id != project_id or params.get('batch_request_id') is not None or params.get('pipeline_id') is not None:
        raise StaleChapterWrite('任务归属不符，不能由独立执行器启动。')
    if params.get('cancelled_at'):
        await db.commit()
        return None
    if (task.execution_version != 0 or task.execution_id is not None or (task.attempt_count or 0) != 0
            or task.heartbeat_at is not None or task.execution_params_json is not None
            or task.execution_source_json is not None or task.attempts_json != [] or task.recovery_history_json != []):
        raise StaleChapterWrite('任务已有执行记录，不能按新任务重新认领。')
    from app.services.generation_task_recovery import has_retained_output
    if task.saved_manuscript_json is not None or has_retained_output(task):
        task.status = 'needs_review'
        task.error_message = '任务已有返回内容但缺少可核对的执行来源，已保留待审；没有重新生成。'
        await db.commit()
        return None
    if not _valid_submission(params):
        raise StaleChapterWrite('任务缺少原始提交证明，请核对后重新提交；未开始生成。')
    active = await db.scalar(select(PipelineRun.id).where(PipelineRun.project_id == project_id,
        PipelineRun.state.in_(ACTIVE_STATES)).limit(1))
    if active is not None:
        raise PipelineConflict('本书已有整书任务，请先暂停后再生成。')
    chapter = None
    if task.task_type == 'chapter':
        source = params['submission'].get('chapter_source')
        if not valid_chapter_submission_source(source, params.get('chapter_id')):
            raise StaleChapterWrite('任务缺少提交时的原稿基准，请核对保存稿后重新提交；未开始生成。')
        chapter = await db.scalar(select(Chapter).join(Volume, Volume.id == Chapter.volume_id)
            .where(Chapter.id == UUID(params['chapter_id']), Volume.project_id == project_id)
            .with_for_update(of=Chapter, read=True).execution_options(populate_existing=True))
        if chapter is None or chapter_submission_source(chapter) != source:
            raise StaleChapterWrite('章节在任务提交后已变化，请核对保存稿后重新提交；未开始生成。')
    elif task.task_type == 'outline_volume':
        try:
            await require_volume_task_source(db, project_id, params)
        except VolumeOutlineError as exc:
            raise StaleChapterWrite(str(exc)) from None
    elif task.task_type not in {'outline_book', 'outline_chapter', 'outline_from_reference'}:
        raise StaleChapterWrite('生成任务类型无效，未开始生成。')
    elif task.task_type == 'outline_chapter':
        try:
            cid = UUID(str(params.get('chapter_id')))
        except ValueError:
            raise StaleChapterWrite('章纲任务缺少有效章节。') from None
        target = await db.scalar(select(Chapter.id).join(Volume, Volume.id == Chapter.volume_id)
            .where(Chapter.id == cid, Volume.project_id == project_id))
        if target is None:
            raise StaleChapterWrite('章纲任务的章节已删除或不属于本书。')
    source = await capture_task_source(db, task, project=project, chapter=chapter)
    params_hash = source_digest(params)
    now, execution_id = datetime.now(timezone.utc), uuid4()
    task.execution_version = EXECUTION_VERSION
    task.execution_id = execution_id
    task.execution_params_json = params
    task.execution_source_json = source
    task.heartbeat_at = now
    task.attempt_count = 1
    task.attempts_json = [{'execution_id': str(execution_id), 'started_at': now.isoformat(),
                          'params_hash': params_hash}]
    task.status = 'running'
    await db.commit()
    return task, (chapter.id, chapter.updated_at) if chapter is not None else None


async def _heartbeat(owner, sessions, stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
            return
        except TimeoutError:
            pass
        # This check performs no SQL on the write connection. A known lost
        # connection must not be kept alive by an independent heartbeat.
        owner.check()
        async with asyncio.timeout(10):
            async with sessions() as db:
                row = (await db.execute(update(GenerationTask).where(GenerationTask.id == owner.task_id,
                    GenerationTask.execution_id == owner.execution_id,
                    GenerationTask.execution_version == EXECUTION_VERSION,
                    GenerationTask.status.notin_(TERMINAL_STATUSES),
                    GenerationTask.params_json['cancelled_at'].as_string().is_(None))
                    .values(heartbeat_at=datetime.now(timezone.utc), updated_at=GenerationTask.updated_at)
                    .returning(GenerationTask.id).execution_options(synchronize_session=False))).first()
                owner.check()
                await db.commit()
                if row is not None:
                    continue
                current = (await db.execute(select(GenerationTask.execution_id, GenerationTask.status,
                    GenerationTask.params_json).where(GenerationTask.id == owner.task_id))).first()
                params = current.params_json if current is not None and isinstance(current.params_json, dict) else {}
                if (current is not None and current.execution_id == owner.execution_id
                        and current.status in TERMINAL_STATUSES - {'cancelled'} and not params.get('cancelled_at')):
                    return
                raise TaskExecutionLost('任务已取消、删除或执行权已变更。')


async def _supervise(owner, operation, sessions):
    stop = asyncio.Event()
    work = asyncio.create_task(operation())
    # The heartbeat is a narrowly scoped nonce-CAS on a separate connection;
    # it must not inherit the canonical-write context or share a transaction.
    pulse = asyncio.create_task(_heartbeat(owner, sessions, stop), context=Context())
    try:
        done, _ = await asyncio.wait((work, pulse), return_when=asyncio.FIRST_COMPLETED)
        if pulse in done:
            try:
                pulse.result()
            except BaseException:
                owner.revoke()
                raise
        # The draining path sends one cancellation. Repeated cancellation of
        # this supervisor must not interrupt the child's own cleanup.
        return await asyncio.shield(work)
    finally:
        stop.set()
        async def drain():
            for task in (work, pulse):
                if not task.done(): task.cancel()
            await asyncio.gather(work, pulse, return_exceptions=True)
        await _drain_cleanup(drain())


async def execute_independent_task(task_id, project_id, *, sessions, generate, recover=False):
    from app.services.generation_submissions import generation_task_results
    from app.services.production.fencing import StaleChapterWrite, standalone_write_fence

    task_id, project_id = UUID(str(task_id)), UUID(str(project_id))
    async with sessions() as selector:
        async with generation_execution_session(selector, project_id) as execution:
            try:
                prepared = await prepare_task_execution(execution, task_id, project_id, recover=recover)
            except (PipelineConflict, GenerationExecutionUnavailable):
                raise
            except Exception as exc:
                await execution.rollback()
                # Rejection is still a write: reacquire parent then task and
                # observe a cancellation that committed after validation.
                project = await execution.scalar(select(Project.id).where(Project.id == project_id)
                    .with_for_update(read=True, key_share=True))
                row = await execution.get(GenerationTask, task_id, populate_existing=True, with_for_update=True)
                params = row.params_json if row is not None and isinstance(row.params_json, dict) else {}
                if (project is not None and row is not None and row.project_id == project_id
                        and row.status == 'pending' and not params.get('cancelled_at')
                        and params.get('batch_request_id') is None and params.get('pipeline_id') is None):
                    row.status = 'failed'
                    row.error_message = str(exc) if isinstance(exc, StaleChapterWrite) else '任务来源无法核对，未开始生成。'
                    await execution.commit()
                raise
            if prepared is None:
                return
            task, chapter = prepared
            owner = TaskExecution(task.id, project_id, task.execution_id, execution.guard)
            writers = owned_task_sessions(execution)
            fence = standalone_write_fence(project_id, chapter[0], chapter[1], task_id) if chapter else nullcontext()
            with task_execution(owner), generation_task_results(task_id), fence:
                try:
                    await _supervise(owner, lambda: generate(writers, sessions), sessions)
                    owner.check()
                    row = await execution.get(GenerationTask, task_id, populate_existing=True)
                    if row is not None and row.status not in TERMINAL_STATUSES:
                        retained = row.saved_manuscript_json is not None or any(
                            (value or '').strip() for value in (row.result_text, row.progress_text, row.polished_text))
                        row.status = 'needs_review' if retained else 'failed'
                        row.error_message = '生成执行已退出，但没有完整交付状态，请核对已保存的正文与候选。'
                        await execution.commit()
                except (GenerationExecutionUnavailable, TaskExecutionLost):
                    raise
                except Exception as exc:
                    await execution.rollback()
                    row = await execution.get(GenerationTask, task_id, populate_existing=True)
                    if row is not None and row.status not in TERMINAL_STATUSES:
                        row.status = 'failed'
                        row.error_message = '生成任务中断，已保存的正文和候选可供核对。'
                        await execution.commit()
                    if isinstance(exc, PipelineConflict):
                        raise TaskExecutionLost('已认领任务中断，不能通过消息重投重新执行。') from None
                    raise
