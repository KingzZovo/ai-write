"""Independent chapter delivery proof in the manuscript's own transaction."""
from uuid import UUID

from sqlalchemy import select, update

from app.models.generation_task import GenerationTask
from app.services.generation_task_execution import TaskExecutionLost, protect_task_write
from app.services.generation_submissions import valid_chapter_submission_source
from app.services.manuscript_receipts import manuscript_source
from app.services.production.control import content_hash


def has_task_save(connection, task_id):
    return connection.scalar(select(GenerationTask.saved_manuscript_json).where(
        GenerationTask.id == UUID(str(task_id)))) is not None


def record_task_save(connection, chapter, task_id):
    owner = protect_task_write(connection)
    if owner is None:
        return
    if owner.task_id != UUID(str(task_id)):
        raise TaskExecutionLost('正文保存证明不属于本次执行。')
    row = connection.execute(select(GenerationTask.task_type, GenerationTask.execution_params_json)
        .where(GenerationTask.id == owner.task_id)).first()
    params = row.execution_params_json if row is not None and isinstance(row.execution_params_json, dict) else {}
    submission = params.get('submission') if isinstance(params.get('submission'), dict) else {}
    if (row is None or row.task_type != 'chapter' or params.get('chapter_id') != str(chapter.id)
            or not valid_chapter_submission_source(submission.get('chapter_source'), chapter.id)):
        raise TaskExecutionLost('无法核对原始任务与正文归属，未写入保存证明。')
    connection.execute(update(GenerationTask).where(GenerationTask.id == owner.task_id).values(
        saved_manuscript_json={'version': 1, 'execution_id': str(owner.execution_id),
            'task_id': str(owner.task_id), 'submission_fingerprint': submission['fingerprint'],
            'source': manuscript_source(chapter), 'body_hash': content_hash(chapter.content_text),
            'status': chapter.status, 'word_count': len(chapter.content_text or '')},
        updated_at=GenerationTask.updated_at,
    ))
