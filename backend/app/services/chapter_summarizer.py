"""v1.7.4 P0-2: chapter post-generation summarizer.

Problem (pre-v1.7.4): Chapter.summary stays NULL forever after generation.
This means ContextPack._build_proximity has empty `recent_summaries`, so the
next chapter is generated with no memory of what just happened in the previous
chapter. Drift compounds as the volume progresses.

Fix: After a chapter is persisted, run a tight summarize call and write the
result back to chapter.summary. Synchronous helper used by the celery task
`tasks.summarize_chapter` and by manual backfill scripts.

The summary target is 80-160 chars Chinese, present-tense, key-events only,
no meta-commentary. We use prompt_assets.task_type='summary' which already
exists in the DB.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter

logger = logging.getLogger(__name__)

# Cap input to keep cost predictable; summaries don't need the entire 8k chapter.
_MAX_INPUT_CHARS = 6000
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
    """Strip markdown fences and JSON wrappers; return clean prose.

    Handles:
      - ```json\n{...}\n``` fences (any language tag)
      - bare JSON like {"summary": "..."}
      - leading/trailing quotes / backticks / whitespace
      - multi-paragraph: keep first non-empty paragraph after cleaning
    """
    import json as _json
    import re as _re
    s = (raw or "").strip()
    if not s:
        return ""
    # 1. Strip outer markdown code fences (```json ... ``` or ``` ... ```).
    fence_re = _re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*$", _re.DOTALL)
    m = fence_re.match(s)
    if m:
        s = m.group(1).strip()
    # 2. If it now looks like a JSON object, try to extract 'summary' field.
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = _json.loads(s)
            if isinstance(obj, dict):
                for k in ("summary", "摘要", "总结", "text", "content"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        s = v.strip()
                        break
        except Exception:
            # Sometimes the model writes pseudo-json like {\n  "summary": "...\n} that
            # is invalid (bad newline escapes inside the value). Fallback to a
            # regex extract of the summary field.
            m2 = _re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', s, _re.DOTALL)
            if m2:
                s = m2.group(1).encode().decode("unicode_escape", errors="ignore").strip()
    # 3. Strip residual fences/quotes the previous steps may have missed.
    s = s.strip()
    for prefix in ("```json", "```", '"', "'"):
        if s.startswith(prefix):
            s = s[len(prefix):].lstrip()
    for suffix in ("```", '"', "'"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
    # 4. Multi-paragraph collapse: keep first non-empty paragraph.
    parts = [p.strip() for p in s.split("\n\n")]
    parts = [p for p in parts if p]
    if parts:
        s = parts[0]
    return s.strip()


async def summarize_chapter_text(
    *,
    title: str,
    content_text: str,
    db: AsyncSession,
    project_id: Any = None,
    chapter_id: Any = None,
) -> str:
    """Generate a tight 80-160 char summary for a chapter.

    Returns empty string on any failure rather than raising, so the chapter
    persistence path is never broken by summarizer trouble.
    """
    if not content_text or len(content_text) < _MIN_INPUT_CHARS:
        return ""
    text = content_text
    if len(text) > _MAX_INPUT_CHARS:
        # Keep head + tail so the summary captures opening hook AND ending beat.
        text = text[: int(_MAX_INPUT_CHARS * 0.7)] + "\n\n…(中部省略)…\n\n" + text[-int(_MAX_INPUT_CHARS * 0.3):]
    user = _USER_TEMPLATE.format(title=title or "", content=text)
    try:
        from app.services.prompt_registry import run_text_prompt
        result = await run_text_prompt(
            task_type="summary",
            user_content=user,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
        out = (result.text or "").strip()
        # Defensive multi-stage cleaning. The summary prompt is supposed to
        # produce plain Chinese, but real-world models still return markdown
        # code fences and/or JSON wrappers. We never want those polluting
        # ContextPack.recent_summaries, so unwrap aggressively.
        out = _clean_summary_output(out)
        if len(out) > 220:
            out = out[:220].rstrip() + "…"
        return out
    except Exception as e:
        logger.warning("summarize_chapter_text failed (chapter_id=%s): %s", chapter_id, e)
        return ""


async def summarize_and_save_chapter(
    *,
    chapter_id: str | UUID,
    db: AsyncSession,
    overwrite: bool = False,
) -> tuple[bool, str]:
    """Fetch chapter by id, summarize, write back to chapter.summary.

    Returns (was_written, summary_text).
    Used by the celery task and by manual backfill scripts.
    """
    chapter = await db.get(Chapter, str(chapter_id))
    if chapter is None:
        return False, ""
    if not chapter.content_text or len(chapter.content_text) < _MIN_INPUT_CHARS:
        return False, ""
    if chapter.summary and not overwrite:
        return False, chapter.summary

    project_id = None
    try:
        from app.models.project import Volume
        if chapter.volume_id is not None:
            volume = await db.get(Volume, str(chapter.volume_id))
            if volume is not None:
                project_id = volume.project_id
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
    chapter.summary = summary
    await db.commit()

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
    )
    return True, summary


async def upsert_chapter_summary_embedding(
    *,
    project_id: Any,
    volume_id: Any,
    chapter_idx: int | None,
    chapter_title: str,
    summary: str,
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
