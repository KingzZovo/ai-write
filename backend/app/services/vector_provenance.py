"""Validate derived memory against current PostgreSQL chapter sources."""
import hashlib
import logging
from uuid import UUID

from sqlalchemy import select

from app.models.project import Chapter, Volume
from app.services.summary_provenance import summary_is_current
from app.services.qdrant_store import valid_global_idx

logger = logging.getLogger(__name__)
_RECEIPT_FIELDS = ('project_id', 'chapter_id', 'volume_id', 'chapter_idx', 'global_idx', 'summary_source_hash', 'summary')


def _chunk_vector_matches(payload, cached) -> bool:
    """这一条 chunk 向量还算"当前记忆"吗?

    硬前提是章节正文的 sha256 没变 —— 改过稿的旧点必须失败(引文可能已不存在)。
    在此之上,允许**切块算法变更**带来的边界重排:旧点只要仍是当前正文里逐字存在
    的一段,它就是有效引文,不能因为 chunk_seq/边界对不上就整点丢弃。

    为什么要这层容错(2026-11-17):切块从"按字数凑段落"改为结构感知(对白不切断、
    场景分隔优先)之后,所有既有 point 的 seq 与文本都可能不匹配,而
    ``filter_current_memory`` 是【原文回读】的唯一把关点 —— 不做容错就等于让长书的
    原文召回静默归零(与"换嵌入模型导致召回 0 点"是同一类事故)。
    """
    digest, parts, text = cached
    if payload.get('source_content_hash') != digest:
        return False
    seq = payload.get('chunk_seq')
    if type(seq) is int and 0 <= seq < len(parts) and payload.get('text') == parts[seq]:
        return True
    payload_text = payload.get('text')
    return isinstance(payload_text, str) and bool(payload_text) and payload_text in text


def summary_receipt(payload: dict) -> dict:
    return {key: payload.get(key) for key in _RECEIPT_FIELDS}


def _source_id(payload):
    try:
        return UUID(str(payload.get('chapter_id')))
    except (ValueError, TypeError, AttributeError):
        return None


def _sources(hit):
    payload = hit.get('payload') or {}
    if hit.get('tier') != 'compacted':
        return [payload]
    sources = payload.get('sources')
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 20
            or type(payload.get('source_count')) is not int
            or len(sources) != payload['source_count']
            or not all(isinstance(source, dict) for source in sources)):
        return []
    ids = [_source_id(source) for source in sources]
    indices = [source.get('global_idx') for source in sources]
    if (None in ids or len(set(ids)) != len(ids)
            or not all(valid_global_idx(idx) for idx in indices)
            or not valid_global_idx(payload.get('source_global_idx_min'))
            or not valid_global_idx(payload.get('source_global_idx_max'))
            or payload.get('source_global_idx_min') != min(indices)
            or payload.get('source_global_idx_max') != max(indices)):
        return []
    return sources


async def filter_current_memory(db, project_id, hits, *, before_global_idx, chunks=False):
    """Bounded, fail-closed read validation; None is for compaction, not writing.

    Does not flush caller changes or trust its ORM identity cache. A completed
    chapter and its exact source must still exist. Legacy payloads without
    provenance need a source-backed rebuild; they are not guessed current.
    """
    if not hits or (before_global_idx is not None and not valid_global_idx(before_global_idx)):
        return []
    try:
        entries = [(hit, _sources(hit)) for hit in hits
                   if str((hit.get('payload') or {}).get('project_id')) == str(project_id)]
        ids = {_source_id(source) for _, sources in entries for source in sources}
        ids.discard(None)
        if not ids or len(ids) > 200:
            return []
        rows = (await db.execute(
            select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx, Chapter.global_idx,
                   Chapter.status, Chapter.title, Chapter.content_text, Chapter.summary,
                   Chapter.summary_source_hash)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == str(project_id), Chapter.id.in_(ids))
            .execution_options(autoflush=False)
        )).all()
        by_id = {row.id: row for row in rows}
        chunk_cache = {}

        def matches(payload):
            row = by_id.get(_source_id(payload))
            if (row is None or row.status != 'completed'
                    or str(payload.get('project_id')) != str(project_id)
                    or str(payload.get('volume_id')) != str(row.volume_id)
                    or type(payload.get('chapter_idx')) is not int
                    or payload['chapter_idx'] != row.chapter_idx
                    or not valid_global_idx(payload.get('global_idx'))
                    or payload['global_idx'] != row.global_idx
                    or (before_global_idx is not None and row.global_idx >= before_global_idx)):
                return False
            if chunks:
                from app.services.chapter_chunker import chunk_chapter_text
                if row.id not in chunk_cache:
                    text = row.content_text or ''
                    chunk_cache[row.id] = (
                        hashlib.sha256(text.encode()).hexdigest(),
                        chunk_chapter_text(text),
                        text,
                    )
                return _chunk_vector_matches(payload, chunk_cache[row.id])
            return (summary_is_current(row)
                    and payload.get('summary_source_hash') == row.summary_source_hash
                    and payload.get('summary') == row.summary)

        return [hit for hit, sources in entries if sources and all(matches(source) for source in sources)]
    except Exception as exc:
        logger.warning('Memory source verification unavailable: %s', type(exc).__name__)
        return []
