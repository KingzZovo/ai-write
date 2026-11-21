"""Fixed legacy-run requests and their original chapter material.

An accepted request is read-only on POST retry. Only an explicit resume may
execute again, after checking the original source and rewrite budget. The
bounded receipt survives chapter/run deletion until the book is purged.
"""
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from app.models.generation_run import GenerationRun, GenerationRunSubmission
from app.models.project import Chapter, Project, Volume
from app.services.generation_execution import GenerationRunUnavailable
from app.services.generation_submissions import (
    chapter_submission_source, valid_chapter_submission_source,
)
from app.services.volume_outline_writes import volume_submission_source


class RunSubmissionError(ValueError):
    def __init__(self, code, message, status=409, **context):
        super().__init__(message)
        self.status = status
        self.detail = {'code': code, 'message': message, **context}


@dataclass(frozen=True)
class RunSubmissionIntent:
    request_id: UUID
    project_id: UUID
    chapter_id: UUID
    max_rewrite_count: int
    expected_content_text: str
    fingerprint: str


def run_submission_intent(*, request_id, project_id, chapter_id,
                          max_rewrite_count, expected_content_text):
    if request_id is None:
        raise RunSubmissionError('generation_request_id_required',
            '请为本次生成保留固定请求编号，再提交任务。', 428)
    if expected_content_text is None:
        raise RunSubmissionError('chapter_content_precondition_required',
            '请先读取完整保存稿，再提交正文生成。', 428)
    if (not isinstance(expected_content_text, str) or type(max_rewrite_count) is not int
            or not 0 <= max_rewrite_count <= 8):
        raise RunSubmissionError('generation_request_invalid', '原稿或改写次数无效。', 422)
    try:
        rid, pid, cid = (UUID(str(value)) for value in (request_id, project_id, chapter_id))
    except (ValueError, TypeError):
        raise RunSubmissionError('generation_request_invalid', '请求、作品或章节编号无效。', 422) from None
    try:
        fingerprint = hashlib.sha256(json.dumps({
            'version': 1, 'operation': 'generation_run', 'project_id': str(pid),
            'chapter_id': str(cid), 'max_rewrite_count': max_rewrite_count,
            'expected_content_text': expected_content_text,
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    except (ValueError, TypeError):
        raise RunSubmissionError('generation_request_invalid', '原稿不是有效的Unicode文本。', 422) from None
    return RunSubmissionIntent(rid, pid, cid, max_rewrite_count, expected_content_text, fingerprint)


def make_run_submission(intent, chapter, volume):
    return GenerationRunSubmission(
        id=intent.request_id, project_id=intent.project_id, chapter_id=intent.chapter_id,
        version=1, request_hash=intent.fingerprint, max_rewrite_count=intent.max_rewrite_count,
        source_json={'chapter': chapter_submission_source(chapter),
                     'volume': volume_submission_source(volume)},
    )


def _unverified():
    return RunSubmissionError('generation_run_source_unverified',
        '任务缺少有效的原始请求或章稿来源证明；请先查看已保存候选，再按当前保存稿提交新请求。')


def _validate_receipt(receipt):
    source = receipt.source_json
    if (type(receipt.version) is not int or receipt.version != 1
            or type(receipt.max_rewrite_count) is not int or not 0 <= receipt.max_rewrite_count <= 8
            or not isinstance(receipt.request_hash, str) or len(receipt.request_hash) != 64
            or any(char not in '0123456789abcdef' for char in receipt.request_hash)
            or not isinstance(source, dict) or set(source) != {'chapter', 'volume'}
            or not valid_chapter_submission_source(source.get('chapter'), receipt.chapter_id)):
        raise _unverified()
    volume = source['volume']
    if (not isinstance(volume, dict) or set(volume) != {'version', 'project_id', 'volume_id', 'volume_idx'}
            or type(volume.get('version')) is not int or volume['version'] != 1
            or volume.get('project_id') != str(receipt.project_id)
            or type(volume.get('volume_idx')) is not int or not 0 < volume['volume_idx'] <= 2147483647):
        raise _unverified()
    try:
        if str(UUID(volume['volume_id'])) != volume['volume_id']:
            raise _unverified()
    except (ValueError, TypeError, AttributeError):
        raise _unverified() from None


def _require_run_identity(receipt, run):
    _validate_receipt(receipt)
    if (run.id != receipt.id or run.project_id != receipt.project_id
            or run.chapter_id != receipt.chapter_id or run.max_rewrite_count != receipt.max_rewrite_count
            or type(run.rewrite_count) is not int or not 0 <= run.rewrite_count <= receipt.max_rewrite_count):
        raise _unverified()


def deleted_run_error(run_id):
    return RunSubmissionError('generation_run_gone',
        '原请求已受理，但运行记录或来源章节已删除；此编号不能重新启动生成。', 410, run_id=str(run_id))


async def find_submitted_run(db, intent):
    """None means never accepted; any existing identity must not execute."""
    receipt = await db.get(GenerationRunSubmission, intent.request_id, populate_existing=True)
    if receipt is None:
        existing_id = await db.scalar(select(GenerationRun.id).where(GenerationRun.id == intent.request_id)
            .execution_options(autoflush=False))
        if existing_id is None:
            return None
        # READ COMMITTED may see an acceptance commit between these reads.
        # Recheck the receipt before calling this a colliding legacy UUID.
        receipt = await db.get(GenerationRunSubmission, intent.request_id, populate_existing=True)
        if receipt is None:
            raise RunSubmissionError('generation_request_conflict',
                '该编号已有运行记录，但无法证明属于本次请求，请先核对原任务。')
    if (
            receipt.project_id != intent.project_id or receipt.chapter_id != intent.chapter_id
            or receipt.max_rewrite_count != intent.max_rewrite_count or receipt.request_hash != intent.fingerprint):
        raise RunSubmissionError('generation_request_conflict',
            '该请求编号已用于另一份生成请求，请先核对原任务。')
    run = await db.get(GenerationRun, intent.request_id, populate_existing=True)
    _validate_receipt(receipt)
    if run is None:
        raise deleted_run_error(receipt.id)
    _require_run_identity(receipt, run)
    return run


async def _current_material(db, project_id, chapter_id):
    """Short parent-first locks for capture/check; caller owns project execution.

    KEY SHARE protects the project parent without blocking an unrelated
    uncommitted non-key edit in the caller's separate session. Volume and
    chapter SHARE locks keep their source stable until the receipt commits.
    """
    project = await db.scalar(select(Project.id).where(
        Project.id == project_id, Project.deleted_at.is_(None))
        .with_for_update(read=True, key_share=True).execution_options(autoflush=False))
    if project is None:
        raise GenerationRunUnavailable('作品不存在或已删除。')
    volume_id = await db.scalar(select(Chapter.volume_id).where(Chapter.id == chapter_id)
        .execution_options(autoflush=False))
    volume = await db.scalar(select(Volume).where(Volume.id == volume_id, Volume.project_id == project_id)
        .with_for_update(read=True).execution_options(populate_existing=True, autoflush=False))
    if volume is None:
        raise GenerationRunUnavailable('章节不存在或不属于当前作品。')
    chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.volume_id == volume.id)
        .with_for_update(read=True).execution_options(populate_existing=True, autoflush=False))
    if chapter is None:
        raise GenerationRunUnavailable('章节不存在或位置已变化。')
    return chapter, volume


async def prepare_run_submission(db, intent):
    chapter, volume = await _current_material(db, intent.project_id, intent.chapter_id)
    if (chapter.content_text or '') != intent.expected_content_text:
        raise RunSubmissionError('chapter_edit_conflict', '正文已在其他位置更新，请先核对完整保存稿再生成。')
    try:
        receipt = make_run_submission(intent, chapter, volume)
    except (ValueError, TypeError):
        raise _unverified() from None
    _validate_receipt(receipt)
    return receipt


async def require_run_submission_source(db, run):
    receipt = await db.get(GenerationRunSubmission, run.id, populate_existing=True)
    if receipt is None:
        raise _unverified()
    _require_run_identity(receipt, run)
    chapter, volume = await _current_material(db, run.project_id, run.chapter_id)
    try:
        current_source = {'chapter': chapter_submission_source(chapter),
                          'volume': volume_submission_source(volume)}
    except (ValueError, TypeError):
        raise _unverified() from None
    if receipt.source_json != current_source:
        raise RunSubmissionError('generation_run_source_changed',
            '章稿或分卷位置在原请求后发生变化，请先核对已保存候选和当前保存稿，再提交新请求。', run_id=str(run.id))
    # Release source row locks before any model/context work. Physical
    # project execution ownership remains held by generation_execution.
    await db.commit()
