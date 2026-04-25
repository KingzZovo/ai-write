"""Reference-book decompile orchestrator (v0.6).

Pipeline (per reference book):
  1. Load raw text from TextChunk rows (already cleaned by crawler/upload).
  2. Re-chunk into semantic slices via semantic_chunker.
  3. For each slice (concurrently, bounded):
      - style_abstractor -> StyleProfileCard + Qdrant point
      - beat_extractor -> BeatSheetCard + Qdrant point
      - entity_redactor + embed -> Qdrant style_samples_redacted point
  4. Persist slice + cards in Postgres, update book status.

Feature flag: STYLE_REDACTION_ENABLED (default on) controls the third branch.
"""

from __future__ import annotations

import asyncio
import logging
import os

from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import async_session_factory
from app.models.decompile import BeatSheetCard, ReferenceBookSlice, StyleProfileCard
from app.models.project import ReferenceBook, TextChunk
from app.services import semantic_chunker
from app.services.beat_extractor import extract_beat
from app.services.entity_redactor import redact as redact_text
from app.services.feature_extractor import generate_embedding
from app.services.qdrant_store import QdrantStore
from app.services.style_abstractor import abstract_style

logger = logging.getLogger(__name__)

_REDACTION_ENABLED = os.getenv("STYLE_REDACTION_ENABLED", "true").lower() in ("1", "true", "yes")
_CONCURRENCY = int(os.getenv("REFERENCE_INGEST_CONCURRENCY", "3"))


async def _qdrant_client() -> AsyncQdrantClient:
    return AsyncQdrantClient(
        host=getattr(settings, "QDRANT_HOST", "localhost"),
        port=getattr(settings, "QDRANT_PORT", 6333),
    )


async def _load_book_text(db: AsyncSession, book_id: str) -> str:
    result = await db.execute(
        select(TextChunk)
        .where(TextChunk.book_id == book_id)
        .order_by(TextChunk.sequence_id.asc())
    )
    chunks = list(result.scalars().all())
    if not chunks:
        return ""
    # Preserve chapter boundaries: prefix chapter_title when it changes.
    parts: list[str] = []
    last_chapter_idx = -1
    for c in chunks:
        if c.chapter_idx != last_chapter_idx:
            if c.chapter_title:
                parts.append(f"\n\n{c.chapter_title}\n")
            last_chapter_idx = c.chapter_idx
        parts.append(c.content or "")
    return "".join(parts)


async def _process_slice(
    *,
    db: AsyncSession,
    store: QdrantStore,
    book_id: str,
    slc: semantic_chunker.Slice,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        raw = slc.raw_text

        # 1. Persist the slice first so we have a UUID for FK references.
        slice_row = ReferenceBookSlice(
            book_id=book_id,
            slice_type=slc.slice_type,
            chapter_idx=slc.chapter_idx,
            sequence_id=slc.sequence_id,
            start_offset=slc.start_offset,
            end_offset=slc.end_offset,
            raw_text=raw,
            token_count=slc.token_count,
        )
        db.add(slice_row)
        await db.flush()
        slice_id = str(slice_row.id)

        # 2. Launch the three branches concurrently.
        async def _style_branch() -> dict:
            profile = await abstract_style(raw, db)
            if not profile:
                return {"ok": False}
            summary_text = (
                f"{profile.get('pov', '')} | {profile.get('sentence_rhythm', '')} | "
                f"{profile.get('emotional_register', '')} | "
                f"{','.join(profile.get('vocab_tone', []) or [])}"
            )
            emb = await generate_embedding(summary_text)
            point_id = await store.store_style_profile(book_id, slice_id, profile, emb)
            db.add(StyleProfileCard(
                book_id=book_id,
                slice_id=slice_row.id,
                profile_json=profile,
                qdrant_point_id=str(point_id),
            ))
            return {"ok": True}

        async def _beat_branch() -> dict:
            beat = await extract_beat(raw, db)
            if not beat:
                return {"ok": False}
            summary_text = (
                f"{beat.get('scene_type', '')} | {beat.get('reusable_pattern', '')} | "
                f"{beat.get('emotional_arc', '')}"
            )
            emb = await generate_embedding(summary_text)
            point_id = await store.store_beat_sheet(book_id, slice_id, beat, emb)
            db.add(BeatSheetCard(
                book_id=book_id,
                slice_id=slice_row.id,
                beat_json=beat,
                qdrant_point_id=str(point_id),
            ))
            return {"ok": True}

        async def _redact_branch() -> dict:
            if not _REDACTION_ENABLED:
                return {"ok": False, "skipped": True}
            redacted = await redact_text(raw, db)
            # If redaction failed or returned an empty string, fall back to raw
            # so we still have a usable passage for style reference search.
            text_to_embed = redacted if (redacted and redacted.strip()) else raw
            emb = await generate_embedding(text_to_embed)
            await store.store_style_sample_redacted(
                book_id=book_id,
                slice_id=slice_id,
                redacted_text=text_to_embed,
                embedding=emb,
            )
            return {"ok": True}

        results = await asyncio.gather(
            _style_branch(),
            _beat_branch(),
            _redact_branch(),
            return_exceptions=True,
        )
        await db.flush()

        summary = {
            "slice_id": slice_id,
            "style_ok": isinstance(results[0], dict) and results[0].get("ok"),
            "beat_ok": isinstance(results[1], dict) and results[1].get("ok"),
            "redact_ok": isinstance(results[2], dict) and results[2].get("ok"),
        }
        for r in results:
            if isinstance(r, Exception):
                logger.warning("slice %s branch failed: %s", slice_id, r)
        return summary


async def reprocess_reference_book(
    *,
    book_id: str,
    db: AsyncSession | None = None,
) -> dict:
    """Run the full decompile pipeline for one reference book.

    Returns {done, total, failed, status}. Safe to call repeatedly;
    previous slices/cards for the book are wiped before re-ingest.
    """
    owns = db is None
    if db is None:
        db = async_session_factory()

    try:
        book = await db.get(ReferenceBook, book_id)
        if book is None:
            return {"status": "error", "error": "book not found"}

        book.status = "extracting"
        await db.flush()

        text = await _load_book_text(db, book_id)
        if not text.strip():
            book.status = "error"
            book.error_message = "no text chunks"
            await db.commit()
            return {"status": "error", "total": 0, "done": 0, "failed": 0}

        # Wipe previous slices (cascade wipes cards).
        old = await db.execute(
            select(ReferenceBookSlice).where(ReferenceBookSlice.book_id == book_id)
        )
        for s in old.scalars().all():
            await db.delete(s)
        await db.flush()

        slices = semantic_chunker.chunk(text)
        if not slices:
            book.status = "error"
            book.error_message = "semantic chunking yielded no slices"
            await db.commit()
            return {"status": "error", "total": 0, "done": 0, "failed": 0}

        client = await _qdrant_client()
        store = QdrantStore(client)
        await store.ensure_collections()

        semaphore = asyncio.Semaphore(_CONCURRENCY)
        results = await asyncio.gather(
            *[
                _process_slice(
                    db=db,
                    store=store,
                    book_id=book_id,
                    slc=s,
                    semaphore=semaphore,
                )
                for s in slices
            ],
            return_exceptions=True,
        )

        done = sum(1 for r in results if isinstance(r, dict))
        failed = sum(1 for r in results if isinstance(r, Exception))
        book.status = "ready" if failed == 0 else "error"
        if failed:
            book.error_message = f"{failed} slices failed during decompile"
        await db.commit()
        try:
            await client.close()
        except Exception:
            pass

        return {
            "status": book.status,
            "total": len(slices),
            "done": done,
            "failed": failed,
        }
    finally:
        if owns:
            await db.close()
