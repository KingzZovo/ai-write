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
    return True, summary
