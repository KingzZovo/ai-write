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

from app.services.outline_authority import outline_for_generation, TEXT_REVISION

import json
from copy import deepcopy
import logging
from typing import Any

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, Outline, Volume, Project
from app.services.narrative_contract import WORLD_LOGIC_CONTRACT
from app.services.outline_generator import OUTLINE_CHAPTER_CONTRACT_PROMPT, merge_chapter_outline_enrichment
from app.services.outline_contract import CHAPTER_CONTRACT_FIELDS, render_contract_value, chapter_scope_directive

logger = logging.getLogger(__name__)

DEFAULT_TASK_TYPE = "outline_chapter"
MAX_PREV_TEXT_CHARS = 1500


class ChapterOutlineExpandError(RuntimeError):
    """扩写失败，aller 能选择保留原拷贝或返回 5xx。"""


SYSTEM_PROMPT = (
    "你是一位高质量中文小说大纲作者。你要输出严格符合 schema 的章节大纲扩写，\n"
    "只输出 JSON 本身，不要 markdown 代码块，不要任何辅助说明文本。\n"
    "以中文完成。必要时 \"\\n\" 表示换行。\n"
    "保留已定人物关系、物品归属和剧情边界；可补足相容的日常动作，不新增改变主线的角色、设定或关键道具。\n"
    "【title 冻结】本章 title 已在分卷大纲阶段按作者命名风格确定并写入 stub.title，请直接原样复制到输出的 title 字段，不要修改、不要润色、不要重新生成。\n"
    "章末按既定剧情收束，有后章时自然衔接，全书终章明确完结，不强留悬念。\n"
    + WORLD_LOGIC_CONTRACT
    + OUTLINE_CHAPTER_CONTRACT_PROMPT
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

请在 stub 基础上写一份本章「过程性详细大纲」：summary 应按本章篇幅清楚交代过程，去修辞、去环境描写与心理渲染，但过程不能丢：人物进场顺序、关键对话要点、动作与结果、状态转折、到头状态都要讲清楚。key_events / state_changes / foreshadows 是 summary 的结构化索引，不是 summary 本身。不要进入正文描写，但也不要只写关键词清单。

输出 schema (JSON 严格遵守)：
{{
  "chapter_idx": {chapter_idx},
  "title": "原样复制 stub.title，保持已确认标题",
  "summary": "本章过程性叙事。去修辞但过程完整。含：进入状态·中段关键场景与对话要点·转折·到头状态。例如既定计划为交还修好的家谱，应交代剩余修补、交付核对、客户接走与桌上剩下的物品，不另添秘密或意外。\\n 表示换行。",
  "key_events": ["事件 1", "事件 2", "按实际事件列出，作为 summary 的结构索引。"],
  "prev_chapter_threads": ["本章需接住的上章未完样动作 / 冲突 / 悬念 1", "…"],
  "state_changes": {{
    "characters": [{{"name": "人物名", "change": "本章末尾该人物状态的变化说明"}}],
    "items": [{{"name": "道具/关键物件", "change": "本章末尾该物件状态变化说明"}}],
    "relationships": [{{"from": "A", "to": "B", "change": "本章末尾两人关系变化说明"}}]
  }},
  "foreshadows_planted": [
    {{"description": "本章埋下的伏笔描述", "resolve_conditions": "未来某章兑现条件"}}
  ],
  "foreshadows_resolved": ["本章兑现之前某伏笔的描述"],
  "next_chapter_hook": "有下章时写具体衔接；全书终章写明完结与收束，不新造钩子。",
  "start_state": "本章开头承接上一章的时间、空间、人物、物件、信息和冲突状态",
  "end_state": "本章结束时实际交付给下一章的状态",
  "time_delta": "距离上一章/上一关键场景过去多久；哪些流程、移动、准备消耗了时间",
  "location_path": "关键人物/物件/消息/风险如何从上一地点到达本章地点；无移动写同地承接",
  "entity_transfers": "关键人物、物件、资源、消息等如何到场/转移/留置",
  "information_state": "本章开始和结束时各方已知/未知/误解的信息，以及证据强度",
  "power_resource_map": "本章人物实际可用的时间、工具、关系与限制，不强设敌我或代价",
  "mechanism_limits": "本章使用的能力/技术/制度工具/证据/资源调用的条件、成本、边界、反制",
  "result_strength": "本章行动和结果的依据；已充分铺垫的结果可以完成",
  "handoff_to_next": "本章如何把时间、空间、人物、物件、信息状态交接给下一章",
  "transition_bridge": "有后章时写实际需要的时间、移动、物件与信息交接；终章说明收束，没有未解决风险时不强造风险。",
  "action_budget": "本章如有高压动作，说明可用时间、身体限制与必要准备；没有时写无，不为填字段新增追捕或对质",
  "inference_ledger": "本章如有关键推断，说明人物感知或证据能支持什么；没有时写无，不为填字段制造疑案"
}}

额外要求：
- 首章时 prev_chapter_threads 可为 [] 且 prev_outline / prev_text 会为空提示。
- foreshadows 可以为 [] 但不能缺字段。
- summary 详略应适合本章实际篇幅以使后续章节只看 summary 也能复述本章过程。key_events 是 summary 的结构化骨架，不能代替 summary。
- summary 中不要出现「如上所述」「总之」「本章讲了 X」这类总结性词·直接叙述过程。
- 不要输出多余说明文字、markdown 标题或代码围栏。
"""


async def _gather_context(
    db: AsyncSession,
    chapter: Chapter,
    *, lock: bool = False,
) -> dict[str, Any]:
    """从 DB 拉取全书 / 本卷 / 上章上下文。返回 raw 字典，未序列化。"""
    def source_query(stmt):
        stmt = stmt.execution_options(populate_existing=True)
        return stmt.with_for_update() if lock else stmt

    def source_values(row):
        return {column.key: getattr(row, column.key) for column in row.__table__.columns} if row else None

    # 所属卷
    vol = await db.scalar(source_query(select(Volume).where(Volume.id == chapter.volume_id)))
    if vol is None:
        raise ChapterOutlineExpandError(f"chapter {chapter.id} 所属卷不存在")
    project_id = vol.project_id
    from app.services.chapter_target_words import resolve_chapter_target_word_count
    project = await db.scalar(source_query(select(Project).where(Project.id == project_id)))
    settings = project.settings_json if project and isinstance(project.settings_json, dict) else {}
    target_word_count = resolve_chapter_target_word_count(chapter.target_word_count, settings.get("target_chapter_words"))

    # 全书大纲：最新已确认版本优先，否则使用最新版本。
    book_q = await db.execute(
        source_query(select(Outline)
        .where(Outline.project_id == project_id, Outline.level == "book")
        .order_by(Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1))
    )
    book_outline_row = book_q.scalars().first()
    book_cj = outline_for_generation((book_outline_row.content_json if book_outline_row else None) or {})

    # Stable volume identity wins over legacy numeric metadata.
    from app.services.outline_order import volume_condition, chapter_stub, bind_chapter_content
    volume_outline = await db.scalar(source_query(select(Outline).where(
        Outline.project_id == project_id, Outline.level == "volume",
        volume_condition(vol.id, vol.volume_idx)
    ).order_by(Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1)))
    volume_content = (volume_outline.content_json if volume_outline else None) or {}
    volume_outline_cj = outline_for_generation(volume_content)
    if volume_outline_cj:
        volume_outline_cj['volume_idx'] = vol.volume_idx

    # Prefer the chapter's own plan; only an explicit unambiguous association
    # can supply a volume stub. Never infer identity from an array position.
    stub: dict[str, Any] = {}
    if isinstance(chapter.outline_json, dict) and chapter.outline_json:
        stub = outline_for_generation(bind_chapter_content(chapter.outline_json, chapter))
    elif not volume_content.get(TEXT_REVISION):
        stub = outline_for_generation(chapter_stub(volume_content, chapter))
    if not stub:
        # 最后的兒后章仅以章号+标题构造一个最小 stub，避免 LLM 丢字段。
        stub = {
            "chapter_idx": chapter.chapter_idx,
            "title": chapter.title or "",
            "summary": "",
            "key_events": [],
        }

    # Immediate predecessor in book order, including the previous volume.
    prev_chapter = await db.scalar(source_query(select(Chapter).join(Volume, Chapter.volume_id == Volume.id).where(
        Volume.project_id == project_id,
        tuple_(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
        < (vol.volume_idx, vol.id, chapter.chapter_idx, chapter.id)
    ).order_by(Volume.volume_idx.desc(), Volume.id.desc(), Chapter.chapter_idx.desc(), Chapter.id.desc()).limit(1)))
    prev_outline: dict[str, Any] = {}
    prev_text = ""
    if prev_chapter is not None:
        if isinstance(prev_chapter.outline_json, dict):
            prev_outline = outline_for_generation(bind_chapter_content(prev_chapter.outline_json, prev_chapter))
        text = prev_chapter.content_text or ""
        if text:
            prev_text = text[-MAX_PREV_TEXT_CHARS:]

    # The persisted target and current outline sources own chronology. A
    # failed read cannot silently replace these references with an empty set.
    from app.services.foreshadow_lifecycle import load_active_foreshadows_for_context
    active_foreshadows = await load_active_foreshadows_for_context(
        db, project_id, chapter.global_idx, limit=20,
    )

    return {
        "_sources": {"project": source_values(project), "volume": source_values(vol),
                     "book": source_values(book_outline_row), "volume_outline": source_values(volume_outline),
                     "previous": source_values(prev_chapter)},
        "target_word_count": target_word_count,
        "book_outline": book_cj,
        "volume_outline": volume_outline_cj,
        "stub": stub,
        "prev_outline": prev_outline,
        "prev_text": prev_text,
        "project_id": str(project_id),
        "active_foreshadows": active_foreshadows,
    }


def _format_user_prompt(ctx: dict[str, Any], chapter: Chapter) -> str:
    def _dump(o: Any) -> str:
        if not o:
            return "（无）"
        try:
            return json.dumps(outline_for_generation(o), ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(o)

    base = USER_PROMPT_TMPL.format(
        book_outline=_dump(ctx["book_outline"]),
        volume_outline=_dump(ctx["volume_outline"]),
        prev_outline=_dump(ctx["prev_outline"]),
        prev_text=ctx["prev_text"] or "（无）",
        stub=_dump(ctx["stub"]),
        chapter_idx=chapter.chapter_idx,
    )
    base += "\n\n" + chapter_scope_directive(ctx["book_outline"], ctx["volume_outline"],
                                               chapter.chapter_idx, ctx.get("target_word_count", getattr(chapter, "target_word_count", None)))
    # PR-FORESHADOW-LIFECYCLE: append active foreshadows directive.
    # NOTE: prior implementation appended into an undefined ``lines`` list
    # whose NameError was silently swallowed by the broad except — so the
    # foreshadow block never reached the prompt. Fixed: append to the
    # rendered string instead.
    try:
        af = ctx.get("active_foreshadows") or []
        if af:
            from app.services.foreshadow_lifecycle import format_active_foreshadows_for_prompt
            af_block = format_active_foreshadows_for_prompt(af)
            if af_block:
                base = base + "\n\n" + af_block
    except Exception:
        pass
    return base


def _validate_expansion_payload(parsed: dict) -> None:
    if not isinstance(parsed, dict) or parsed.get("parse_error") or parsed.get("_parse_error"):
        raise ValueError("章节细纲输出解析失败")
    if not isinstance(parsed.get("summary"), str) or not parsed["summary"].strip():
        raise ValueError("章节细纲缺少完整的过程梗概 summary")
    events = parsed.get("key_events")
    if not isinstance(events, list) or not events or not all(isinstance(event, (str, dict)) and render_contract_value(event) for event in events):
        raise ValueError("章节细纲缺少有效关键事件 key_events")
    for key in ("prev_chapter_threads", "foreshadows_planted", "foreshadows_resolved"):
        if key in parsed and not isinstance(parsed[key], list):
            raise ValueError(f"章节细纲 {key} 必须为数组")
    if "state_changes" in parsed:
        if not isinstance(parsed["state_changes"], dict):
            raise ValueError("章节细纲 state_changes 必须为对象")
        for key in ("characters", "items", "relationships"):
            if key not in parsed["state_changes"]:
                continue
            changes = parsed["state_changes"][key]
            if not isinstance(changes, list):
                raise ValueError(f"章节细纲 state_changes.{key} 必须为数组")
            required = ("from", "to", "change") if key == "relationships" else ("name", "change")
            if any(not isinstance(item, dict) or any(
                not isinstance(item.get(field), str) or not item[field].strip()
                for field in required
            ) for item in changes):
                raise ValueError(f"章节细纲 state_changes.{key} 条目缺少有效字段：{', '.join(required)}")


def _validate_and_normalize(parsed: Any, chapter: Chapter, stub: dict, *, target_word_count=None) -> dict[str, Any]:
    """Reject failed/thin payloads; retain existing obligations and identity."""
    try:
        _validate_expansion_payload(parsed)
    except ValueError as exc:
        raise ChapterOutlineExpandError(str(exc)) from exc
    stub = outline_for_generation(stub)
    out = merge_chapter_outline_enrichment(stub, parsed)
    # A validated new structured expansion supersedes the old free-text draft.
    out.pop("raw_text", None)
    out.pop(TEXT_REVISION, None)
    out["chapter_idx"] = chapter.chapter_idx
    out["title"] = chapter.title or stub.get("title") or parsed.get("title") or ""
    for key in ("prev_chapter_threads", "foreshadows_planted", "foreshadows_resolved"):
        if not isinstance(out.get(key), list):
            out[key] = []
    prior_changes = stub.get("state_changes") if isinstance(stub.get("state_changes"), dict) else {}
    new_changes = parsed.get("state_changes") or {}
    out["state_changes"] = {
        key: (new_changes.get(key) if isinstance(new_changes.get(key), list) and new_changes[key]
              else prior_changes.get(key) if isinstance(prior_changes.get(key), list) else [])
        for key in ("characters", "items", "relationships")
    }
    target = target_word_count if target_word_count is not None else getattr(chapter, "target_word_count", None)
    if type(target) is int and target > 0:
        out["word_count_target"] = target
    out["next_chapter_hook"] = render_contract_value(out.get("next_chapter_hook"))
    for key in CHAPTER_CONTRACT_FIELDS:
        out[key] = render_contract_value(parsed.get(key)) or render_contract_value(stub.get(key))
    if not out["transition_bridge"]:
        out["transition_bridge"] = out["handoff_to_next"] or out["end_state"] or out["next_chapter_hook"]
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

    ctx = deepcopy(await _gather_context(db, chapter))
    if str(ctx["project_id"]) != str(project_id):
        raise ChapterOutlineExpandError(
            f"chapter {chapter_id} 不属于 project {project_id}"
        )

    user_prompt = _format_user_prompt(ctx, chapter)
    expected_version = chapter.updated_at
    expected_volume_id = chapter.volume_id

    # Do not hold the request transaction open while the LLM call runs.
    # Chapter outline expansion can take several minutes; Postgres kills
    # idle-in-transaction connections before the model returns, so the later
    # UPDATE used to fail with "underlying connection is closed". Release the
    # read transaction after context assembly, then open a fresh transaction
    # only for the final write.
    await db.rollback()

    from app.services.prompt_registry import run_structured_prompt
    parsed = await run_structured_prompt(
        task_type, user_prompt, db, extra_system=SYSTEM_PROMPT,
        project_id=str(project_id), chapter_id=str(chapter_id),
        output_validator=_validate_expansion_payload,
    )
    # A cold registry lookup may have opened a read transaction. End it before
    # reloading the latest chapter and checking the author's version.
    await db.rollback()
    project = await db.scalar(select(Project).where(Project.id == project_id)
                              .with_for_update().execution_options(populate_existing=True))
    if project is None or project.deleted_at is not None:
        raise ChapterOutlineExpandError("作品不存在或已删除，已停止写入")
    volume = await db.scalar(select(Volume).where(Volume.id == expected_volume_id)
                             .with_for_update().execution_options(populate_existing=True))
    if volume is None or str(volume.project_id) != str(project_id):
        raise ChapterOutlineExpandError("章节所属作品已变更，已停止写入")
    chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id)
                              .with_for_update().execution_options(populate_existing=True))
    if chapter is None:
        raise ChapterOutlineExpandError(f"chapter {chapter_id} 不存在")
    if chapter.volume_id != expected_volume_id:
        raise ChapterOutlineExpandError("章节所属作品已变更，已停止写入")
    if chapter.updated_at != expected_version:
        raise ChapterOutlineExpandError("章节在扩写期间已被修改，已保留作者当前版本")
    # Lock existing outline candidates so confirmation/version changes cannot
    # supersede the selected source between re-read and commit. The project
    # lock prevents insertion of new outline candidates during this phase.
    await db.execute(select(Outline.id).where(Outline.project_id == project.id,
        Outline.level.in_(("book", "volume"))).order_by(Outline.id).with_for_update())
    current_ctx = await _gather_context(db, chapter, lock=True)
    if current_ctx != ctx:
        raise ChapterOutlineExpandError("扩写期间书纲、分卷、预算或前章已修改，已保留作者当前版本")
    normalized = _validate_and_normalize(parsed, chapter, ctx["stub"], target_word_count=ctx.get("target_word_count"))

    # 写回 DB。
    from app.services.outline_order import bind_chapter_content
    normalized = bind_chapter_content(normalized, chapter)
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
