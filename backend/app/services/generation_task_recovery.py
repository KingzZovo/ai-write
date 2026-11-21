"""Recover only an expired, independently owned task with no retained delivery."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.generation_task import GenerationTask
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, Project, Volume
from app.services.generation_execution import GenerationExecutionUnavailable
from app.services.generation_submissions import valid_chapter_submission_source
from app.services.generation_task_execution import EXECUTION_VERSION, _valid_submission
from app.services.generation_task_sources import capture_task_source, source_digest, valid_task_source
from app.services.manuscript_receipts import manuscript_source
from app.services.production.control import ACTIVE_STATES, PipelineConflict, content_hash

STALE_SECONDS = 90
MAX_TASK_ATTEMPTS = 3
ACTIVE_TASK_STATUSES = ('running', 'evaluating', 'polishing', 'regenerating')
STATUS_PROGRESS = frozenset({
    'force_direct_chapter: 正在跳过 scene_planner，直接生成完整正文。',
    'scene_mode fallback: 正在切换为单段正文直出，避免 0 字卡死。',
})
RUNTIME_PARAMS = frozenset({'chinese_prose_mechanics_preflight', 'generation_stop_reason',
    'revision_stop_reason', 'quality_gate', 'fact_contract_gate', 'volume_candidate',
    'polish_candidate', 'sketch_line_count', 'reference_book'})


def recovery_due(task, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = task.heartbeat_at
    if not isinstance(stamp, datetime):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (task.status in ACTIVE_TASK_STATUSES and task.execution_version == EXECUTION_VERSION
        and task.execution_id is not None and stamp is not None
        and stamp <= now - timedelta(seconds=STALE_SECONDS))


def has_retained_output(task):
    if any(value not in (None, '') for value in (task.result_text, task.polished_text)):
        return True
    if task.char_count is not None and (type(task.char_count) is not int or task.char_count != 0):
        return True
    progress = task.progress_text or ''
    if not isinstance(progress, str):
        return True
    if progress.strip():
        # Both the typed marker and the exact known operation message are
        # required. Old or inconsistent records are never guessed as empty.
        return task.progress_kind != 'status' or progress not in STATUS_PROGRESS
    return task.progress_kind not in (None, 'status')


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('Execution timestamps must include their timezone')
    return stamp


def _valid_execution_record(task):
    original, current = task.execution_params_json, task.params_json
    if not isinstance(original, dict) or not isinstance(current, dict) or not _valid_submission(original):
        return False
    if not set(original) <= set(current) or set(current) - set(original) - RUNTIME_PARAMS:
        return False
    attempts = task.attempts_json
    if (type(task.attempt_count) is not int or not 1 <= task.attempt_count <= MAX_TASK_ATTEMPTS
            or not isinstance(attempts, list) or len(attempts) != task.attempt_count
            or not isinstance(task.recovery_history_json, list)):
        return False
    history, ids = task.recovery_history_json, []
    try:
        expected_hash = source_digest(original)
        if source_digest({key: current[key] for key in original}) != expected_hash:
            return False
        for attempt in attempts:
            if not isinstance(attempt, dict) or attempt.get('params_hash') != expected_hash:
                return False
            ids.append(UUID(attempt['execution_id']))
            _timestamp(attempt['started_at'])
        if len(history) != len(attempts) - 1:
            return False
        for index, decision in enumerate(history):
            if (not isinstance(decision, dict) or decision.get('outcome') != 'retry_without_delivery'
                    or decision.get('execution_id') != str(ids[index])
                    or decision.get('successor_id') != str(ids[index + 1])
                    or type(decision.get('attempt_count')) is not int or decision['attempt_count'] != index + 1
                    or _timestamp(decision['at']) != _timestamp(attempts[index + 1]['started_at'])):
                return False
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    return len(set(ids)) == len(ids) and ids[-1] == task.execution_id


def task_receipt_matches(task, chapter):
    proof = task.saved_manuscript_json
    params = task.execution_params_json if isinstance(task.execution_params_json, dict) else {}
    submission = params.get('submission') if isinstance(params.get('submission'), dict) else {}
    return (chapter is not None and isinstance(proof, dict) and type(proof.get('version')) is int
        and proof['version'] == 1 and task.task_type == 'chapter'
        and params.get('chapter_id') == str(chapter.id)
        and valid_chapter_submission_source(submission.get('chapter_source'), chapter.id)
        and proof.get('task_id') == str(task.id) and proof.get('execution_id') == str(task.execution_id)
        and proof.get('submission_fingerprint') == submission.get('fingerprint')
        and proof.get('source') == manuscript_source(chapter)
        and proof.get('body_hash') == content_hash(chapter.content_text)
        and proof.get('status') == chapter.status
        and type(proof.get('word_count')) is int and proof['word_count'] == len(chapter.content_text or ''))


def _decision(task, now, outcome, reason, successor):
    history = deepcopy(task.recovery_history_json)
    if isinstance(history, list):
        history.append({'at': now.isoformat(), 'execution_id': str(task.execution_id),
            'successor_id': str(successor), 'task_status': task.status, 'task_error': task.error_message,
            'attempt_count': task.attempt_count, 'outcome': outcome, 'reason': reason})
        task.recovery_history_json = history
    # Malformed history stays intact; the task can only be quarantined.
    task.execution_id, task.heartbeat_at = successor, now


async def recover_task_execution(db, task_id, project_id, *, now=None):
    """Require the actual project lease, then lock and reread Project -> Task.

    A recovery grants its successor directly in one transaction; there is no
    reset-to-pending window and no reset of first parameters or attempt count.
    """
    guard = getattr(db, 'guard', None)
    bind = getattr(db, 'bind', None)
    if (guard is None or getattr(guard, 'project_id', None) != str(project_id)
            or getattr(bind, 'sync_connection', bind) is not guard.connection):
        raise GenerationExecutionUnavailable()
    guard.check()
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        .with_for_update(read=True, key_share=True).execution_options(populate_existing=True, autoflush=False))
    task = await db.scalar(select(GenerationTask).where(GenerationTask.id == task_id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    if project is None or task is None:
        await db.commit()
        return None
    now = now or datetime.now(timezone.utc)
    params = task.params_json if isinstance(task.params_json, dict) else {}
    if (task.project_id != project.id or params.get('cancelled_at')
            or params.get('batch_request_id') is not None or params.get('pipeline_id') is not None
            or not recovery_due(task, now)):
        await db.commit()
        return None
    active = await db.scalar(select(PipelineRun.id).where(PipelineRun.project_id == project.id,
        PipelineRun.state.in_(ACTIVE_STATES)).limit(1))
    if active is not None:
        raise PipelineConflict('本书已有整书任务，独立任务未自动恢复。')
    chapter = None
    if task.task_type == 'chapter':
        original = task.execution_params_json if isinstance(task.execution_params_json, dict) else {}
        try:
            cid = UUID(str(original.get('chapter_id')))
        except ValueError:
            cid = None
        if cid is not None:
            chapter = await db.scalar(select(Chapter).join(Volume, Volume.id == Chapter.volume_id)
                .where(Chapter.id == cid, Volume.project_id == project.id).with_for_update(of=Chapter)
                .execution_options(populate_existing=True, autoflush=False))
    if task.saved_manuscript_json is not None:
        reason = ('正文已保存但任务没有完整结束，请核对保存稿与候选；没有重新生成。'
            if task_receipt_matches(task, chapter) else
            '正文保存记录或作者当前稿件需要核对，已保留全部内容；没有覆盖当前稿。')
        outcome = 'saved_requires_review'
    elif has_retained_output(task):
        outcome, reason = 'candidate_requires_review', '中断任务已有返回内容，已保留候选，请核对后再决定如何续写。'
    elif not _valid_execution_record(task):
        outcome, reason = 'record_requires_review', '原执行参数、尝试记录或任务归属无法核对，未自动重新生成。'
    elif not valid_task_source(task.execution_source_json, task.task_type):
        outcome, reason = 'source_requires_review', '旧任务缺少首次执行的来源证明，未补猜来源或自动重新生成。'
    elif task.attempt_count >= MAX_TASK_ATTEMPTS:
        outcome, reason = 'budget_exhausted', '本请求的自动尝试次数已用尽，请核对原任务后再决定是否提交新请求。'
    else:
        try:
            current_source = await capture_task_source(db, task, project=project, chapter=chapter)
        except (ValueError, TypeError):
            current_source = None
        if current_source != task.execution_source_json:
            outcome, reason = 'source_changed', '目标稿件、大纲或作品设置已变化，已保留作者修改，未自动重新生成。'
        else:
            outcome, reason = 'retry_without_delivery', '原来源仍一致，未发现已保存正文或返回候选，沿用原请求进行有限恢复。'
    successor = uuid4()
    _decision(task, now, outcome, reason, successor)
    if outcome != 'retry_without_delivery':
        task.status, task.error_message = 'needs_review', reason
        await db.commit()
        return None
    attempts = deepcopy(task.attempts_json)
    attempts.append({'execution_id': str(successor), 'started_at': now.isoformat(),
        'params_hash': source_digest(task.execution_params_json)})
    task.attempt_count += 1
    task.attempts_json = attempts
    task.status, task.error_message = 'running', None
    await db.commit()
    return task, (chapter.id, chapter.updated_at) if chapter is not None else None
