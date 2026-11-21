"""Tier-4 memory pyramid (自由检索层): chapter full-text chunking.

Splits a chapter's ``content_text`` into structure-aware, paragraph-grouped,
sentence-boundary safe chunks (~500-800 chars; one spoken exchange may reach
``CHUNK_MAX_CHARS * 1.2``), embeds each and upserts them into the per-project
Qdrant shard ``chapter_chunks__<project-hex>`` (naming owned by
services/qdrant_store). ContextPack L3 recalls the top chunks as 【原文回读】
snippets so generation can re-read verbatim prose from anywhere in the book.

Chunk boundaries follow narrative structure rather than a pure character
budget: a run of quote-opening paragraphs (one exchange) is never cut while it
fits the dialogue cap, and a blank line / scene-time marker (次日, 与此同时,
—— …) always starts a new chunk.

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

from dataclasses import dataclass

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Chunk sizing: paragraph groups of roughly this many chars. A paragraph
# longer than CHUNK_MAX_CHARS is split at sentence boundaries.
CHUNK_TARGET_CHARS = 500
CHUNK_MAX_CHARS = 800

# Structure-aware chunking tuning (internal): the public constants above and
# the chunk_chapter_text signature keep their historical defaults.
_DIALOGUE_MAX_RATIO = 1.2  # a whole exchange may overshoot CHUNK_MAX_CHARS
_DIALOGUE_OPENERS = "\"“「『‘'"
# Paragraph openings that read as an explicit scene/time cut. Kept narrow on
# purpose: a false positive only starts a new chunk (safe), but too broad a
# list would shred ordinary narrative paragraphs.
_SCENE_MARKERS = (
    "次日",
    "翌日",
    "第二天",
    "隔日",
    "当晚",
    "当天夜里",
    "与此同时",
    "另一边",
    "另一头",
    "画面一转",
)

_SENTENCE_ENDERS = "。!?...!?"


@dataclass(frozen=True)
class _Atom:
    """Smallest indivisible piece: one paragraph or one sentence-boundary cut."""

    text: str
    is_dialogue: bool = False
    scene_break_before: bool = False


@dataclass(frozen=True)
class _Unit:
    """Chunk placement unit: a single atom, or a whole spoken exchange."""

    text: str
    is_dialogue: bool = False
    scene_break_before: bool = False


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


def _is_dialogue_paragraph(para: str) -> bool:
    """True when a paragraph opens with a spoken-line quote mark."""
    return bool(para) and para[0] in _DIALOGUE_OPENERS


def _starts_scene(para: str) -> bool:
    """True when a paragraph opens on an explicit scene/time transition."""
    return para.startswith(_SCENE_MARKERS)


def dialogue_char_cap(max_chars: int = CHUNK_MAX_CHARS) -> int:
    """Hard cap for one unbroken dialogue exchange (``max_chars * 1.2``).

    A dialogue exchange may overshoot ``max_chars`` so whole call-and-response
    runs stay in one chunk, but never beyond this cap.
    """
    return int(max_chars * _DIALOGUE_MAX_RATIO)


def _collect_atoms(text: str, *, max_chars: int) -> list[_Atom]:
    """Paragraph stream → indivisible atoms (paragraph, or sentence piece).

    An atom carries two structural flags collected from the raw text:

    - ``is_dialogue``: the paragraph opens with a quote mark, so it is part of
      a spoken-line exchange. Over-long paragraphs that were split at sentence
      boundaries are *not* flagged (their pieces must be free to land in
      different chunks).
    - ``scene_break_before``: a blank line separated this atom from the
      previous one, or it opens on a scene/time marker.
    """
    atoms: list[_Atom] = []
    blank_count = 0
    seen_text = False
    for raw in text.split("\n"):
        para = raw.strip()
        if not para:
            if seen_text:
                blank_count += 1
            continue
        # 2026-11-17 修正:中文网文习惯是**每段之间一个空行**,单空行只是段落
        # 分隔,不是分场。把单空行当分场会把每段拆成 ~90 字的碎片 ——
        # 在 100 章真实语料上实测:片段数 230 → 1109、对白被切断 4 → 37 处。
        # 只有**两个以上连续空行**才按作者标出的分场处理。
        scene_break = seen_text and (blank_count >= 2 or _starts_scene(para))
        seen_text = True
        blank_count = 0
        if len(para) > max_chars:
            pieces = _split_long_paragraph(para, max_chars)
        else:
            pieces = [para]
        whole = len(pieces) == 1
        for i, piece in enumerate(pieces):
            atoms.append(
                _Atom(
                    text=piece,
                    is_dialogue=whole and _is_dialogue_paragraph(para),
                    scene_break_before=scene_break if i == 0 else False,
                )
            )
    return atoms


def _group_units(atoms: list[_Atom], *, max_chars: int) -> list[_Unit]:
    """Merge consecutive dialogue paragraphs into one indivisible unit.

    A maximal run of quote-opening paragraphs is glued into a single unit as
    long as the merge stays within ``dialogue_char_cap``; past that cap the run
    is broken *between* turns only. A scene break always ends the run (it is a
    new beat, not the same exchange). Non-dialogue paragraphs stay one atom per
    unit.
    """
    cap = dialogue_char_cap(max_chars)
    units: list[_Unit] = []
    run: list[_Atom] = []
    run_len = 0

    def flush() -> None:
        nonlocal run, run_len
        if run:
            units.append(
                _Unit(
                    text="\n".join(a.text for a in run),
                    is_dialogue=run[0].is_dialogue,
                    scene_break_before=run[0].scene_break_before,
                )
            )
        run = []
        run_len = 0

    for atom in atoms:
        if not run:
            run = [atom]
            run_len = len(atom.text)
            continue
        if atom.is_dialogue and run[0].is_dialogue and not atom.scene_break_before:
            joined = run_len + 1 + len(atom.text)
            if joined <= cap:  # still one exchange: keep it whole
                run.append(atom)
                run_len = joined
                continue
        flush()
        run = [atom]
        run_len = len(atom.text)
    flush()
    return units


def _pack_units(
    units: list[_Unit], *, target_chars: int, max_chars: int
) -> list[str]:
    """Pack units into chunk strings, preferring structural break points.

    Rules, in order of precedence:

    1. A unit is never cut (dialogue units may reach ``dialogue_char_cap``).
    2. A scene break starts a new chunk even before ``target_chars``.
    3. Otherwise keep appending until ``target_chars`` is reached, without
       passing ``max_chars`` — a dialogue unit is allowed to extend the chunk
       up to ``dialogue_char_cap`` so the exchange stays intact.
    """
    cap = dialogue_char_cap(max_chars)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            chunks.append("\n".join(cur))
        cur = []
        cur_len = 0

    for unit in units:
        if not cur:
            cur = [unit.text]
            cur_len = len(unit.text)
            continue
        if unit.scene_break_before or cur_len >= target_chars:
            flush()
            cur = [unit.text]
            cur_len = len(unit.text)
            continue
        limit = cap if unit.is_dialogue else max_chars
        joined = cur_len + 1 + len(unit.text)
        if joined > limit:
            flush()
            cur = [unit.text]
            cur_len = len(unit.text)
        else:
            cur.append(unit.text)
            cur_len = joined
    flush()
    return chunks


def chunk_chapter_text(
    text: str,
    *,
    target_chars: int = CHUNK_TARGET_CHARS,
    max_chars: int = CHUNK_MAX_CHARS,
) -> list[str]:
    """Split chapter text into structure-aware, paragraph-grouped chunks.

    Deterministic and sentence-boundary safe. Chunks are complete narrative
    units:

    - paragraphs (newline-separated) are grouped until the group reaches
      ``target_chars``;
    - a run of quote-opening paragraphs (one spoken exchange) is kept in a
      single chunk up to ``dialogue_char_cap`` and is only broken between
      turns past that;
    - a blank line or a scene/time marker (次日/与此同时/—— …) starts a new
      chunk even if the current one is still short;
    - no chunk exceeds ``dialogue_char_cap`` (``max_chars * 1.2``); over-long
      paragraphs are split at sentence enders, single sentences hard-split.

    A short chapter yields a single chunk.
    """
    if not text or not text.strip():
        return []
    atoms = _collect_atoms(text, max_chars=max_chars)
    if not atoms:
        return []
    units = _group_units(atoms, max_chars=max_chars)
    return _pack_units(units, target_chars=target_chars, max_chars=max_chars)


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
    chapter_id: Any = None,
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

        source_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
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
                        "chapter_id": str(chapter_id) if chapter_id else None,
                        "source_content_hash": source_hash,
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
                Chapter.id,
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
    for chapter_id, volume_id, chapter_idx, global_idx, content_text in chapters:
        n = await upsert_chapter_chunks(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
            global_idx=global_idx,
            content_text=content_text or "",
            chapter_id=chapter_id,
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
