"""v1.7.4 P0-2: chapter post-generation summarizer.

Problem (pre-v1.7.4): Chapter.summary stays NULL forever after generation.
This means ContextPack._build_proximity has empty `recent_summaries`, so the
next chapter is generated with no memory of what just happened in the previous
chapter. Drift compounds as the volume progresses.

Fix: After a chapter is persisted, run a tight summarize call and write the
result back to chapter.summary. Synchronous helper used by the celery task
`tasks.summarize_chapter` and by manual backfill scripts.

The summarizer receives the complete chapter without mechanically clipping
source text or output. Factual coverage still needs verification. We use the
existing prompt_assets.task_type='summary'.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.project import Chapter
from app.services.summary_provenance import summary_is_current, summary_source_hash

logger = logging.getLogger(__name__)

# Summaries must see the complete chapter; provider capacity errors remain failures.
_MIN_INPUT_CHARS = 200

# Collection naming is owned by services/qdrant_store (per-project sharding:
# chapter_summaries__<project-hex>, with legacy-global fallback on reads).
# Points written here MUST stay interchangeable with rag_rebuild's: same
# deterministic id per (volume_id, chapter_idx), same payload keys.

# Auto-compaction guard: at most one in-flight compaction per project.
_COMPACT_IN_FLIGHT: set[str] = set()
# Strong references to detached compaction tasks (asyncio tasks are only
# weakly referenced by the loop; without this a task could be GC'd mid-run).
_COMPACT_TASKS: set = set()

_USER_TEMPLATE = (
    "请用 80-160 个中文字概括下面这章内容。"
    "只写事实发生了什么、人物状态变化、新引出的问题。"
    "不要评价，不要总结主题，不要加“本章讲述了”这类套话。\n\n"
    "【章节标题】{title}\n\n"
    "【章节正文】\n{content}"
)


def _clean_summary_output(raw: str) -> str:
    """Unwrap complete prose/JSON without decoding Chinese as byte escapes.

    Invalid wrappers are rejected, not guessed by extracting a partial field.
    Literal newlines in otherwise valid JSON strings are tolerated; all
    paragraphs of the summary are preserved.
    """
    import json
    import re

    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        fence = re.fullmatch(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*", text, re.DOTALL)
        if not fence:
            return ""
        text = fence.group(1).strip()

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate summary wrapper key")
            obj[key] = value
        return obj

    if text.startswith(("{", "[", '"')):
        try:
            value = json.loads(text, strict=False, object_pairs_hook=unique_object)
        except (ValueError, TypeError):
            return ""
        if isinstance(value, dict):
            value = next((value[key] for key in ("summary", "摘要", "总结", "text", "content") if key in value), None)
        if not isinstance(value, str):
            return ""
        text = value.strip()
    elif len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", "`"):
        text = text[1:-1].strip()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return "" if "\x00" in text else text


async def summarize_chapter_text(
    *,
    title: str,
    content_text: str,
    db: AsyncSession | None,
    project_id: Any = None,
    chapter_id: Any = None,
) -> str:
    """Generate chapter memory from the complete source, without local clipping.

    Returns empty string on any failure rather than raising, so the chapter
    persistence path is never broken by summarizer trouble.
    """
    if not content_text or len(content_text) < _MIN_INPUT_CHARS:
        return ""
    user = _USER_TEMPLATE.format(title=title or "", content=content_text)
    try:
        from app.services.prompt_registry import run_text_prompt
        for attempt in range(2):
            result = await run_text_prompt(
                task_type="summary", user_content=user, db=db,
                project_id=project_id, chapter_id=chapter_id, max_tokens=2048,
            )
            raw = result.text or ""
            out = _clean_summary_output(raw)
            if out:
                return out
            if attempt == 0:
                user += (
                    "\n\n上次输出没有可解析的完整摘要。请重新依据完整正文返回合法JSON，"
                    "summary必须是完整字符串，保留全部关键事件；不要输出代码围栏。"
                    "\n【上次输出】\n" + raw
                )
        logger.warning("Summary output remained invalid (chapter_id=%s)", chapter_id)
        return ""
    except Exception as e:
        logger.warning("summarize_chapter_text failed (chapter_id=%s): %s", chapter_id, e)
        return ""


async def summarize_scoped_chapter(
    *, db: AsyncSession, project_id: Any, volume_id: Any,
    chapter_idx: int, chapter_text: str, project_vectors: bool = True,
) -> tuple[bool, str]:
    """Resolve a persisted chapter under its project before dispatching memory."""
    from app.models.project import Volume

    # Retain the scoped source identity through model dispatch: a move after
    # this query must fail the shared snapshot CAS, not load a new location.
    rows = (await db.execute(
        select(Chapter).join(Volume, Chapter.volume_id == Volume.id).where(
            Volume.project_id == str(project_id), Chapter.volume_id == str(volume_id),
            Chapter.chapter_idx == chapter_idx,
        ).limit(2).execution_options(autoflush=False)
    )).scalars().all()
    if len(rows) != 1:
        return False, ""
    return await summarize_and_save_chapter(
        chapter_id=rows[0].id, db=db, expected_content=chapter_text,
        project_vectors=project_vectors,
    )


@asynccontextmanager
async def _summary_session(db: AsyncSession | None):
    """Close owned phases; never commit/rollback a borrowed read transaction."""
    if db is not None:
        yield db
    else:
        from app.db.session import async_session_factory
        async with async_session_factory() as owned:
            yield owned


async def summarize_and_save_chapter(
    *,
    chapter_id: str | UUID,
    db: AsyncSession | None = None,
    overwrite: bool = False,
    project_vectors: bool = True,
    expected_content: str | None = None,
) -> tuple[bool, str]:
    """Fetch chapter by id, summarize, write back to chapter.summary.

    Returns (was_written, summary_text).
    With db=None, source reads close before model IO and CAS uses a new session.
    Borrowed sessions retain their existing ownership contract.
    """
    async with _summary_session(db) as read_db:
        with read_db.no_autoflush:
            chapter = await read_db.get(Chapter, str(chapter_id))
        if chapter is None or chapter in read_db.dirty:
            return False, ""
        if expected_content is not None and chapter.content_text != expected_content:
            return False, ""
        if not chapter.content_text or len(chapter.content_text) < _MIN_INPUT_CHARS:
            return False, ""
        # Bind the output to exactly the text/metadata observed before model IO.
        # updated_at also fences edit-and-restore (ABA) and concurrent summaries.
        source = {name: getattr(chapter, name) for name in (
            "content_text", "title", "summary", "summary_source_hash", "updated_at", "status",
            "volume_id", "chapter_idx", "global_idx",
        )}
        source_id = chapter.id
        if not overwrite and summary_is_current(chapter):
            # A caller may hold an old ORM identity while another session edits
            # the chapter. Validate reuse against the current DB row, without
            # flushing or refreshing away pending author changes in this session.
            current = await read_db.scalar(
                select(Chapter.id).where(
                    Chapter.id == source_id,
                    *(getattr(Chapter, name).is_not_distinct_from(value)
                      for name, value in source.items()),
                ).execution_options(autoflush=False)
            )
            return False, chapter.summary if current is not None else ""
        project_id = None
        volume_idx = None
        try:
            from app.models.project import Volume
            if chapter.volume_id is not None:
                volume = await read_db.get(Volume, str(chapter.volume_id))
                if volume is not None:
                    project_id = volume.project_id
                    volume_idx = volume.volume_idx
        except Exception:
            pass

    summary = await summarize_chapter_text(
        title=chapter.title or "",
        content_text=chapter.content_text,
        db=db,
        project_id=project_id,
        chapter_id=chapter.id,
    )
    if not summary:
        return False, ""
    if any(getattr(chapter, name) != value for name, value in source.items()):
        return False, ""
    provenance = summary_source_hash(source["title"], source["content_text"], summary)
    statement = (
        update(Chapter)
        .where(Chapter.id == source_id,
               *(getattr(Chapter, name).is_not_distinct_from(value) for name, value in source.items()))
        # Derived memory is not an author/body revision. Advancing updated_at
        # here bypasses the ORM write fence and makes our next status/body
        # save appear stale. Source CAS still compares the body revision and
        # old summary/hash, so concurrent author edits and summaries are fenced.
        .values(summary=summary, summary_source_hash=provenance,
                updated_at=Chapter.updated_at)
        .returning(Chapter.updated_at)
        .execution_options(synchronize_session=False, autoflush=False)
    )
    async with _summary_session(db) as write_db:
        saved = (await write_db.execute(statement)).first()
        if saved is None:
            logger.info("Discarding stale chapter summary (chapter_id=%s)", source_id)
            return False, ""
        await write_db.commit()
    # Avoid a second ORM UPDATE, and keep callers using this identity coherent.
    set_committed_value(chapter, "summary", summary)
    set_committed_value(chapter, "summary_source_hash", provenance)
    set_committed_value(chapter, "updated_at", saved[0])

    if not project_vectors:
        return True, summary

    # RAG-L3 fix: mirror the summary into the Qdrant chapter_summaries
    # collection so vector recall works on normally-generated novels, not just
    # after a manual rebuild. Failure-tolerant by contract — the chapter (and
    # its summary) are already committed above and must stay saved.
    await upsert_chapter_summary_embedding(
        project_id=project_id,
        volume_id=chapter.volume_id,
        chapter_idx=chapter.chapter_idx,
        chapter_title=chapter.title or "",
        summary=summary,
        global_idx=chapter.global_idx,
        volume_idx=volume_idx,
        chapter_id=source_id,
        source_hash=provenance,
    )

    # Tier-4 (自由检索层): mirror the chapter's FULL TEXT into the per-project
    # chapter_chunks shard so ContextPack L3 can recall verbatim prose, not
    # just summaries. Same failure-tolerant contract as the summary upsert —
    # the committed chapter must stay saved whatever happens here.
    try:
        from app.services.chapter_chunker import upsert_chapter_chunks

        await upsert_chapter_chunks(
            project_id=project_id,
            volume_id=chapter.volume_id,
            chapter_idx=chapter.chapter_idx,
            global_idx=chapter.global_idx,
            content_text=source["content_text"] or "",
            chapter_id=source_id,
        )
    except Exception as e:  # noqa: BLE001 — never break the save path
        logger.warning(
            "chapter chunk upsert failed (chapter_id=%s): %s", chapter_id, e
        )
    return True, summary


async def upsert_chapter_summary_embedding(
    *,
    project_id: Any,
    volume_id: Any,
    chapter_idx: int | None,
    chapter_title: str,
    summary: str,
    global_idx: int | None = None,
    volume_idx: int | None = None,
    chapter_id: Any = None,
    source_hash: str | None = None,
) -> bool:
    """Embed a chapter summary and upsert it into the project's summary shard.

    Collection resolution goes through services/qdrant_store
    (``chapter_summaries__<project-hex>``); on first shard creation the
    project's legacy-global points are copied over so recall never loses
    pre-shard history. Follows services/rag_rebuild.py's conventions exactly
    (deterministic point id from md5(f"{volume_id}_{chapter_idx}"), same
    payload shape) so points from this path and the manual rebuild endpoint
    overwrite each other cleanly.

    After a successful upsert, auto-compaction is fired (fire-and-forget)
    when the project's live point count exceeds
    ``settings.MEMORY_COMPACT_THRESHOLD_POINTS``.

    Never raises — an embedding/Qdrant failure logs a warning and returns
    False so the chapter save path is never broken.
    """
    if not summary or project_id is None or volume_id is None or chapter_idx is None:
        return False
    try:
        import hashlib

        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import PointStruct

        from app.config import settings
        from app.services import qdrant_store as qs
        from app.services.feature_extractor import generate_embedding

        vector = await generate_embedding(summary)
        if not vector:
            return False

        client = AsyncQdrantClient(
            host=getattr(settings, "QDRANT_HOST", "localhost"),
            port=getattr(settings, "QDRANT_PORT", 6333),
        )
        try:
            # Dimension-agnostic: shard is created with the dimension the
            # configured embedding model actually returns (2048 for
            # nvidia/llama-nemotron-embed-vl-1b-v2).
            collection = await qs.ensure_summary_shard(
                client, project_id, len(vector)
            )
            key = f"{volume_id}_{chapter_idx}"
            point_id = int(hashlib.md5(key.encode()).hexdigest()[:16], 16)
            await client.upsert(
                collection_name=collection,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=vector,
                        payload={
                            "project_id": str(project_id),
                            "volume_id": str(volume_id),
                            "chapter_idx": chapter_idx,
                            "chapter_title": chapter_title or "",
                            "summary": summary,
                            # Book-global context (E2E 2026-07-26): without
                            # these, two volumes' "chapter 1" points were
                            # indistinguishable in recall results. Point id
                            # stays (volume_id, chapter_idx)-keyed, so existing
                            # points get enriched on their next overwrite.
                            "global_idx": global_idx,
                            "volume_idx": volume_idx,
                            "chapter_id": str(chapter_id) if chapter_id else None,
                            "summary_source_hash": source_hash,
                        },
                    )
                ],
            )
            # Auto-compaction trigger — must never block or fail the save.
            try:
                await _maybe_trigger_auto_compaction(
                    str(project_id), collection, client
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("auto-compaction trigger check failed: %s", e)
        finally:
            try:
                await client.close()
            except Exception:
                pass
        return True
    except Exception as e:
        logger.warning(
            "chapter summary Qdrant upsert failed (volume_id=%s idx=%s): %s",
            volume_id, chapter_idx, e,
        )
        return False


async def _maybe_trigger_auto_compaction(
    project_id: str,
    collection: str,
    client: Any,
) -> None:
    """Fire-and-forget compaction when the live point count crosses threshold.

    Counts non-compacted points in the project's live collection; above
    ``settings.MEMORY_COMPACT_THRESHOLD_POINTS`` (default 200) it schedules
    ``compact_project_memory`` on the running loop with its own DB session.
    At most one compaction runs per project at a time. The manual endpoint
    (POST /api/projects/{id}/compact-memory) is unaffected.
    """
    from app.config import settings

    threshold = int(getattr(settings, "MEMORY_COMPACT_THRESHOLD_POINTS", 200))
    if threshold <= 0 or project_id in _COMPACT_IN_FLIGHT:
        return
    from qdrant_client import models as qmodels

    count_res = await client.count(
        collection_name=collection,
        count_filter=qmodels.Filter(
            must_not=[
                qmodels.FieldCondition(
                    key="compacted", match=qmodels.MatchValue(value=True)
                )
            ]
        ),
        exact=True,
    )
    live_points = int(getattr(count_res, "count", 0) or 0)
    if live_points <= threshold:
        return

    import asyncio

    _COMPACT_IN_FLIGHT.add(project_id)
    logger.info(
        "Auto-compaction triggered for project %s (%d live points > %d)",
        project_id, live_points, threshold,
    )
    task = asyncio.get_running_loop().create_task(_run_auto_compaction(project_id))
    _COMPACT_TASKS.add(task)
    task.add_done_callback(_COMPACT_TASKS.discard)


async def _run_auto_compaction(project_id: str) -> None:
    """Detached compaction run; opens its own session, never raises."""
    try:
        from app.db.session import async_session_factory
        from app.services.memory_compactor import compact_project_memory

        async with async_session_factory() as db:
            result = await compact_project_memory(
                project_id=project_id, db=db, force=False
            )
            logger.info("Auto-compaction finished for %s: %s", project_id, result)
    except Exception as e:  # noqa: BLE001
        logger.warning("Auto-compaction failed (project %s): %s", project_id, e)
    finally:
        _COMPACT_IN_FLIGHT.discard(project_id)
