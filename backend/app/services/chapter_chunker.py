"""Tier-4 memory pyramid (自由检索层): chapter full-text chunking.

Splits a chapter's ``content_text`` into paragraph-grouped, sentence-boundary
safe chunks (~500-800 chars), embeds each and upserts them into the
per-project Qdrant shard ``chapter_chunks__<project-hex>`` (naming owned by
services/qdrant_store). ContextPack L3 recalls the top chunks as 【原文回读】
snippets so generation can re-read verbatim prose from anywhere in the book.

Write-side conventions (mirroring chapter_summarizer's summary upsert):
- Fired on chapter persist alongside the summary embedding, failure-tolerant
  by contract — chunking trouble must never break a chapter save.
- Deterministic point ids: md5(f"{volume_id}_{chapter_idx}_{seq}") so a
  re-save overwrites the same points; stale tail chunks of a now-shorter
  chapter are deleted by filter (global_idx == X AND chunk_seq >= new count).
- Gated on ``settings.CHAPTER_CHUNKING_ENABLED``.

``backfill_project_chunks(project_id)`` chunks every existing chapter of a
project (manual invocation for pre-existing books).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Chunk sizing: paragraph groups of roughly this many chars. A paragraph
# longer than CHUNK_MAX_CHARS is split at sentence boundaries.
CHUNK_TARGET_CHARS = 500
CHUNK_MAX_CHARS = 800

_SENTENCE_ENDERS = "。！？…!?"


def _split_long_paragraph(para: str, max_chars: int) -> list[str]:
    """Split one over-long paragraph at sentence boundaries into <=max pieces.

    A single sentence longer than ``max_chars`` is hard-split (degenerate
    input; never produced by normal prose).
    """
    sentences: list[str] = []
    buf: list[str] = []
    for ch in para:
        buf.append(ch)
        if ch in _SENTENCE_ENDERS:
            sentences.append("".join(buf))
            buf = []
    if buf:
        sentences.append("".join(buf))

    pieces: list[str] = []
    cur = ""
    for s in sentences:
        while len(s) > max_chars:  # degenerate: sentence itself too long
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(s[:max_chars])
            s = s[max_chars:]
        if cur and len(cur) + len(s) > max_chars:
            pieces.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def chunk_chapter_text(
    text: str,
    *,
    target_chars: int = CHUNK_TARGET_CHARS,
    max_chars: int = CHUNK_MAX_CHARS,
) -> list[str]:
    """Split chapter text into paragraph-grouped chunks (~target..max chars).

    Deterministic and sentence-boundary safe: paragraphs (newline-separated)
    are grouped until the group reaches ``target_chars``; no group exceeds
    ``max_chars`` (over-long paragraphs are split at sentence enders). A
    short chapter yields a single chunk.
    """
    if not text or not text.strip():
        return []
    paragraphs: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            paragraphs.extend(_split_long_paragraph(para, max_chars))
        else:
            paragraphs.append(para)

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for para in paragraphs:
        joined_len = cur_len + (1 if cur else 0) + len(para)
        if cur and (cur_len >= target_chars or joined_len > max_chars):
            chunks.append("\n".join(cur))
            cur = [para]
            cur_len = len(para)
        else:
            cur.append(para)
            cur_len = joined_len
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def chunk_point_id(volume_id: Any, chapter_idx: int, chunk_seq: int) -> int:
    """Deterministic Qdrant point id for one chunk (re-save overwrites)."""
    key = f"{volume_id}_{chapter_idx}_{chunk_seq}"
    return int(hashlib.md5(key.encode()).hexdigest()[:16], 16)


async def upsert_chapter_chunks(
    *,
    project_id: Any,
    volume_id: Any,
    chapter_idx: int | None,
    global_idx: int | None,
    content_text: str,
) -> int:
    """Chunk + embed + upsert one chapter into the project's chunk shard.

    Returns the number of points upserted (0 on skip/failure). Never raises
    — the chapter persistence path must stay unbreakable, matching
    ``chapter_summarizer.upsert_chapter_summary_embedding``.
    """
    if (
        not content_text
        or project_id is None
        or volume_id is None
        or chapter_idx is None
    ):
        return 0
    try:
        from app.config import settings

        if not getattr(settings, "CHAPTER_CHUNKING_ENABLED", True):
            return 0

        chunks = chunk_chapter_text(content_text)
        if not chunks:
            return 0

        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            FilterSelector,
            MatchValue,
            PointStruct,
            Range,
        )

        from app.services import qdrant_store as qs
        from app.services.feature_extractor import generate_embedding

        points: list[PointStruct] = []
        dim: int | None = None
        for seq, chunk in enumerate(chunks):
            vector = await generate_embedding(chunk)
            if not vector:
                continue
            dim = len(vector)
            points.append(
                PointStruct(
                    id=chunk_point_id(volume_id, int(chapter_idx), seq),
                    vector=vector,
                    payload={
                        "project_id": str(project_id),
                        "volume_id": str(volume_id),
                        "chapter_idx": int(chapter_idx),
                        "global_idx": int(global_idx) if global_idx is not None else None,
                        "chunk_seq": seq,
                        "text": chunk,
                    },
                )
            )
        if not points or dim is None:
            return 0

        client = AsyncQdrantClient(
            host=getattr(settings, "QDRANT_HOST", "localhost"),
            port=getattr(settings, "QDRANT_PORT", 6333),
        )
        try:
            collection = await qs.ensure_chunk_shard(client, project_id, dim)
            await client.upsert(collection_name=collection, points=points)

            # Stale-tail cleanup: a re-saved shorter chapter leaves points
            # with chunk_seq >= new count behind — delete them by filter.
            # global_idx keys the chapter on the book-global axis; rows
            # predating the global_idx backfill fall back to the
            # (volume_id, chapter_idx) key the point ids are built from.
            must: list[Any] = [
                FieldCondition(
                    key="chunk_seq", range=Range(gte=len(chunks))
                ),
            ]
            if global_idx is not None:
                must.append(
                    FieldCondition(
                        key="global_idx",
                        match=MatchValue(value=int(global_idx)),
                    )
                )
            else:
                must.extend(
                    (
                        FieldCondition(
                            key="volume_id",
                            match=MatchValue(value=str(volume_id)),
                        ),
                        FieldCondition(
                            key="chapter_idx",
                            match=MatchValue(value=int(chapter_idx)),
                        ),
                    )
                )
            await client.delete(
                collection_name=collection,
                points_selector=FilterSelector(filter=Filter(must=must)),
            )
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        return len(points)
    except Exception as e:  # noqa: BLE001 — chunking must never break saves
        logger.warning(
            "chapter chunk upsert failed (volume_id=%s idx=%s): %s",
            volume_id, chapter_idx, e,
        )
        return 0


async def backfill_project_chunks(project_id: Any) -> dict[str, Any]:
    """Chunk every existing chapter of a project (manual backfill entry).

    Invocation (orchestrator runbook):
      ``await backfill_project_chunks(project_id)``
    Safe to re-run: point ids are deterministic, so re-runs overwrite.
    """
    from sqlalchemy import select

    from app.db.session import async_session_factory
    from app.models.project import Chapter, Volume

    chunked = 0
    skipped = 0
    async with async_session_factory() as db:
        rows = await db.execute(
            select(
                Chapter.volume_id,
                Chapter.chapter_idx,
                Chapter.global_idx,
                Chapter.content_text,
            )
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == str(project_id))
            .order_by(Chapter.global_idx.asc().nulls_last())
        )
        chapters = rows.all()
    for volume_id, chapter_idx, global_idx, content_text in chapters:
        n = await upsert_chapter_chunks(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
            global_idx=global_idx,
            content_text=content_text or "",
        )
        if n:
            chunked += 1
        else:
            skipped += 1
    return {
        "status": "ok",
        "project_id": str(project_id),
        "chapters_chunked": chunked,
        "chapters_skipped": skipped,
    }
