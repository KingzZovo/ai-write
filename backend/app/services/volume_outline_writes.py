"""Author volume outlines: explicit source, durable receipt, complete history.

Prose and independently authored chapter plans stay intact. Structured edits
may sync proven untouched stubs; author text owns later prose revisions.
"""
from copy import deepcopy
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.models.generation_task import GenerationTask
from app.models.outline_write import VolumeOutlineWrite
from app.models.project import Chapter, Outline, Project, Volume
from app.services.chapter_order import lock_chapter_structure
from app.services.outline_authority import record_text_edit
from app.services.outline_order import positive_index, volume_condition


class VolumeOutlineError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status
        self.detail = {'code': code, 'message': message}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def volume_submission_source(volume):
    return {'version': 1, 'project_id': str(volume.project_id), 'volume_id': str(volume.id),
            'volume_idx': volume.volume_idx}


async def require_unique_volume_position(db, volume):
    if not positive_index(volume.volume_idx):
        raise VolumeOutlineError('volume_outline_invalid', '当前卷号无效，请先核对分卷顺序。', 422)
    owners = list((await db.execute(select(Volume.id).where(
        Volume.project_id == volume.project_id, Volume.volume_idx == volume.volume_idx)
        .limit(2).execution_options(autoflush=False))).scalars())
    if owners != [volume.id]:
        raise VolumeOutlineError('volume_candidate_changed', '当前卷号重复或已变化，请先核对分卷顺序。')


async def require_volume_submission_source(db, project_id, source):
    if (not isinstance(source, dict) or set(source) != {'version', 'project_id', 'volume_id', 'volume_idx'}
            or type(source.get('version')) is not int or source['version'] != 1
            or source.get('project_id') != str(project_id) or type(source.get('volume_idx')) is not int
            or source['volume_idx'] < 1):
        raise VolumeOutlineError('volume_candidate_unverified', '任务缺少提交时的分卷来源，请核对后重新提交。')
    try:
        vid = UUID(source['volume_id'])
    except (ValueError, TypeError, AttributeError):
        raise VolumeOutlineError('volume_candidate_unverified', '任务的分卷来源无效，请核对后重新提交。') from None
    volume = await db.scalar(select(Volume).join(Project, Project.id == Volume.project_id)
        .where(Volume.id == vid, Volume.project_id == UUID(str(project_id)), Project.deleted_at.is_(None))
        .execution_options(populate_existing=True, autoflush=False))
    if volume is None or volume_submission_source(volume) != source:
        raise VolumeOutlineError('volume_candidate_changed', '分卷在提交后已删除或调整，请核对当前分卷再操作。')
    await require_unique_volume_position(db, volume)
    return volume


async def require_volume_task_source(db, project_id, params):
    submission = params.get('submission') if isinstance(params, dict) else None
    source = submission.get('volume_source') if isinstance(submission, dict) else None
    volume = await require_volume_submission_source(db, project_id, source)
    if type(params.get('volume_idx')) is not int or params['volume_idx'] != volume.volume_idx:
        raise VolumeOutlineError('volume_candidate_unverified', '任务卷号与提交时的分卷来源不一致，请重新核对任务。')
    return volume


def outline_snapshot(outline):
    return {'id': str(outline.id), 'parent_id': str(outline.parent_id) if outline.parent_id else None,
            'content_json': deepcopy(outline.content_json or {}), 'version': outline.version,
            'is_confirmed': outline.is_confirmed}


async def _read(db, project_id, volume_id, *, lock=False):
    pid, vid = UUID(str(project_id)), UUID(str(volume_id))
    project = await db.scalar(select(Project.id).where(Project.id == pid, Project.deleted_at.is_(None))
        .execution_options(autoflush=False))
    if project is None:
        raise VolumeOutlineError('volume_outline_unavailable', '作品不存在或已删除。', 404)
    query = select(Volume).where(Volume.id == vid, Volume.project_id == pid)
    if lock: query = query.with_for_update()
    volume = await db.scalar(query.execution_options(populate_existing=True, autoflush=False))
    if volume is None:
        raise VolumeOutlineError('volume_outline_unavailable', '分卷不存在或不属于本书。', 404)
    await require_unique_volume_position(db, volume)
    query = select(Outline).where(Outline.project_id == pid, Outline.level == 'volume',
        volume_condition(vid, volume.volume_idx)).order_by(
            Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc())
    if lock: query = query.with_for_update()
    outlines = list((await db.execute(query.execution_options(populate_existing=True, autoflush=False))).scalars())
    snapshots = [outline_snapshot(outline) for outline in outlines]
    source = {'schema': 1, 'project_id': str(pid), 'volume_id': str(vid), 'volume_idx': volume.volume_idx,
        'title': volume.title, 'outline': snapshots[0] if snapshots else None,
        'selection': [{'id': snapshot['id'], 'hash': digest(snapshot)} for snapshot in snapshots]}
    return source, volume, outlines


async def read_volume_outline(db, project_id, volume_id):
    return (await _read(db, project_id, volume_id))[0]


async def read_volume_candidate(db, project_id, task_id):
    task = await db.scalar(select(GenerationTask).where(GenerationTask.id == UUID(str(task_id)),
        GenerationTask.project_id == UUID(str(project_id)))
        .execution_options(populate_existing=True, autoflush=False))
    if task is None or task.task_type != 'outline_volume':
        raise VolumeOutlineError('volume_candidate_unavailable', '本书中找不到这个卷纲任务。', 404)
    params = task.params_json if isinstance(task.params_json, dict) else {}
    candidate = params.get('volume_candidate')
    submission = params.get('submission')
    source = submission.get('volume_source') if isinstance(submission, dict) else None
    if (task.status != 'completed' or params.get('cancelled_at') or not isinstance(candidate, dict)
            or digest(candidate.get('target')) != digest(source) or type(candidate.get('version')) is not int or candidate['version'] != 1
            or not isinstance(task.result_text, str) or not task.result_text.strip()
            or candidate.get('text_hash') != text_digest(task.result_text)):
        raise VolumeOutlineError('volume_candidate_unverified',
            '候选尚未完整交付或缺少可核对的分卷来源，请先保留全文并核对任务。')
    volume = await require_volume_task_source(db, project_id, params)
    return {'task_id': str(task.id), 'volume_id': str(volume.id), 'volume_idx': volume.volume_idx,
            'text': task.result_text}


def write_intent(project_id, volume_id, request_id, expected_source, text, candidate=None, content_json=None):
    if request_id is None or expected_source is None:
        raise VolumeOutlineError('volume_outline_precondition_required', '请先读取完整卷纲，并保留本次保存请求编号。', 428)
    if (text is None) == (content_json is None):
        raise VolumeOutlineError('volume_outline_invalid', '请选择文字卷纲或完整结构卷纲进行保存。', 422)
    if content_json is None and (not isinstance(text, str) or not text.strip()):
        raise VolumeOutlineError('volume_outline_invalid', '正式卷纲不能为空，当前编辑稿已保留。', 422)
    if content_json is not None and (not isinstance(content_json, dict) or not content_json or candidate is not None):
        raise VolumeOutlineError('volume_outline_invalid', '结构卷纲必须是完整对象；候选请先在文字编辑稿中核对。', 422)
    try:
        pid, vid, rid = UUID(str(project_id)), UUID(str(volume_id)), UUID(str(request_id))
        payload_hash = digest({'version': 1, 'project_id': str(pid), 'volume_id': str(vid),
            'source': expected_source, 'text': text, 'candidate': candidate, 'content_json': content_json})
    except (ValueError, TypeError):
        raise VolumeOutlineError('volume_outline_invalid', '卷纲保存请求无效，请保留编辑稿并重新核对。', 422) from None
    return pid, vid, rid, payload_hash


def _match_receipt(row, pid, vid, payload_hash):
    if row.project_id != pid or row.volume_id != vid or row.request_hash != payload_hash:
        raise VolumeOutlineError('volume_outline_request_conflict', '该编号已用于另一份卷纲保存请求，请先核对原回执。')
    return deepcopy(row.result_json)


async def prepare_volume_outline_write(db, project_id, volume_id, *, request_id, expected_source, text=None, candidate=None, content_json=None):
    pid, vid, rid, payload_hash = write_intent(project_id, volume_id, request_id, expected_source, text, candidate, content_json)
    existing = await db.get(VolumeOutlineWrite, rid, populate_existing=True)
    if existing is not None:
        return _match_receipt(existing, pid, vid, payload_hash)
    await lock_chapter_structure(db, pid)
    existing = await db.get(VolumeOutlineWrite, rid, populate_existing=True)
    if existing is not None:
        return _match_receipt(existing, pid, vid, payload_hash)
    before, volume, rows = await _read(db, pid, vid, lock=True)
    if digest(before) != digest(expected_source):
        raise VolumeOutlineError('volume_outline_conflict', '卷纲或分卷已变化，请对照当前保存稿后再保存；编辑稿已保留。')
    if candidate is not None:
        if not isinstance(candidate, dict) or set(candidate) != {'task_id', 'text'}:
            raise VolumeOutlineError('volume_outline_invalid', '候选来源不完整，请重新核对任务。', 422)
        try:
            source_task = await read_volume_candidate(db, pid, candidate['task_id'])
        except (ValueError, TypeError, AttributeError) as exc:
            if isinstance(exc, VolumeOutlineError): raise
            raise VolumeOutlineError('volume_outline_invalid', '候选任务编号无效。', 422) from None
        if source_task['volume_id'] != str(vid) or source_task['text'] != candidate['text']:
            raise VolumeOutlineError('volume_candidate_changed', '候选文本或所属分卷与核对时不同，未采用候选。')
    old = rows[0] if rows else None
    content = deepcopy(content_json) if content_json is not None else deepcopy(old.content_json or {}) if old else {}
    if content_json is not None and (content.get('volume_id') not in (None, str(vid))
            or content.get('volume_idx') not in (None, volume.volume_idx)):
        raise VolumeOutlineError('volume_outline_invalid', '结构卷纲不能改变所属分卷，请核对卷号。', 422)
    content.update(volume_id=str(vid), volume_idx=volume.volume_idx)
    if content_json is None:
        content['raw_text'] = text
    content = record_text_edit(old.content_json if old else {}, content)
    # A new author decision cannot inherit a model verdict about an older draft.
    content.pop('_quality_status', None)
    content.pop('_consistency_report', None)
    from app.services.outline_authority import outline_for_generation
    from app.services.outline_readiness import has_meaningful_outline_content
    if not has_meaningful_outline_content(outline_for_generation(content)):
        raise VolumeOutlineError('volume_outline_invalid', '正式卷纲缺少可用于写作的内容，编辑稿已保留。', 422)
    from app.services.volume_outline_sync import SyncResult, sync_untouched_stubs
    sync = SyncResult()
    if content_json is not None:
        try:
            sync = await sync_untouched_stubs(db, pid, old.content_json if old else {}, content)
        except ValueError as exc:
            raise VolumeOutlineError('volume_outline_invalid', str(exc), 422) from None
    prior_competitors = [outline_snapshot(row) for row in rows[1:]]
    changed = (old is None or old.content_json != content or old.is_confirmed != 1
               or any(row.is_confirmed != 0 for row in rows[1:]))
    if changed:
        versions = [row.version for row in rows]
        if any(type(version) is not int or version < 1 for version in versions) or max(versions, default=0) >= 2147483647:
            raise VolumeOutlineError('volume_outline_invalid', '现有卷纲版本无效，请先核对历史记录。', 422)
        if old is None:
            book = await db.scalar(select(Outline.id).where(Outline.project_id == pid, Outline.level == 'book')
                .order_by(Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc())
                .limit(1).execution_options(autoflush=False))
            old = Outline(id=uuid4(), project_id=pid, level='volume', parent_id=book)
            db.add(old)
        old.content_json, old.version, old.is_confirmed = content, max(versions, default=0) + 1, 1
        # Explicit adoption establishes the active outline, while older rows
        # remain available as historical material and cannot win selection.
        for row in rows:
            if row.id != old.id: row.is_confirmed = 0
        await db.flush()
    after = await read_volume_outline(db, pid, vid)
    preserved = await db.scalar(select(func.count()).select_from(Chapter).where(Chapter.volume_id == vid)
        .execution_options(autoflush=False))
    result = {'status': 'saved', 'request_id': str(rid), 'source': after, 'changed': changed,
        'preserved_chapters': preserved, 'candidate_task_id': candidate['task_id'] if candidate else None,
        'submitted_json': deepcopy(content_json), 'synced_chapters': sync.synced,
        'unmaterialized_chapters': sync.unmaterialized}
    db.add(VolumeOutlineWrite(id=rid, project_id=pid, volume_id=vid, request_hash=payload_hash,
        before_json={**before, 'competing_outlines': prior_competitors}, result_json=deepcopy(result)))
    await db.flush()
    return result
