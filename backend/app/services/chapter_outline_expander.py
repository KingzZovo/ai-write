"""PR-OUTLINE-DEEPDIVE Phase 1 · chapter outline expander (2026-05-04).

Why
---
在本 PR 之前，`Chapter.outline_json` 仅仅是分卷大纲中于本章位置的
``chapter_summaries[i]`` 直接拷贝（4 个字段：``chapter_idx / title /
summary / key_events``）。生成路径：

  - api/volumes.py:313 · 创建分卷时 ``Chapter(outline_json=cs)``
  - api/outlines.py:142 · PR-OL9 级联同步 ``ch.outline_json = cs``

不含任何 LLM 扩写。用户需要章节大纲是分卷大纲的「扩写」，含跳板字段
(全本记忆)：

  - prev_chapter_threads (上章余波接续)
  - state_changes (人物 / 道具 / 关系状态变化)
  - foreshadows_planted / foreshadows_resolved (本章埋 / 兑伏笔)
  - next_chapter_hook (下章钩子)

本模块仅提供「按一章扩写」主函数。Celery 包装 + 按卷/按全书批量由
Phase 1.5 / Phase 2 担责。本函数为 async、可被 API 路由同步调用。

Output contract
---------------
返回一个可以直接当 ``chapter.outline_json`` 底床定义的 dict：

```python
{
  # 现有字段（必填）
  "chapter_idx": int,
  "title": str,
  "summary": str,
  "key_events": list[str],
  # PR-OUTLINE-DEEPDIVE 新增字段（跳板资产）
  "prev_chapter_threads": list[str],
  "state_changes": {
    "characters": list[{"name": str, "change": str}],
    "items": list[{"name": str, "change": str}],
    "relationships": list[{"from": str, "to": str, "change": str}],
  },
  "foreshadows_planted": list[{"description": str, "resolve_conditions": str}],
  "foreshadows_resolved": list[str],
  "next_chapter_hook": str,
}
```

调用例
-----
```python
from app.services.chapter_outline_expander import expand_chapter_outline
result = await expand_chapter_outline(project_id, chapter_id, db)
```

Response caller responsibilities
--------------------------------
- API 路由调用后不要在 caller 边存 chapter （本模块已代以 db.flush）。
- LLM 调用失败会抛 ``ChapterOutlineExpandError``，caller 需 try/except。
- 默认 task_type 为 "outline_chapter"（复用现有 prompt route）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, Outline, Volume

logger = logging.getLogger(__name__)

DEFAULT_TASK_TYPE = "outline_chapter"
MAX_PREV_TEXT_CHARS = 1500


class ChapterOutlineExpandError(RuntimeError):
    """扩写失败，aller 能选择保留原拷贝或返回 5xx。"""


SYSTEM_PROMPT = (
    "你是一位高质量中文小说大纲作者。你要输出严格符合 schema 的章节大纲扩写，\n"
    "只输出 JSON 本身，不要 markdown 代码块，不要任何辅助说明文本。\n"
    "以中文完成。必要时 \"\\n\" 表示换行。\n"
    "不要创作未在上下文中出现的人物、设定、道具，除非 stub 中明确提示。"
)


USER_PROMPT_TMPL = """\
【全书大纲】
{book_outline}

【本卷大纲】
{volume_outline}

【上一章的大纲】
{prev_outline}

【上一章近文（末尾片段）】
{prev_text}

【本章 stub（来自分卷大纲）】
{stub}

请遵忪以下要求写一份本章的详细大纲，在 stub 基础上扩写，增加跳板资产。不要进入场景化描写 — 仅提供提纲、状态、伏笔、关键事件。

输出 schema (JSON 严格遵守)：
{{
  "chapter_idx": {chapter_idx},
  "title": "本章标题（8–3 字，不同于卷名）",
  "summary": "本章主事件 30–70 字（陈述句）",
  "key_events": ["事件 1", "事件 2", "… 3-6 条。2 条以上。"],
  "prev_chapter_threads": ["本章需接住的上章未完样动作 / 冲突 / 悬念 1", "…"],
  "state_changes": {{
    "characters": ["name": "人物名", "change": "本章末尾未该人物状态的变化说明"],
    "items": ["name": "道具/关键物件", "change": "本章末尾该物件状态变化说明"],
    "relationships": ["from": "A", "to": "B", "change": "本章末尾两人关系变化说明"]
  }},
  "foreshadows_planted": [
    "description": "本章埋下的伏笔描述", "resolve_conditions": "未来某章兑现条件"
  ],
  "foreshadows_resolved": ["本章兑现之前某伏笔的描述"],
  "next_chapter_hook": "本章末尾留给下章的明确动作 · 冲突 · 问题。不可为空。"
}}

额外要求：
- 首章时 prev_chapter_threads 可为 [] 且 prev_outline / prev_text 会为空提示。
- foreshadows 可以为 [] 但不能缺字段。
- key_events 与 summary 要可被后续正文生成者直接译为场景。
- 不要输出多余说明文字、markdown 标题或代码围栏。
"""


async def _gather_context(
    db: AsyncSession,
    chapter: Chapter,
) -> dict[str, Any]:
    """从 DB 拉取全书 / 本卷 / 上章上下文。返回 raw 字典，未序列化。"""
    # 所属卷
    vol = await db.get(Volume, chapter.volume_id)
    if vol is None:
        raise ChapterOutlineExpandError(f"chapter {chapter.id} 所属卷不存在")
    project_id = vol.project_id

    # 全书大纲（选任一个 confirmed，其次最早 book 级）
    book_q = await db.execute(
        select(Outline)
        .where(Outline.project_id == project_id, Outline.level == "book")
        .order_by(Outline.is_confirmed.desc(), Outline.created_at.asc())
    )
    book_outline_row = book_q.scalars().first()
    book_cj = (book_outline_row.content_json if book_outline_row else None) or {}

    # 本卷大纲（以 volume_idx 匹配）
    vol_q = await db.execute(
        select(Outline)
        .where(Outline.project_id == project_id, Outline.level == "volume")
        .order_by(Outline.created_at.desc())
    )
    volume_outline_cj: dict[str, Any] = {}
    chapter_summaries: list[dict] = []
    for o in vol_q.scalars().all():
        cj = o.content_json or {}
        if isinstance(cj, dict) and cj.get("volume_idx") == vol.volume_idx:
            volume_outline_cj = cj
            cs = cj.get("chapter_summaries")
            if isinstance(cs, list):
                chapter_summaries = cs
            break

    # 本章 stub。优先 chapter.outline_json，后调卷大纲 chapter_summaries[i]。
    stub: dict[str, Any] = {}
    if isinstance(chapter.outline_json, dict) and chapter.outline_json:
        stub = dict(chapter.outline_json)
    elif chapter_summaries:
        idx = chapter.chapter_idx
        for cs in chapter_summaries:
            if isinstance(cs, dict) and cs.get("chapter_idx") == idx:
                stub = dict(cs)
                break
        else:
            # fallback by order
            if 0 < idx <= len(chapter_summaries) and isinstance(chapter_summaries[idx - 1], dict):
                stub = dict(chapter_summaries[idx - 1])
    if not stub:
        # 最后的兒后章仅以章号+标题构造一个最小 stub，避免 LLM 丢字段。
        stub = {
            "chapter_idx": chapter.chapter_idx,
            "title": chapter.title or "",
            "summary": (chapter.summary or "")[:120],
            "key_events": [],
        }

    # 上一章上下文。如果为首章则为空。
    prev_chapter_q = await db.execute(
        select(Chapter)
        .where(
            Chapter.volume_id == chapter.volume_id,
            Chapter.chapter_idx == chapter.chapter_idx - 1,
        )
    )
    prev_chapter = prev_chapter_q.scalars().first()
    prev_outline: dict[str, Any] = {}
    prev_text = ""
    if prev_chapter is not None:
        if isinstance(prev_chapter.outline_json, dict):
            prev_outline = prev_chapter.outline_json
        text = prev_chapter.content_text or ""
        if text:
            prev_text = text[-MAX_PREV_TEXT_CHARS:]

    return {
        "book_outline": book_cj,
        "volume_outline": volume_outline_cj,
        "stub": stub,
        "prev_outline": prev_outline,
        "prev_text": prev_text,
        "project_id": str(project_id),
    }


def _format_user_prompt(ctx: dict[str, Any], chapter: Chapter) -> str:
    def _dump(o: Any) -> str:
        if not o:
            return "（无）"
        try:
            return json.dumps(o, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(o)
    return USER_PROMPT_TMPL.format(
        book_outline=_dump(ctx["book_outline"]),
        volume_outline=_dump(ctx["volume_outline"]),
        prev_outline=_dump(ctx["prev_outline"]),
        prev_text=ctx["prev_text"] or "（无）",
        stub=_dump(ctx["stub"]),
        chapter_idx=chapter.chapter_idx,
    )


def _validate_and_normalize(parsed: Any, chapter: Chapter, stub: dict) -> dict[str, Any]:
    """轻量校验 + 补默认。LLM 输出常见缺字段以默认填补。"""
    if not isinstance(parsed, dict):
        raise ChapterOutlineExpandError("LLM 输出不是合法 JSON")

    out: dict[str, Any] = {}
    out["chapter_idx"] = parsed.get("chapter_idx") or chapter.chapter_idx
    out["title"] = parsed.get("title") or chapter.title or stub.get("title") or ""
    out["summary"] = parsed.get("summary") or stub.get("summary") or ""
    ke = parsed.get("key_events")
    out["key_events"] = ke if isinstance(ke, list) else (stub.get("key_events") or [])

    pct = parsed.get("prev_chapter_threads")
    out["prev_chapter_threads"] = pct if isinstance(pct, list) else []

    sc = parsed.get("state_changes") if isinstance(parsed.get("state_changes"), dict) else {}
    out["state_changes"] = {
        "characters": sc.get("characters") if isinstance(sc.get("characters"), list) else [],
        "items": sc.get("items") if isinstance(sc.get("items"), list) else [],
        "relationships": sc.get("relationships") if isinstance(sc.get("relationships"), list) else [],
    }

    fp = parsed.get("foreshadows_planted")
    out["foreshadows_planted"] = fp if isinstance(fp, list) else []
    fr = parsed.get("foreshadows_resolved")
    out["foreshadows_resolved"] = fr if isinstance(fr, list) else []

    nch = parsed.get("next_chapter_hook")
    out["next_chapter_hook"] = nch if isinstance(nch, str) else ""

    return out


async def expand_chapter_outline(
    project_id: str,
    chapter_id: str,
    db: AsyncSession,
    *,
    task_type: str = DEFAULT_TASK_TYPE,
) -> dict[str, Any]:
    """为指定章节调一次 LLM 扩写大纲。

    Returns the new ``outline_json`` dict that has already been written into
    ``chapter.outline_json`` (and ``db.flush``过).
    """
    chapter = await db.get(Chapter, chapter_id)
    if chapter is None:
        raise ChapterOutlineExpandError(f"chapter {chapter_id} 不存在")

    ctx = await _gather_context(db, chapter)
    if str(ctx["project_id"]) != str(project_id):
        raise ChapterOutlineExpandError(
            f"chapter {chapter_id} 不属于 project {project_id}"
        )

    user_prompt = _format_user_prompt(ctx, chapter)

    # 须在函数内延迟导入，避免 backend 启动时交叉依赖。
    from app.services.model_router import get_model_router
    from app.services.outline_generator import OutlineGenerator

    router = get_model_router()
    log_meta = {"project_id": str(project_id), "task_type": task_type}
    result = await router.generate(
        task_type=task_type,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        _log_meta=log_meta,
    )
    raw_text = getattr(result, "text", None) or ""

    # 复用 outline_generator 的宽松 JSON 解析（除 markdown / 其他顶层控制字符）。
    parsed = OutlineGenerator()._parse_json(raw_text)
    normalized = _validate_and_normalize(parsed, chapter, ctx["stub"])

    # 写回 DB。
    chapter.outline_json = normalized
    await db.flush()
    await db.refresh(chapter)

    logger.info(
        "chapter outline expanded · project=%s chapter=%s idx=%s key_events=%d",
        project_id,
        chapter_id,
        chapter.chapter_idx,
        len(normalized.get("key_events") or []),
    )
    return normalized
