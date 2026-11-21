"""Reconcile orphaned batches without replacing delivered or edited prose."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import defer

from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Project, Volume
from app.services.batch_execution import EXECUTION_VERSION, MAX_ITEM_ATTEMPTS, STALE_SECONDS, require_batch_lease
from app.services.batch_receipts import receipt_matches
from app.services.batch_submissions import commit_batch
from app.services.generation_submissions import (
    chapter_submission_source, effective_generation_task_status, valid_chapter_submission_source,
)
from app.services.production.control import content_hash
from app.services.production.locking import try_command_lock

TERMINAL_ITEMS = frozenset(('completed', 'needs_review', 'error', 'cancelled'))
ACTIVE_TASKS = frozenset(('pending', 'running', 'evaluating', 'polishing'))


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def recovery_due(row, now):
    return (row.status == 'running' and row.execution_version == EXECUTION_VERSION
        and row.worker_id is not None and row.heartbeat_at is not None
        and _utc(row.heartbeat_at) <= now - timedelta(seconds=STALE_SECONDS))


def _task_matches(row, item, task):
    params = task.params_json if task is not None and isinstance(task.params_json, dict) else {}
    submission = params.get('submission')
    source = submission.get('chapter_source') if isinstance(submission, dict) else None
    return (task is not None and task.id == uuid5(row.id, str(item.chapter_id))
        and task.project_id == row.project_id and task.task_type == 'chapter'
        and params.get('batch_request_id') == str(row.id) and params.get('chapter_id') == str(item.chapter_id)
        and params.get('volume_idx') == item.volume_idx and params.get('chapter_idx') == item.chapter_idx
        and valid_chapter_submission_source(source, item.chapter_id))


def _review(item, task, reason):
    item.status, item.error_message = 'needs_review', reason
    if task is not None and effective_generation_task_status(task) in ACTIVE_TASKS:
        task.status, task.error_message = 'needs_review', reason


def _recovery_note(item, task, now, outcome):
    # Attempts are capped. Repeated reconciliation before the next attempt
    # retains each prior status/error instead of replacing the first crash.
    attempts = deepcopy(item.attempts_json or [])
    if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict):
        note = {'at': now.isoformat(), 'outcome': outcome,
            'task_status': task.status if task is not None else None,
            'task_error': task.error_message if task is not None else None}
        previous = attempts[-1].get('recoveries', [])
        if not isinstance(previous, list):
            previous = [previous]  # Preserve malformed historical evidence too.
        attempts[-1]['recoveries'] = [*previous, note]
        attempts[-1]['recovery'] = note
        item.attempts_json = attempts


def _cancel_waiting(item, task, now):
    message = '批次恢复需要核对已保存稿件，本章尚未继续；请核对后提交新的请求。'
    item.status, item.error_message = 'cancelled', message
    if task is not None and effective_generation_task_status(task) in ACTIVE_TASKS:
        params = task.params_json if isinstance(task.params_json, dict) else {}
        task.params_json = {**params, 'cancelled_at': params.get('cancelled_at') or now.isoformat()}
        task.status, task.error_message = 'cancelled', message


async def _quarantine_detached_tasks(db, row, known_ids):
    tasks = (await db.execute(select(GenerationTask).where(GenerationTask.project_id == row.project_id,
        GenerationTask.task_type == 'chapter',
        GenerationTask.params_json['batch_request_id'].as_string() == str(row.id))
        .options(defer(GenerationTask.progress_text), defer(GenerationTask.result_text), defer(GenerationTask.polished_text))
        .order_by(GenerationTask.id).with_for_update()
        .execution_options(populate_existing=True, autoflush=False))).scalars()
    for task in tasks:
        if task.id in known_ids or effective_generation_task_status(task) not in ACTIVE_TASKS:
            continue
        try:
            belongs = task.id == uuid5(row.id, str(UUID(task.params_json.get('chapter_id'))))
        except (TypeError, ValueError, AttributeError):
            belongs = False
        if belongs:
            task.status = 'needs_review'
            task.error_message = task.error_message or '批次清单缺少本章记录，请核对原任务；已保留现有输出。'


async def recover_batch(db, project_id, request_id, *, now=None, incomplete_pending=False):
    """Called only inside the acquired, live batch lease; commit one takeover.

    The project command lock excludes an old in-flight chapter too. Examine
    one chapter at a time instead of loading a 5-million-character book into
    a recovery scan. Completed item observations remain historical receipts.
    """
    lease = require_batch_lease(request_id)
    now = now or datetime.now(timezone.utc)
    pid, rid = UUID(str(project_id)), UUID(str(request_id))
    await db.rollback()
    if not await try_command_lock(db, pid):
        await db.rollback()
        return None
    project = await db.scalar(select(Project.id).where(Project.id == pid, Project.deleted_at.is_(None)).with_for_update())
    if project is None:
        await db.rollback()
        return None
    row = await db.scalar(select(GenerationBatch).where(GenerationBatch.id == rid, GenerationBatch.project_id == pid)
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    pending_manifest = row is not None and row.status == 'pending' and incomplete_pending
    if row is None or not (recovery_due(row, now) or pending_manifest):
        await db.rollback()
        return None
    old_worker = row.worker_id
    successor = uuid4()
    items = list((await db.execute(select(GenerationBatchItem).where(GenerationBatchItem.batch_id == rid)
        .order_by(GenerationBatchItem.position).options(defer(GenerationBatchItem.outline_json))
        .with_for_update().execution_options(populate_existing=True, autoflush=False))).scalars())
    complete_manifest = (len(items) == row.total_chapters
        and [item.position for item in items] == list(range(1, row.total_chapters + 1)))
    if pending_manifest and complete_manifest:
        await db.rollback()
        return None
    if not complete_manifest:
        await _quarantine_detached_tasks(db, row, {item.task_id for item in items})
    blocked = not complete_manifest or any(item.status in ('needs_review', 'error', 'cancelled') for item in items)
    waiting = []
    for item in items:
        if item.status in TERMINAL_ITEMS:
            continue
        task = await db.scalar(select(GenerationTask).where(GenerationTask.id == item.task_id)
            .with_for_update().execution_options(populate_existing=True, autoflush=False))
        if not _task_matches(row, item, task):
            # Never mutate a task whose identity no longer proves membership.
            _review(item, None, '原批次的章节任务或来源记录缺失，请核对已有正文和候选。')
            blocked = True
            continue
        state = effective_generation_task_status(task)
        chapter_row = (await db.execute(select(Chapter, Volume.project_id, Volume.volume_idx)
            .join(Volume, Chapter.volume_id == Volume.id).where(Chapter.id == item.chapter_id)
            .with_for_update(of=Chapter).execution_options(populate_existing=True, autoflush=False))).first()
        chapter = chapter_row[0] if chapter_row else None
        if state == 'cancelled':
            _recovery_note(item, task, now, 'cancelled')
            item.status, item.error_message = 'cancelled', task.error_message or '本章已取消，未自动恢复。'
            blocked = True
        elif (chapter is None or chapter_row.project_id != pid or chapter.volume_id != item.volume_id
                or chapter_row.volume_idx != item.volume_idx or chapter.chapter_idx != item.chapter_idx):
            _recovery_note(item, task, now, 'source_changed')
            _review(item, task, '章节已删除、移动或更改位置，请核对稿件；没有重新生成。')
            blocked = True
        elif item.saved_receipt_json is not None:
            matched = receipt_matches(item, chapter)
            if (matched and state == 'completed' and chapter.status == 'completed'
                    and content_hash(task.result_text or '') == item.saved_receipt_json['body_hash']):
                _recovery_note(item, task, now, 'delivered')
                item.status, item.word_count, item.error_message = 'completed', len(chapter.content_text), None
            else:
                _recovery_note(item, task, now, 'saved_requires_review')
                _review(item, task, '正文已经保存，但生成结果或当前稿件仍需核对；已保留正文和全部候选。'
                    if matched else '已保存正文的来源发生变化，请核对作者修改和原任务；没有覆盖当前稿。')
                item.word_count = len(chapter.content_text or '')
                blocked = True
        else:
            source = task.params_json['submission']['chapter_source']
            attempts = item.attempts_json
            last = attempts[-1] if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict) else {}
            prior_recovery = last.get('recovery') if isinstance(last.get('recovery'), dict) else {}
            waiting_after_recovery = (item.status == 'pending' and state == 'pending'
                and prior_recovery.get('worker_id') == str(old_worker)
                and prior_recovery.get('outcome') == 'retry_without_delivery')
            history_valid = (isinstance(attempts, list) and len(attempts) == item.attempt_count
                and 0 <= item.attempt_count <= MAX_ITEM_ATTEMPTS
                and (not attempts or last.get('worker_id') == str(old_worker) or waiting_after_recovery))
            unstarted = item.status == 'pending' and item.attempt_count == 0 and state == 'pending'
            interrupted = item.status == 'generating' and item.attempt_count > 0 and state in ACTIVE_TASKS
            produced = any((value or '').strip() for value in (task.result_text, task.polished_text, task.progress_text)) or bool(task.char_count)
            retryable = (history_valid and (unstarted or interrupted or waiting_after_recovery) and not produced
                and item.attempt_count < MAX_ITEM_ATTEMPTS and source == chapter_submission_source(chapter))
            if retryable:
                waiting.append((item, task))
            else:
                _recovery_note(item, task, now, 'review_required')
                _review(item, task, '本章已有输出、原稿变化、执行记录不完整或重试次数用尽，请核对原任务。')
                blocked = True
    for item, task in waiting:
        _recovery_note(item, task, now, 'paused_for_review' if blocked else 'retry_without_delivery')
        if blocked:
            _cancel_waiting(item, task, now)
        else:
            task.status, task.error_message = 'pending', None
            item.status, item.error_message = 'pending', None
            if item.attempts_json:
                attempts = deepcopy(item.attempts_json)
                attempts[-1]['recovery']['worker_id'] = str(successor)
                item.attempts_json = attempts
    lease.check()
    row.worker_id = successor
    row.heartbeat_at, row.recovery_count = now, row.recovery_count + 1
    if blocked:
        row.status, row.error_message = 'needs_review', (
            '批次清单不完整，已保留现有稿件，请核对原任务。' if not complete_manifest else
            '中断批次存在需要核对的稿件，后续章节已停止自动生成。')
    elif not waiting:
        states = {item.status for item in items}
        row.status = 'error' if states & {'error', 'cancelled'} else 'needs_review' if 'needs_review' in states else 'completed'
        row.error_message = None
    else:
        row.error_message = None
    if row.status != 'running':
        row.completed_at = now
    worker_id = row.worker_id if row.status == 'running' else None
    await commit_batch(db, rid)
    return worker_id
