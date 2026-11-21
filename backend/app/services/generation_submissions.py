"""Durable async submissions and the chapter the author submitted against.

The client UUID is the task primary key. Reusing it can publish the same
pending task again, but cannot create another task or change its request.
Only a pending-to-running compare-and-set grants execution to a worker.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone
import asyncio
import hashlib
import json
import logging
from uuid import UUID

from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Outline, Project, Volume
from app.services.outline_readiness import (
    LAYER_LABELS, build_outline_readiness_report, has_meaningful_outline_content,
)
from app.services.production.fencing import StaleChapterWrite

logger = logging.getLogger(__name__)


class GenerationSubmissionError(ValueError):
    def __init__(self, code, message, **context):
        super().__init__(message)
        self.code = code
        self.detail = {'code': code, 'message': message, **context}

    @property
    def http_status(self):
        if self.code in {'generation_request_id_required', 'chapter_content_precondition_required'}:
            return 428
        if self.code in {'generation_request_conflict', 'chapter_edit_conflict'}:
            return 409
        if self.code == 'generation_submission_unconfirmed':
            return 503
        if self.code.endswith('_unavailable'):
            return 404
        return 422


def generation_task_identity(task):
    """Expose the persisted target without arbitrary task parameters."""
    params = task.params_json if isinstance(task.params_json, dict) else {}
    chapter_id, volume_idx = params.get('chapter_id'), params.get('volume_idx')
    return {
        'task_id': str(task.id), 'project_id': str(task.project_id), 'task_type': task.task_type,
        'chapter_id': chapter_id if isinstance(chapter_id, str) and chapter_id.strip() else None,
        'volume_idx': volume_idx if type(volume_idx) is int and volume_idx > 0 else None,
    }


def effective_generation_task_status(task):
    params = task.params_json if isinstance(task.params_json, dict) else {}
    return 'cancelled' if params.get('cancelled_at') else task.status


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def chapter_material_values(chapter):
    """Canonical values shared by submission and manuscript receipts."""
    stamp = chapter.updated_at
    if stamp is not None:
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    values = {key: getattr(chapter, key) for key in (
        'title', 'content_text', 'outline_json', 'chapter_idx', 'global_idx',
        'status', 'summary', 'summary_source_hash', 'target_word_count',
    )}
    values.update(id=str(chapter.id), volume_id=str(chapter.volume_id),
                  updated_at=stamp.isoformat(timespec='microseconds') if stamp else None)
    return values


def chapter_submission_source(chapter):
    """A bounded receipt; task lists must never load a copy of the old body."""
    return {'version': 1, 'chapter_id': str(chapter.id), 'material_hash': _hash(chapter_material_values(chapter))}


def valid_chapter_submission_source(value, chapter_id):
    return (isinstance(value, dict) and set(value) == {'version', 'chapter_id', 'material_hash'}
        and type(value.get('version')) is int and value['version'] == 1
        and value.get('chapter_id') == str(chapter_id)
        and isinstance(value.get('material_hash'), str) and len(value['material_hash']) == 64
        and all(c in '0123456789abcdef' for c in value['material_hash']))


@dataclass(frozen=True)
class SubmissionIntent:
    request_id: UUID
    project_id: UUID
    task_type: str
    params: dict
    expected_content_text: str | None
    fingerprint: str


def submission_intent(values):
    request_id = values.get('request_id')
    if request_id is None:
        raise GenerationSubmissionError('generation_request_id_required', '请为本次生成保留固定请求编号，再提交任务。')
    try:
        rid, pid = UUID(str(request_id)), UUID(str(values['project_id']))
    except (ValueError, TypeError, KeyError):
        raise GenerationSubmissionError('generation_request_invalid', '作品或请求编号无效。') from None
    params = {key: values.get(key) for key in ('user_input', 'style_id', 'structure_book_id',
        'enable_polish', 'skip_polish', 'chapter_id', 'volume_idx', 'chapter_idx')}
    extra = values.get('params') or {}
    protected = {'submission', 'request_id', 'expected_content_text', 'celery_task_id', 'cancelled_at',
                 'volume_candidate', 'batch_request_id', 'pipeline_id'}
    if not isinstance(extra, dict) or protected.intersection(extra):
        raise GenerationSubmissionError('generation_request_invalid', '生成参数包含保留的请求或任务状态字段。')
    for key, value in extra.items():
        if key in params and params[key] is not None and params[key] != value:
            raise GenerationSubmissionError('generation_request_invalid', '生成参数与请求中的章节或设置不一致。')
        params[key] = value
    cid = params.get('chapter_id')
    if cid is not None:
        try:
            params['chapter_id'] = str(UUID(str(cid)))
        except (ValueError, TypeError):
            raise GenerationSubmissionError('generation_request_invalid', '章节编号无效。') from None
    task_type = values.get('task_type') or ('chapter' if params.get('chapter_id') else 'outline_book')
    if task_type not in {'chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference'}:
        raise GenerationSubmissionError('generation_request_invalid', '不支持的生成任务类型。')
    for key in ('volume_idx', 'chapter_idx'):
        value = params.get(key)
        if value is not None and (type(value) is not int or not 0 < value <= 2147483647):
            raise GenerationSubmissionError('generation_request_invalid', '卷号和章号必须是正整数。')
    expected = values.get('expected_content_text')
    try:
        fingerprint = _hash({'version': 1, 'project_id': str(pid), 'task_type': task_type,
                             'params': params, 'expected_content_text': expected})
    except (ValueError, TypeError):
        raise GenerationSubmissionError('generation_request_invalid', '生成参数不是有效的JSON。') from None
    return SubmissionIntent(rid, pid, task_type, params, expected, fingerprint)


def match_submission(task, intent):
    params = task.params_json if isinstance(task.params_json, dict) else {}
    submission = params.get('submission')
    if (task.project_id != intent.project_id or task.task_type != intent.task_type
            or not isinstance(submission, dict) or submission.get('version') != 1
            or submission.get('fingerprint') != intent.fingerprint):
        raise GenerationSubmissionError('generation_request_conflict', '该请求编号已用于另一份生成请求，请先核对原任务。')
    return task


def _outline_incomplete(missing_layers, *, readiness=None):
    labels = '、'.join(LAYER_LABELS.get(layer, layer) for layer in missing_layers)
    raise GenerationSubmissionError('outline_chain_incomplete',
        readiness.block_message() if readiness is not None else f'缺少：{labels}',
        missing_layers=missing_layers, **({'readiness': readiness.to_dict()} if readiness is not None else {}))


async def validate_submission_outlines(db, intent):
    if intent.task_type == 'outline_volume':
        outlines = (await db.execute(select(Outline).where(Outline.project_id == intent.project_id,
            Outline.level == 'book').execution_options(autoflush=False))).scalars().all()
        if not any(has_meaningful_outline_content(row.content_json) for row in outlines):
            _outline_incomplete(['book'])
    elif intent.task_type in {'chapter', 'outline_chapter'}:
        cid = intent.params.get('chapter_id')
        if not cid:
            raise GenerationSubmissionError('outline_target_required', '请先选择要生成的章节。')
        readiness = await build_outline_readiness_report(db, project_id=intent.project_id, chapter_id=cid)
        missing = list(readiness.missing_layers)
        if intent.task_type == 'outline_chapter':
            missing = [layer for layer in missing if layer in {'book', 'volume'}]
        if missing or (intent.task_type == 'chapter' and not readiness.ready):
            _outline_incomplete(missing, readiness=readiness)


def _resolve_params(project, params):
    result = dict(params)
    settings = project.settings_json if isinstance(project.settings_json, dict) else {}
    if not result.get('style_id'):
        style_ref = settings.get('style_reference')
        sid = settings.get('style_profile_id') or (style_ref.get('profile_id') if isinstance(style_ref, dict) else None)
        result['style_id'] = sid if isinstance(sid, str) and sid.strip() else None
    if not result.get('structure_book_id'):
        structure = settings.get('plot_structure')
        sid = structure.get('structure_book_id') if isinstance(structure, dict) else None
        result['structure_book_id'] = sid if isinstance(sid, str) and sid.strip() else None
    return result


async def prepare_generation_submission(db, intent):
    """Prepare or read the original request. The caller commits before publish."""
    existing = await db.get(GenerationTask, intent.request_id, populate_existing=True)
    if existing is not None:
        return match_submission(existing, intent)
    project = await db.scalar(select(Project).where(Project.id == intent.project_id, Project.deleted_at.is_(None))
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    if project is None:
        raise GenerationSubmissionError('generation_project_unavailable', '作品不存在或已删除。')
    # Another request for this project may have committed while we waited.
    existing = await db.get(GenerationTask, intent.request_id, populate_existing=True)
    if existing is not None:
        return match_submission(existing, intent)
    cid = intent.params.get('chapter_id')
    source = None
    volume_source = None
    if intent.task_type in {'chapter', 'outline_chapter'}:
        if not cid:
            raise GenerationSubmissionError('outline_target_required', '请先选择要生成的章节。')
        target = (await db.execute(select(Chapter, Volume.volume_idx).join(Volume, Volume.id == Chapter.volume_id)
            .where(Chapter.id == UUID(cid), Volume.project_id == intent.project_id)
            .with_for_update(of=Chapter).execution_options(populate_existing=True, autoflush=False))).one_or_none()
        if target is None:
            raise GenerationSubmissionError('generation_chapter_unavailable', '章节不存在或不属于本书。')
        chapter, volume_idx = target
        if any(intent.params.get(key) is not None and intent.params[key] != actual
               for key, actual in (('volume_idx', volume_idx), ('chapter_idx', chapter.chapter_idx))):
            raise GenerationSubmissionError('generation_request_invalid',
                '请求中的卷号或章号与当前章节不一致，请刷新目录后重新选择。')
        if intent.task_type == 'chapter':
            if intent.expected_content_text is None:
                raise GenerationSubmissionError('chapter_content_precondition_required', '请先读取完整保存稿，再提交正文生成。')
            if (chapter.content_text or '') != intent.expected_content_text:
                raise GenerationSubmissionError('chapter_edit_conflict', '正文已在其他位置更新，请先核对保存稿再生成。')
            source = chapter_submission_source(chapter)
    if intent.task_type == 'outline_volume':
        idx = intent.params.get('volume_idx')
        if idx is None:
            raise GenerationSubmissionError('outline_target_required', '请先选择要生成大纲的分卷。')
        volumes = list((await db.execute(select(Volume).where(Volume.project_id == intent.project_id, Volume.volume_idx == idx)
            .limit(2).execution_options(populate_existing=True, autoflush=False))).scalars())
        if not volumes:
            raise GenerationSubmissionError('generation_volume_unavailable', '分卷不存在或不属于本书。')
        if len(volumes) != 1:
            raise GenerationSubmissionError('generation_volume_ambiguous', '卷号重复，请先核对分卷后再生成。')
        volume = volumes[0]
        from app.services.volume_outline_writes import volume_submission_source
        volume_source = volume_submission_source(volume)
    await validate_submission_outlines(db, intent)
    params = _resolve_params(project, intent.params)
    params.update(submission={'version': 1, 'fingerprint': intent.fingerprint, 'chapter_source': source},
                  celery_task_id=str(intent.request_id))
    if volume_source is not None:
        params['submission']['volume_source'] = volume_source
    task = GenerationTask(id=intent.request_id, project_id=intent.project_id, task_type=intent.task_type,
                          status='pending', params_json=params)
    db.add(task)
    await db.flush()
    return task


async def submit_generation(db, values):
    """Commit once, then publish the fixed task for all async HTTP adapters."""
    intent = None
    try:
        intent = submission_intent(values)
        async with asyncio.timeout(30):
            try:
                task = await prepare_generation_submission(db, intent)
                receipt = {**generation_task_identity(task), 'status': effective_generation_task_status(task)}
                await db.commit()
            except IntegrityError:
                # The primary key also serializes the same request ID used
                # concurrently for different projects.
                await db.rollback()
                existing = await db.get(GenerationTask, intent.request_id, populate_existing=True)
                if existing is None:
                    raise
                task = match_submission(existing, intent)
                receipt = {**generation_task_identity(task), 'status': effective_generation_task_status(task)}
                await db.commit()
    except GenerationSubmissionError:
        await db.rollback()
        raise
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.warning('Async submission commit unconfirmed (%s)', type(exc).__name__)
        raise GenerationSubmissionError('generation_submission_unconfirmed',
            '未取得任务提交确认，请保留原请求编号并核对任务，勿重新创建。',
            task_id=str(intent.request_id) if intent is not None else values.get('request_id')) from None

    if receipt['status'] != 'pending':
        return receipt
    from app.tasks.knowledge_tasks import run_async_generation
    task_id = receipt['task_id']
    try:
        # A lost broker acknowledgement never makes the task failed. Retry
        # may publish this ID again; only a pending CAS can grant execution.
        async with asyncio.timeout(10):
            await asyncio.to_thread(run_async_generation.apply_async,
                args=[task_id], task_id=task_id, retry=False)
    except Exception as exc:
        logger.warning('Async submission delivery unconfirmed (%s)', type(exc).__name__)
        raise GenerationSubmissionError('generation_submission_unconfirmed',
            '任务派发未获确认，请核对或重试同一请求；原任务和章稿基准已保留。',
            task_id=task_id) from None
    # Never write the pre-publication ORM row back over worker/cancel changes.
    return receipt


async def claim_generation_submission(db, task_id):
    """Only the worker that commits this transition may invoke generation."""
    row = (await db.execute(update(GenerationTask).where(GenerationTask.id == UUID(str(task_id)),
        GenerationTask.status == 'pending', GenerationTask.params_json['cancelled_at'].as_string().is_(None))
        .values(status='running').returning(GenerationTask.id).execution_options(synchronize_session=False))).first()
    await db.commit()
    return row is not None


@dataclass
class TaskResultOwner:
    task_id: UUID
    active: bool = True


_result_owner = ContextVar('generation_task_result_owner', default=None)


@contextmanager
def generation_task_results(task_id):
    """Keep every task type's late result from erasing a committed cancel."""
    owner = TaskResultOwner(UUID(str(task_id)))
    token = _result_owner.set(owner)
    try:
        yield
    finally:
        owner.active = False
        _result_owner.reset(token)


def _protect_task_results(session, _flush_context, _instances):
    owner = _result_owner.get()
    if owner is None:
        return
    from app.services.generation_task_execution import protect_task_flush
    protect_task_flush(session)
    from app.services.batch_execution import current_batch_execution, protect_batch_write
    if current_batch_execution() is not None and (session.dirty or session.new or session.deleted):
        if not owner.active:
            raise StaleChapterWrite('任务执行已结束，已停止迟到写入')
        protect_batch_write(session.connection(), owner.task_id)
    tasks = [row for row in session.dirty | session.new if isinstance(row, GenerationTask)]
    if not tasks:
        return
    if not owner.active or any(row.id != owner.task_id for row in tasks):
        raise StaleChapterWrite('任务执行已结束或来源不一致，已停止迟到结果写入')
    current = session.connection().execute(select(GenerationTask.status, GenerationTask.params_json)
        .where(GenerationTask.id == owner.task_id).with_for_update()).first()
    params = current.params_json if current is not None and isinstance(current.params_json, dict) else {}
    if current is None or current.status == 'cancelled' or params.get('cancelled_at'):
        raise StaleChapterWrite('生成任务已取消或删除，已停止结果写入')


event.listen(Session, 'before_flush', _protect_task_results)
