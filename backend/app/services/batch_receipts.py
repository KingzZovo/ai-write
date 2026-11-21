"""Small save receipts written in the canonical manuscript transaction."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update

from app.models.generation_batch import GenerationBatchItem
from app.services.batch_execution import protect_batch_write
from app.services.manuscript_receipts import manuscript_source
from app.services.production.control import content_hash


def has_batch_save(connection, task_id):
    return connection.scalar(select(GenerationBatchItem.saved_receipt_json).where(
        GenerationBatchItem.task_id == UUID(str(task_id)))) is not None


def record_batch_save(connection, chapter, task_id):
    owner = protect_batch_write(connection, task_id)
    if owner is None or owner.terminal:
        return
    connection.execute(update(GenerationBatchItem).where(
        GenerationBatchItem.batch_id == UUID(owner.request_id),
        GenerationBatchItem.task_id == UUID(owner.task_id),
        GenerationBatchItem.chapter_id == chapter.id,
    ).values(saved_receipt_json={
        'version': 1, 'worker_id': str(owner.worker_id), 'task_id': owner.task_id,
        'source': manuscript_source(chapter), 'body_hash': content_hash(chapter.content_text),
        'status': chapter.status, 'word_count': len(chapter.content_text or ''),
    }))


def receipt_matches(item, chapter):
    receipt = item.saved_receipt_json
    attempts = item.attempts_json
    return (chapter is not None and isinstance(receipt, dict) and type(receipt.get('version')) is int and receipt['version'] == 1
        and receipt.get('task_id') == str(item.task_id)
        and isinstance(attempts, list) and len(attempts) == item.attempt_count and bool(attempts)
        and isinstance(attempts[-1], dict) and receipt.get('worker_id') == attempts[-1].get('worker_id')
        and receipt.get('source') == manuscript_source(chapter)
        and bool((chapter.content_text or '').strip())
        and receipt.get('body_hash') == content_hash(chapter.content_text)
        and receipt.get('status') == chapter.status
        and receipt.get('word_count') == len(chapter.content_text))
