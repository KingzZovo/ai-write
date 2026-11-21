"""Fixed batch requests, ordered child tasks and committed progress.

This module does not call models or publish child tasks. A claimed batch may
invoke one child through the canonical worker, using the original sources.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Project, Volume
from app.services.generation_submissions import (
    GenerationSubmissionError, prepare_generation_submission, submission_intent,
)
from app.services.batch_execution import (
    EXECUTION_VERSION, MAX_ITEM_ATTEMPTS, StaleBatchExecution, batch_execution,
    batch_execution_allows, current_batch_execution, require_batch_lease, revoke_batch_lease,
)


class BatchSubmissionError(ValueError):
    def __init__(self, code, message, status=409, request_id=None):
        super().__init__(message)
        self.code, self.http_status = code, status
        self.detail = {'code': code, 'message': message}
        if request_id is not None: self.detail['request_id'] = str(request_id)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def batch_intent(project_id, request_id, configs, style_instruction=''):
    if request_id is None:
        raise BatchSubmissionError('batch_request_id_required', '请为这次批量生成保留固定请求编号。', 428)
    try:
        pid, rid = UUID(str(project_id)), UUID(str(request_id))
        if not isinstance(configs, list) or not configs or not isinstance(style_instruction, str):
            raise ValueError()
        normalized = []
        allowed = {'chapter_id', 'volume_id', 'chapter_idx', 'volume_idx', 'outline', 'user_instruction', 'expected_content_text'}
        for config in configs:
            if not isinstance(config, dict) or set(config) - allowed: raise ValueError()
            cid = str(UUID(str(config.get('chapter_id'))))
            if config.get('expected_content_text') is None:
                raise BatchSubmissionError('chapter_content_precondition_required',
                    '批量生成需要逐章提供作者已核对的完整保存稿。', 428, rid)
            if not isinstance(config['expected_content_text'], str): raise ValueError()
            item = {'chapter_id': cid, 'expected_content_text': config['expected_content_text'],
                    'user_instruction': config.get('user_instruction', '')}
            if not isinstance(item['user_instruction'], str): raise ValueError()
            for key in ('chapter_idx', 'volume_idx'):
                value = config.get(key)
                if value is not None and (type(value) is not int or not 0 < value <= 2147483647): raise ValueError()
                item[key] = value
            item['volume_id'] = str(UUID(str(config['volume_id']))) if config.get('volume_id') is not None else None
            item['outline'] = deepcopy(config.get('outline'))
            if item['outline'] is not None and not isinstance(item['outline'], dict): raise ValueError()
            normalized.append(item)
        if len({item['chapter_id'] for item in normalized}) != len(normalized): raise ValueError()
        fingerprint = digest({'version': 1, 'project_id': str(pid), 'chapters': normalized, 'style_instruction': style_instruction})
    except BatchSubmissionError:
        raise
    except (TypeError, ValueError, KeyError):
        raise BatchSubmissionError('batch_request_invalid', '批量请求或章节清单无效，请保留原稿并核对。', 422) from None
    return pid, rid, normalized, fingerprint


def match_batch(row, pid, fingerprint):
    if row.project_id != pid or row.request_hash != fingerprint:
        raise BatchSubmissionError('batch_request_conflict', '该编号已用于另一份批量请求，请先核对原批次。', request_id=row.id)
    return row


async def prepare_batch(db, project_id, request_id, configs, style_instruction=''):
    pid, rid, configs, fingerprint = batch_intent(project_id, request_id, configs, style_instruction)
    existing = await db.get(GenerationBatch, rid, populate_existing=True)
    if existing is not None: return match_batch(existing, pid, fingerprint)
    project = await db.scalar(select(Project).where(Project.id == pid, Project.deleted_at.is_(None))
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    if project is None:
        raise BatchSubmissionError('batch_project_unavailable', '作品不存在或已删除。', 404, rid)
    existing = await db.get(GenerationBatch, rid, populate_existing=True)
    if existing is not None: return match_batch(existing, pid, fingerprint)
    from app.services.chapter_order import ChapterOrderConflict, ordered_chapters, read_chapter_structure
    try:
        volumes, chapters = await read_chapter_structure(db, pid)
        ordered_chapters(volumes, chapters)
    except ChapterOrderConflict as exc:
        raise BatchSubmissionError('batch_chapter_conflict', str(exc), request_id=rid) from None
    by_id = {UUID(item['chapter_id']): item for item in configs}
    rows = (await db.execute(select(Chapter, Volume.volume_idx).join(Volume, Volume.id == Chapter.volume_id)
        .where(Volume.project_id == pid, Chapter.id.in_(by_id))
        .order_by(Volume.volume_idx, Chapter.chapter_idx, Chapter.id).with_for_update(of=Chapter)
        .execution_options(populate_existing=True, autoflush=False))).all()
    if len(rows) != len(configs):
        raise BatchSubmissionError('batch_chapter_unavailable', '清单包含不存在或不属于本书的章节。', 404, rid)
    row = GenerationBatch(id=rid, project_id=pid, request_hash=fingerprint, total_chapters=len(rows), status='pending')
    db.add(row)
    await db.flush()
    for position, (chapter, volume_idx) in enumerate(rows, 1):
        config = by_id[chapter.id]
        if (config['volume_id'] is not None and config['volume_id'] != str(chapter.volume_id)
                or config['outline'] is not None and config['outline'] != (chapter.outline_json or {})):
            raise BatchSubmissionError('batch_chapter_conflict', '章节所属分卷或细纲与核对时不同，请重新读取。', request_id=rid)
        child = submission_intent({'request_id': str(uuid5(rid, str(chapter.id))), 'project_id': str(pid),
            'task_type': 'chapter', 'chapter_id': str(chapter.id),
            'volume_idx': config['volume_idx'] if config['volume_idx'] is not None else volume_idx,
            'chapter_idx': config['chapter_idx'] if config['chapter_idx'] is not None else chapter.chapter_idx,
            'expected_content_text': config['expected_content_text'],
            'user_input': '\n\n'.join(filter(None, (style_instruction, config['user_instruction'])))})
        # The public single-task API cannot recreate this private request
        # fingerprint or use a batch child as an independent submission.
        child = replace(child, params={**child.params, 'batch_request_id': str(rid)},
                        fingerprint=digest({'batch_request_id': str(rid), 'child': child.fingerprint}))
        task = await prepare_generation_submission(db, child)
        db.add(GenerationBatchItem(task_id=task.id, batch_id=rid, chapter_id=chapter.id,
            volume_id=chapter.volume_id, volume_idx=volume_idx, chapter_idx=chapter.chapter_idx,
            position=position, outline_json=deepcopy(chapter.outline_json or {}), status='pending', word_count=0))
    await db.flush()
    return row


async def commit_batch(db, request_id):
    try:
        await db.commit()
    except asyncio.CancelledError:
        revoke_batch_lease(request_id)
        await db.rollback()
        raise
    except Exception:
        revoke_batch_lease(request_id)
        await db.rollback()
        raise BatchSubmissionError('batch_commit_unconfirmed',
            '批量请求或进度提交尚未确认，请保留原编号并读取同一批次，勿重新生成。', 503, request_id) from None


async def submit_batch(db, project_id, request_id, configs, style_instruction=''):
    try:
        async with asyncio.timeout(30):
            try:
                row = await prepare_batch(db, project_id, request_id, configs, style_instruction)
                accepted_id = str(row.id)
                await commit_batch(db, request_id)
            except IntegrityError:
                await db.rollback()
                row = await db.get(GenerationBatch, UUID(str(request_id)), populate_existing=True)
                if row is None: raise
                row = await prepare_batch(db, project_id, request_id, configs, style_instruction)
                accepted_id = str(row.id)
                await commit_batch(db, request_id)
            return accepted_id
    except (BatchSubmissionError, GenerationSubmissionError):
        await db.rollback()
        raise
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise BatchSubmissionError('batch_commit_unconfirmed',
            '批量受理结果尚未确认，请保留完整原请求和编号，再核对原批次。', 503, request_id) from None


async def read_batch(db, project_id, request_id, *, with_outlines=False):
    row = await db.scalar(select(GenerationBatch).where(GenerationBatch.id == UUID(str(request_id)),
        GenerationBatch.project_id == UUID(str(project_id))).execution_options(populate_existing=True, autoflush=False))
    if row is None:
        raise BatchSubmissionError('batch_unavailable', '本书中找不到这个批量请求。', 404, request_id)
    query = select(GenerationBatchItem).where(GenerationBatchItem.batch_id == row.id).order_by(GenerationBatchItem.position)
    if not with_outlines:
        query = query.options(defer(GenerationBatchItem.outline_json), defer(GenerationBatchItem.attempts_json),
                              defer(GenerationBatchItem.saved_receipt_json))
    items = list((await db.execute(query.execution_options(populate_existing=True, autoflush=False))).scalars())
    if len(items) != row.total_chapters or [item.position for item in items] != list(range(1, row.total_chapters + 1)):
        raise BatchSubmissionError('batch_manifest_incomplete', '批量章节清单不完整，请保留现有稿件并核对记录。', request_id=request_id)
    return row, items


async def batch_status(db, project_id, request_id):
    row, items = await read_batch(db, project_id, request_id)
    return {'job_id': str(row.id), 'request_id': str(row.id), 'project_id': str(row.project_id),
        'status': row.status, 'total': row.total_chapters,
        'completed': sum(item.status == 'completed' for item in items),
        'error': row.error_message or '',
        'results': [{'chapter_id': str(item.chapter_id), 'chapter': item.chapter_idx, 'volume_idx': item.volume_idx,
            'generation_task_id': str(item.task_id), 'status': item.status,
            'word_count': item.word_count, 'attempts': item.attempt_count,
            'error': item.error_message or ''} for item in items]}


async def claim_batch(db, project_id, request_id):
    require_batch_lease(request_id)
    project = await db.scalar(select(Project.id).where(Project.id == UUID(str(project_id)), Project.deleted_at.is_(None)))
    if project is None:
        raise BatchSubmissionError('batch_project_unavailable', '作品不存在或已删除。', 404, request_id)
    worker_id = uuid4()
    claimed = await db.scalar(update(GenerationBatch).where(GenerationBatch.id == UUID(str(request_id)),
        GenerationBatch.project_id == UUID(str(project_id)), GenerationBatch.status == 'pending')
        .values(status='running', worker_id=worker_id, execution_version=EXECUTION_VERSION,
                started_at=datetime.now(timezone.utc), heartbeat_at=datetime.now(timezone.utc))
        .returning(GenerationBatch.id).execution_options(synchronize_session=False))
    await commit_batch(db, request_id)
    return worker_id if claimed is not None else None


async def lock_batch_owner(db, request_id, worker_id):
    require_batch_lease(request_id)
    row = await db.scalar(select(GenerationBatch).where(GenerationBatch.id == UUID(str(request_id)))
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    if (row is None or row.status != 'running' or row.worker_id != worker_id
            or row.execution_version != EXECUTION_VERSION):
        raise StaleBatchExecution('批次执行权已经变化，未继续生成。')
    return row


async def record_item(db, request_id, worker_id, task_id, status, word_count=0, error=''):
    await lock_batch_owner(db, request_id, worker_id)
    item = await db.scalar(select(GenerationBatchItem).where(GenerationBatchItem.batch_id == UUID(str(request_id)),
        GenerationBatchItem.task_id == UUID(str(task_id))).with_for_update()
        .execution_options(populate_existing=True, autoflush=False))
    if item is None: raise StaleBatchExecution('章节不属于当前批次。')
    if status == 'generating':
        if item.status != 'pending' or item.attempt_count >= MAX_ITEM_ATTEMPTS:
            raise StaleBatchExecution('本章已有执行结果或已达到恢复次数上限。')
        item.attempt_count += 1
        item.attempts_json = [*(item.attempts_json or []), {
            'worker_id': str(worker_id), 'started_at': datetime.now(timezone.utc).isoformat(),
        }]
    item.status, item.word_count, item.error_message = status, word_count, error or None
    await commit_batch(db, request_id)


async def finish_batch(db, request_id, worker_id, status, error=''):
    row = await lock_batch_owner(db, request_id, worker_id)
    row.status, row.error_message = status, error or None
    row.completed_at = datetime.now(timezone.utc)
    await commit_batch(db, request_id)


async def cancel_pending_items(db, request_id, worker_id, *, message='批次已中断，本章尚未开始。'):
    """Close unstarted children when their owning stream is interrupted."""
    await db.rollback()
    await lock_batch_owner(db, request_id, worker_id)
    items = list((await db.execute(select(GenerationBatchItem).where(
        GenerationBatchItem.batch_id == UUID(str(request_id)), GenerationBatchItem.status == 'pending')
        .order_by(GenerationBatchItem.position).with_for_update()
        .execution_options(populate_existing=True, autoflush=False))).scalars())
    if items:
        tasks = list((await db.execute(select(GenerationTask).where(GenerationTask.id.in_([item.task_id for item in items]))
            .order_by(GenerationTask.id).with_for_update()
            .execution_options(populate_existing=True, autoflush=False))).scalars())
        by_id = {task.id: task for task in tasks}
        stamp = datetime.now(timezone.utc).isoformat()
        for item in items:
            task = by_id.get(item.task_id)
            if task is not None and task.status == 'pending':
                params = task.params_json if isinstance(task.params_json, dict) else {}
                task.params_json = {**params, 'cancelled_at': params.get('cancelled_at') or stamp}
                task.status, task.error_message = 'cancelled', message
                item.status, item.error_message = 'cancelled', task.error_message
            elif task is None:
                item.status, item.error_message = 'error', '原批次的章节任务已不存在，请核对记录。'
            elif task.status == 'cancelled':
                item.status, item.error_message = 'cancelled', task.error_message or '本章任务已取消。'
            else:
                item.status, item.error_message = 'error', '本章任务状态与批次清单不同，请核对原任务。'
    await commit_batch(db, request_id)


async def verify_batch_execution(db, task_id, params):
    if not batch_execution_allows(task_id, params): raise StaleBatchExecution('批量子任务不能独立执行。')
    owner = current_batch_execution()
    await lock_batch_owner(db, owner.request_id, owner.worker_id)
    status = await db.scalar(select(GenerationBatchItem.status).where(
        GenerationBatchItem.task_id == UUID(str(task_id)), GenerationBatchItem.batch_id == UUID(owner.request_id)))
    if status != 'generating': raise StaleBatchExecution('尚未轮到当前批量章节。')
