"""Reference-book decompile orchestrator (v1.7).

Pipeline (per reference book):
  1. Load raw text from TextChunk rows (already cleaned by crawler/upload).
  2. Re-chunk into semantic slices via semantic_chunker.
  3. For each slice (concurrently, bounded):
      - style_abstractor -> StyleProfileCard + Qdrant point
      - beat_extractor -> BeatSheetCard + Qdrant point
      - entity_redactor + embed -> Qdrant style_samples_redacted point
  4. Persist slice + cards in Postgres, update book status.

Feature flag: STYLE_REDACTION_ENABLED (default on) controls the third branch.

v1.7 (partial-failure handling):
  - reprocess_reference_book counts per-branch successes (style/beat),
    classifies the run as ready / partial / error and persists detailed
    state to ReferenceBook.metadata_json["decompile_retry"].
  - retry_missing_branches re-runs only the style/beat branches for slices
    whose cards are still missing, without wiping existing data. Designed
    to be invoked from a celery countdown task or from a manual UI action
    to recover from transient upstream LLM outages.
  - redact branch is not retried at row granularity because its output
    only lives in Qdrant (no PG table to detect missing slices). It will
    be re-attempted only as part of a fresh full reprocess.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

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
# Maximum number of automatic retry waves scheduled by reprocess after a
# partial run. The first retry runs ``DECOMPILE_RETRY_INITIAL_DELAY`` seconds
# after the partial run completes; subsequent retries back off geometrically.
_MAX_AUTO_RETRIES = int(os.getenv("DECOMPILE_MAX_AUTO_RETRIES", "5"))
_RETRY_INITIAL_DELAY = int(os.getenv("DECOMPILE_RETRY_INITIAL_DELAY", "300"))
_RETRY_BACKOFF_FACTOR = float(os.getenv("DECOMPILE_RETRY_BACKOFF_FACTOR", "2.0"))


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


async def _persist_slice(
    *,
    book_id: str,
    slc: semantic_chunker.Slice,
) -> tuple[object, str, str]:
    """Insert a ReferenceBookSlice row and commit. Returns (slice_uuid, slice_id, raw_text)."""
    raw = slc.raw_text
    async with async_session_factory() as db:
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
        slice_uuid = slice_row.id
        slice_id = str(slice_uuid)
        await db.commit()
    return slice_uuid, slice_id, raw


async def _style_branch(
    *,
    store: QdrantStore,
    book_id: str,
    slice_uuid: object,
    slice_id: str,
    raw: str,
) -> bool:
    async with async_session_factory() as bdb:
        try:
            profile = await abstract_style(raw, bdb)
            if not profile:
                return False
            summary_text = (
                f"{profile.get('pov', '')} | "
                f"{profile.get('sentence_rhythm', '')} | "
                f"{profile.get('emotional_register', '')} | "
                f"{','.join(profile.get('vocab_tone', []) or [])}"
            )
            emb = await generate_embedding(summary_text)
            point_id = await store.store_style_profile(
                book_id, slice_id, profile, emb
            )
            bdb.add(StyleProfileCard(
                book_id=book_id,
                slice_id=slice_uuid,
                profile_json=profile,
                qdrant_point_id=str(point_id),
            ))
            await bdb.commit()
            return True
        except Exception as exc:
            logger.warning("slice %s style branch failed: %s", slice_id, exc)
            await bdb.rollback()
            return False


async def _beat_branch(
    *,
    store: QdrantStore,
    book_id: str,
    slice_uuid: object,
    slice_id: str,
    raw: str,
) -> bool:
    async with async_session_factory() as bdb:
        try:
            beat = await extract_beat(raw, bdb)
            if not beat:
                return False
            summary_text = (
                f"{beat.get('scene_type', '')} | "
                f"{beat.get('reusable_pattern', '')} | "
                f"{beat.get('emotional_arc', '')}"
            )
            emb = await generate_embedding(summary_text)
            point_id = await store.store_beat_sheet(
                book_id, slice_id, beat, emb
            )
            bdb.add(BeatSheetCard(
                book_id=book_id,
                slice_id=slice_uuid,
                beat_json=beat,
                qdrant_point_id=str(point_id),
            ))
            await bdb.commit()
            return True
        except Exception as exc:
            logger.warning("slice %s beat branch failed: %s", slice_id, exc)
            await bdb.rollback()
            return False


async def _redact_branch(
    *,
    store: QdrantStore,
    book_id: str,
    slice_id: str,
    raw: str,
) -> bool:
    if not _REDACTION_ENABLED:
        return False
    async with async_session_factory() as bdb:
        try:
            redacted = await redact_text(raw, bdb)
            text_to_embed = redacted if (redacted and redacted.strip()) else raw
            emb = await generate_embedding(text_to_embed)
            await store.store_style_sample_redacted(
                book_id=book_id,
                slice_id=slice_id,
                redacted_text=text_to_embed,
                embedding=emb,
            )
            await bdb.commit()
            return True
        except Exception as exc:
            logger.warning("slice %s redact branch failed: %s", slice_id, exc)
            await bdb.rollback()
            return False


async def _process_slice(
    *,
    store: QdrantStore,
    book_id: str,
    slc: semantic_chunker.Slice,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Process one slice: persist row, then run 3 branches in parallel.

    v1.4.2 hardening: each slice owns its own AsyncSession. Each branch
    catches its own exceptions and returns a bool, so a single branch
    failure cannot cancel its siblings.
    """
    async with semaphore:
        slice_uuid, slice_id, raw = await _persist_slice(book_id=book_id, slc=slc)

        style_ok, beat_ok, redact_ok = await asyncio.gather(
            _style_branch(
                store=store, book_id=book_id, slice_uuid=slice_uuid,
                slice_id=slice_id, raw=raw,
            ),
            _beat_branch(
                store=store, book_id=book_id, slice_uuid=slice_uuid,
                slice_id=slice_id, raw=raw,
            ),
            _redact_branch(
                store=store, book_id=book_id, slice_id=slice_id, raw=raw,
            ),
        )
        return {
            "slice_id": slice_id,
            "style_ok": bool(style_ok),
            "beat_ok": bool(beat_ok),
            "redact_ok": bool(redact_ok),
        }


# ---------------------------------------------------------------------------
# Coverage helpers (used by both reprocess_reference_book and
# retry_missing_branches to compute partial-failure state).
# ---------------------------------------------------------------------------
async def _count_distinct(
    db: AsyncSession,
    table,
    book_id: str,
) -> int:
    return (
        await db.scalar(
            select(func.count(func.distinct(table.slice_id))).where(
                table.book_id == book_id
            )
        )
    ) or 0


async def _count_slices(db: AsyncSession, book_id: str) -> int:
    return (
        await db.scalar(
            select(func.count(ReferenceBookSlice.id)).where(
                ReferenceBookSlice.book_id == book_id
            )
        )
    ) or 0


async def _slices_missing_style(
    db: AsyncSession, book_id: str
) -> list[tuple[object, str, str]]:
    """Return [(slice_uuid, slice_id_str, raw_text), ...] for slices without StyleProfileCard."""
    sub = select(StyleProfileCard.slice_id).where(StyleProfileCard.book_id == book_id)
    rows = await db.execute(
        select(ReferenceBookSlice.id, ReferenceBookSlice.raw_text)
        .where(ReferenceBookSlice.book_id == book_id)
        .where(~ReferenceBookSlice.id.in_(sub))
        .order_by(ReferenceBookSlice.sequence_id.asc())
    )
    return [(r[0], str(r[0]), r[1]) for r in rows.all()]


async def _slices_missing_beat(
    db: AsyncSession, book_id: str
) -> list[tuple[object, str, str]]:
    sub = select(BeatSheetCard.slice_id).where(BeatSheetCard.book_id == book_id)
    rows = await db.execute(
        select(ReferenceBookSlice.id, ReferenceBookSlice.raw_text)
        .where(ReferenceBookSlice.book_id == book_id)
        .where(~ReferenceBookSlice.id.in_(sub))
        .order_by(ReferenceBookSlice.sequence_id.asc())
    )
    return [(r[0], str(r[0]), r[1]) for r in rows.all()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify(*, total: int, style_done: int, beat_done: int) -> str:
    """Pick a book.status based on per-branch coverage.

    - ready : every slice has both a style and a beat card.
    - partial : at least one slice is still missing a style or beat card,
        but the orchestrator itself completed without raising.
    - error : we never even got to run any slice (caller decides).
    """
    if total == 0:
        return "error"
    if style_done >= total and beat_done >= total:
        return "ready"
    return "partial"


async def _refresh_book_status(
    db: AsyncSession,
    book: ReferenceBook,
    *,
    attempt: int,
    note: str | None = None,
) -> dict:
    """Recompute coverage + status + metadata_json.decompile_retry for a book."""
    book_id = str(book.id)
    total = await _count_slices(db, book_id)
    style_done = await _count_distinct(db, StyleProfileCard, book_id)
    beat_done = await _count_distinct(db, BeatSheetCard, book_id)
    missing_style = max(total - style_done, 0)
    missing_beat = max(total - beat_done, 0)

    status = _classify(total=total, style_done=style_done, beat_done=beat_done)
    book.status = status

    if status == "ready":
        # Clear stale failure messages once everything is covered.
        if book.error_message:
            book.error_message = None
    else:
        msg_parts = []
        if missing_style:
            msg_parts.append(f"{missing_style} slices missing style card")
        if missing_beat:
            msg_parts.append(f"{missing_beat} slices missing beat card")
        msg = "; ".join(msg_parts) if msg_parts else "decompile incomplete"
        if note:
            msg = f"{msg} ({note})"
        book.error_message = msg

    meta = dict(book.metadata_json or {})
    meta["decompile_retry"] = {
        "attempt": int(attempt),
        "max_attempts": _MAX_AUTO_RETRIES,
        "last_run_at": _now_iso(),
        "total_slices": int(total),
        "style_done": int(style_done),
        "beat_done": int(beat_done),
        "missing_style": int(missing_style),
        "missing_beat": int(missing_beat),
        "status": status,
    }
    book.metadata_json = meta
    flag_modified(book, "metadata_json")
    return meta["decompile_retry"]


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------
async def reprocess_reference_book(
    *,
    book_id: str,
    db: AsyncSession | None = None,
) -> dict:
    """Run the full decompile pipeline for one reference book.

    Returns a dict containing per-branch coverage and the resulting status
    (ready / partial / error). Safe to call repeatedly; previous slices
    and cards for the book are wiped before re-ingest.
    """
    owns = db is None
    if db is None:
        db = async_session_factory()

    client = None
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
            return {"status": "error", "total": 0, "style_done": 0, "beat_done": 0}

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
            return {"status": "error", "total": 0, "style_done": 0, "beat_done": 0}

        client = await _qdrant_client()
        store = QdrantStore(client)
        await store.ensure_collections()

        semaphore = asyncio.Semaphore(_CONCURRENCY)
        results = await asyncio.gather(
            *[
                _process_slice(
                    store=store,
                    book_id=book_id,
                    slc=s,
                    semaphore=semaphore,
                )
                for s in slices
            ],
            return_exceptions=True,
        )

        hard_failed = sum(1 for r in results if isinstance(r, Exception))
        # branch-level counters are derived from PG below to stay authoritative.
        retry_state = await _refresh_book_status(
            db, book, attempt=0,
            note=(f"{hard_failed} slices raised exceptions" if hard_failed else None),
        )
        await db.commit()

        return {
            "status": book.status,
            "total": retry_state["total_slices"],
            "style_done": retry_state["style_done"],
            "beat_done": retry_state["beat_done"],
            "missing_style": retry_state["missing_style"],
            "missing_beat": retry_state["missing_beat"],
            "hard_failed": int(hard_failed),
            "retry": retry_state,
        }
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
        if owns:
            await db.close()


async def retry_missing_branches(
    *,
    book_id: str,
    attempt: int = 1,
    db: AsyncSession | None = None,
) -> dict:
    """Re-run style/beat branches for slices whose cards are still missing.

    Does not touch the slice rows themselves nor the redact qdrant points.
    Designed to recover from transient upstream LLM outages without
    rebuilding the entire ingest pipeline.
    """
    owns = db is None
    if db is None:
        db = async_session_factory()

    client = None
    try:
        book = await db.get(ReferenceBook, book_id)
        if book is None:
            return {"status": "error", "error": "book not found"}

        # Snapshot which slices are missing each branch BEFORE running.
        missing_style_rows = await _slices_missing_style(db, book_id)
        missing_beat_rows = await _slices_missing_beat(db, book_id)

        if not missing_style_rows and not missing_beat_rows:
            # Nothing to do; just refresh status to keep metadata accurate.
            retry_state = await _refresh_book_status(db, book, attempt=int(attempt))
            await db.commit()
            return {
                "status": book.status,
                "style_filled": 0,
                "beat_filled": 0,
                "retry": retry_state,
            }

        client = await _qdrant_client()
        store = QdrantStore(client)
        await store.ensure_collections()

        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def _run_style(item):
            slice_uuid, slice_id, raw = item
            async with semaphore:
                ok = await _style_branch(
                    store=store, book_id=book_id, slice_uuid=slice_uuid,
                    slice_id=slice_id, raw=raw,
                )
                return 1 if ok else 0

        async def _run_beat(item):
            slice_uuid, slice_id, raw = item
            async with semaphore:
                ok = await _beat_branch(
                    store=store, book_id=book_id, slice_uuid=slice_uuid,
                    slice_id=slice_id, raw=raw,
                )
                return 1 if ok else 0

        # Run style+beat in parallel; each branch already serializes by
        # using a per-slice short-lived AsyncSession.
        style_results, beat_results = await asyncio.gather(
            asyncio.gather(*[_run_style(it) for it in missing_style_rows], return_exceptions=True),
            asyncio.gather(*[_run_beat(it) for it in missing_beat_rows], return_exceptions=True),
        )
        style_filled = sum(r for r in style_results if isinstance(r, int))
        beat_filled = sum(r for r in beat_results if isinstance(r, int))

        # Detached `book` may now be stale; reload before refresh.
        await db.refresh(book)
        retry_state = await _refresh_book_status(
            db, book, attempt=int(attempt),
            note=f"retry attempt {attempt} filled style={style_filled}, beat={beat_filled}",
        )
        await db.commit()

        return {
            "status": book.status,
            "style_filled": int(style_filled),
            "beat_filled": int(beat_filled),
            "missing_style_before": len(missing_style_rows),
            "missing_beat_before": len(missing_beat_rows),
            "retry": retry_state,
        }
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
        if owns:
            await db.close()


def compute_retry_delay(attempt: int) -> int:
    """Seconds to wait before retry #attempt (attempt is 1-indexed)."""
    if attempt <= 1:
        return _RETRY_INITIAL_DELAY
    return int(_RETRY_INITIAL_DELAY * (_RETRY_BACKOFF_FACTOR ** (attempt - 1)))


def max_auto_retries() -> int:
    return _MAX_AUTO_RETRIES
