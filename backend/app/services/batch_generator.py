"""Sequential, persistent batch adapter over the canonical chapter chain."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
from uuid import UUID

from sqlalchemy import select

from app.models.generation_task import GenerationTask
from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.project import Chapter
from app.services.batch_submissions import (
    BatchSubmissionError, StaleBatchExecution, batch_execution, cancel_pending_items, claim_batch,
    commit_batch, finish_batch, lock_batch_owner, read_batch, record_item, submit_batch,
)
from app.services.generation_submissions import effective_generation_task_status
from app.services.hook_manager import HookManager
from app.services.batch_execution import HEARTBEAT_SECONDS, batch_execution_lease
from app.services.batch_receipts import receipt_matches
from app.services.batch_recovery import recover_batch
from app.services.production.control import content_hash

logger = logging.getLogger(__name__)


class BatchStatus(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    NEEDS_REVIEW = 'needs_review'
    ERROR = 'error'
    INTERRUPTED = 'interrupted'


@dataclass
class BatchChapterResult:
    chapter_idx: int
    chapter_id: str
    volume_idx: int = 0
    status: str = 'pending'
    word_count: int = 0
    error: str = ''
    generation_task_id: str | None = None


@dataclass
class BatchJobStatus:
    job_id: str
    project_id: str
    total_chapters: int
    completed_chapters: int = 0
    current_chapter: int = 0
    status: BatchStatus = BatchStatus.PENDING
    results: list[BatchChapterResult] = field(default_factory=list)
    error: str = ''


def _snapshot(row, items):
    return BatchJobStatus(str(row.id), str(row.project_id), row.total_chapters,
        completed_chapters=sum(item.status == 'completed' for item in items),
        current_chapter=max((item.position for item in items if item.status != 'pending'), default=0),
        status=BatchStatus(row.status), error=row.error_message or '',
        results=[BatchChapterResult(item.chapter_idx, str(item.chapter_id), item.volume_idx,
            item.status, item.word_count, item.error_message or '', str(item.task_id)) for item in items])


def _notify(callback, job):
    if callback is not None:
        try: callback(job)
        except Exception: logger.warning('Batch progress observer failed; committed progress is retained')


async def _stop_task(db, request_id, worker_id, task_id, message, *, cancelled=False):
    await db.rollback()
    await lock_batch_owner(db, request_id, worker_id)
    if cancelled:
        status = await db.scalar(select(GenerationBatchItem.status).where(
            GenerationBatchItem.batch_id == UUID(str(request_id)), GenerationBatchItem.task_id == UUID(str(task_id))))
        if status in ('completed', 'needs_review', 'error', 'cancelled'):
            await db.rollback()
            return False
    task = await db.scalar(select(GenerationTask).where(GenerationTask.id == UUID(str(task_id)))
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    if task is not None and effective_generation_task_status(task) in ('pending', 'running', 'evaluating', 'polishing'):
        task.status = 'cancelled' if cancelled else 'failed'
        task.error_message = message
        if cancelled:
            from datetime import datetime, timezone
            task.params_json = {**(task.params_json or {}), 'cancelled_at': datetime.now(timezone.utc).isoformat()}
    await commit_batch(db, request_id)
    return True


async def _observe(db, result):
    task = await db.get(GenerationTask, UUID(result.generation_task_id), populate_existing=True)
    chapter = await db.get(Chapter, UUID(result.chapter_id), populate_existing=True)
    item = await db.get(GenerationBatchItem, UUID(result.generation_task_id), populate_existing=True)
    matched = item is not None and receipt_matches(item, chapter)
    state = effective_generation_task_status(task) if task else None
    if (state == 'completed' and matched and chapter.status == 'completed'
            and content_hash(task.result_text or '') == item.saved_receipt_json['body_hash']):
        result.status = 'completed'
    elif state == 'cancelled':
        result.status = 'cancelled'
    elif state in ('completed', 'needs_review') or item is not None and item.saved_receipt_json is not None:
        result.status = 'needs_review'
    else:
        result.status = 'error'
    result.word_count = len(chapter.content_text or '') if chapter and result.status in ('completed', 'needs_review') else 0
    result.error = (task.error_message if task else None) or ('草稿或正文保存记录待核对' if result.status == 'needs_review' else
        '章节任务尚未确认交付，请核对原任务。' if result.status == 'error' else '')
    text = chapter.content_text if matched else None
    await db.rollback()
    return text


async def _drain_cleanup(operation):
    task = asyncio.create_task(operation)
    while not task.done():
        try: await asyncio.shield(task)
        except asyncio.CancelledError: continue
    return task.result()


async def _heartbeat(lease, project_id, request_id, worker_id):
    from app.db.session import async_session_factory
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with asyncio.timeout(HEARTBEAT_SECONDS):
            await lease.probe()
            async with async_session_factory() as db:
                row = await db.scalar(select(GenerationBatch).where(GenerationBatch.id == UUID(str(request_id)),
                    GenerationBatch.project_id == UUID(str(project_id))).with_for_update())
                if row is None or row.worker_id != worker_id:
                    raise StaleBatchExecution('批次心跳执行凭证已变化。')
                if row.status != 'running':
                    return
                row.heartbeat_at = datetime.now(timezone.utc)
                await commit_batch(db, request_id)


async def _with_heartbeat(operation, lease, project_id, request_id, worker_id):
    work = asyncio.create_task(operation)
    pulse = asyncio.create_task(_heartbeat(lease, project_id, request_id, worker_id))
    try:
        done, _ = await asyncio.wait((work, pulse), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return await work
        try:
            await pulse
        except BaseException:
            lease.revoke()
            raise StaleBatchExecution('批次心跳或执行锁已失效，请读取原批次核对进度。') from None
        return await work
    finally:
        for task in (work, pulse):
            if not task.done():
                task.cancel()
        async def drain():
            await asyncio.gather(work, pulse, return_exceptions=True)
        await _drain_cleanup(drain())


class BatchGenerator:
    def __init__(self):
        self.hook_manager = HookManager()

    async def generate_batch(self, project_id, chapter_configs, *, db, request_id=None,
                             style_instruction='', on_progress=None):
        request_id = await submit_batch(db, project_id, request_id, chapter_configs, style_instruction)
        return await self.run_submitted(project_id, request_id, db=db, on_progress=on_progress)

    async def run_submitted(self, project_id, request_id, *, db, on_progress=None, recover=False):
        try:
            row, items = await read_batch(db, project_id, request_id)
        except BatchSubmissionError as exc:
            if recover and exc.code == 'batch_manifest_incomplete':
                await db.rollback()
                async with batch_execution_lease(db, request_id) as lease:
                    if lease is not None:
                        await recover_batch(db, project_id, request_id, incomplete_pending=True)
            raise
        snapshot = _snapshot(row, items)
        await db.rollback()
        if snapshot.status != BatchStatus.PENDING and not recover:
            _notify(on_progress, snapshot)
            return snapshot
        async with batch_execution_lease(db, request_id) as lease:
            if lease is None:
                row, items = await read_batch(db, project_id, request_id)
                snapshot = _snapshot(row, items)
                await db.rollback()
                _notify(on_progress, snapshot)
                return snapshot
            worker_id = await claim_batch(db, project_id, request_id)
            if worker_id is None and recover:
                worker_id = await recover_batch(db, project_id, request_id)
            row, items = await read_batch(db, project_id, request_id, with_outlines=worker_id is not None)
            job = _snapshot(row, items)
            targets = [(str(item.volume_id), deepcopy(item.outline_json)) for item in items] if worker_id else []
            await db.rollback()
            if worker_id is None:
                _notify(on_progress, job)
                return job
            return await _with_heartbeat(self._run_claimed(project_id, request_id, worker_id, job, targets,
                db=db, on_progress=on_progress), lease, project_id, request_id, worker_id)

    async def _run_claimed(self, project_id, request_id, worker_id, job, targets, *, db, on_progress):
        from app.tasks.generation_tasks import _run_guarded_generation
        current = None
        try:
            for index, result in enumerate(job.results):
                if result.status != 'pending':
                    continue
                current = result
                volume_id, outline = targets[index]
                await record_item(db, request_id, worker_id, result.generation_task_id, 'generating')
                result.status, job.current_chapter = 'generating', index + 1
                _notify(on_progress, job)
                try:
                    with batch_execution(request_id, worker_id, result.generation_task_id):
                        hooks = await self.hook_manager.run_pre_hooks(project_id=project_id, volume_id=volume_id,
                            chapter_idx=result.chapter_idx, chapter_outline=outline)
                        if not hooks.can_proceed:
                            raise ValueError('; '.join(hooks.errors) or '章节前置检查未通过')
                        await _run_guarded_generation(result.generation_task_id)
                except (BatchSubmissionError, StaleBatchExecution):
                    raise
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ValueError) else '章节执行中断，请核对已保存正文和原任务。'
                    await _stop_task(db, request_id, worker_id, result.generation_task_id, message)
                text = await _observe(db, result)
                await record_item(db, request_id, worker_id, result.generation_task_id,
                    result.status, result.word_count, result.error)
                job.completed_chapters += int(result.status == 'completed')
                current = None
                _notify(on_progress, job)
                if result.status in ('completed', 'needs_review') and text is not None:
                    try:
                        with batch_execution(request_id, worker_id, result.generation_task_id, terminal=True):
                            await self.hook_manager.run_post_hooks(project_id=project_id, volume_id=volume_id,
                                chapter_idx=result.chapter_idx, chapter_text=text)
                    except StaleBatchExecution:
                        raise
                    except Exception:
                        logger.warning('Post-hook failed for saved batch chapter %s', result.chapter_id)
            states = {item.status for item in job.results}
            job.status = (BatchStatus.ERROR if states & {'error', 'cancelled'} else
                          BatchStatus.NEEDS_REVIEW if 'needs_review' in states else BatchStatus.COMPLETED)
            await finish_batch(db, request_id, worker_id, job.status.value)
            return job
        except asyncio.CancelledError:
            if worker_id is not None:
                async def interrupted():
                    if current is not None:
                        if await _stop_task(db, request_id, worker_id, current.generation_task_id,
                                '批量连接中断，已保存正文和候选保留。', cancelled=True):
                            await _observe(db, current)
                            await record_item(db, request_id, worker_id, current.generation_task_id,
                                current.status, current.word_count, current.error)
                    await cancel_pending_items(db, request_id, worker_id)
                    await finish_batch(db, request_id, worker_id, 'interrupted', '批量连接中断，请先读取原批次核对进度。')
                try: await _drain_cleanup(interrupted())
                except Exception: logger.warning('Batch interruption receipt unconfirmed for %s', request_id)
            raise
        finally:
            await _drain_cleanup(self.hook_manager.aclose())
