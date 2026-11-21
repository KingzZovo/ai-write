"""弧式增量创作循环（子项目 A）。

从点子出发，按当前弧逐段展开长篇；承接全书已有事实和长线，不擅自规划远期终局。
弧物理复用 Volume；_arc 保留原规划材料和旧报告。
正式作者进度由 arc_progress_writes 的追加回执保存。正文生成复用章节流水线。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from copy import deepcopy
import json
from uuid import UUID

from app.services.prompt_registry import run_structured_prompt
from app.services.arc_outline import (
    ARC_PLAN_VERSION, build_arc_outline_prompt, generate_arc_outline, validate_arc_chapter, validate_arc_target,
)
from app.services.arc_author_material import render_arc_author_source
from app.services.outline_authority import outline_for_generation
from app.services.outline_order import REF, UNAVAILABLE
from app.services.prompts.arc_planning import ARC_SCOPE_GUIDANCE

logger = logging.getLogger(__name__)

ARC_STATUS_ACTIVE = "active"               # 可继续写下一章
ARC_STATUS_AWAITING = "awaiting_direction"  # 等作者给下一章方向
ARC_STATUS_COMPLETED = "completed"          # 本弧写满，等开下一弧

_DEFAULT_TARGET_CHAPTERS = 20


@dataclass
class ArcState:
    title: str
    core_setup: str
    opening_scene: str
    target_chapters: int = _DEFAULT_TARGET_CHAPTERS
    status: str = ARC_STATUS_ACTIVE
    chapters_written: int = 0
    running_outline: str = ""
    next_direction: str | None = None
    suggestions: list[str] = field(default_factory=list)


def parse_arc_state(content_json: dict | None) -> ArcState | None:
    """从 volume-level Outline.content_json 取出 _arc；非弧返回 None。"""
    if not isinstance(content_json, dict):
        return None
    arc = content_json.get("_arc")
    if not isinstance(arc, dict) or not arc.get("is_arc"):
        return None
    if arc.get("is_arc") is not True:
        raise ValueError("is_arc 必须为布尔值")
    target = validate_arc_target(arc.get("target_chapters", _DEFAULT_TARGET_CHAPTERS))
    written = arc.get("chapters_written", 0)
    if type(written) is not int or not 0 <= written <= target:
        raise ValueError("已写章数必须在原弧章数范围内，不能自动转换")
    status = arc.get("status", ARC_STATUS_ACTIVE)
    if status not in (ARC_STATUS_ACTIVE, ARC_STATUS_AWAITING, ARC_STATUS_COMPLETED):
        raise ValueError("弧状态无效")
    for key in ("title", "core_setup", "opening_scene", "running_outline"):
        if not isinstance(arc.get(key, ""), str):
            raise ValueError(f"{key} 必须为文字")
    direction = arc.get("next_direction")
    if direction is not None and not isinstance(direction, str):
        raise ValueError("下一步方向必须为文字")
    suggestions = arc.get("suggestions", [])
    if not isinstance(suggestions, list) or any(not isinstance(item, str) for item in suggestions):
        raise ValueError("开场建议必须为文字数组")
    return ArcState(
        title=arc.get("title", ""),
        core_setup=arc.get("core_setup", ""),
        opening_scene=arc.get("opening_scene", ""),
        target_chapters=target,
        status=status,
        chapters_written=written,
        running_outline=arc.get("running_outline", ""),
        next_direction=direction,
        suggestions=list(suggestions),
    )


def serialize_arc_state(state: ArcState, *, volume_idx: int, base: dict | None = None) -> dict:
    """组装回 content_json（含 volume_idx 供前端分卷映射 + _arc 命名空间）。"""
    result = deepcopy(base or {})
    previous_state = result.get("_arc")
    result.update({
        "volume_idx": volume_idx,
        "_arc": {
            **(previous_state if isinstance(previous_state, dict) else {}),
            "is_arc": True,
            "title": state.title,
            "core_setup": state.core_setup,
            "opening_scene": state.opening_scene,
            "target_chapters": state.target_chapters,
            "status": state.status,
            "chapters_written": state.chapters_written,
            "running_outline": state.running_outline,
            "next_direction": state.next_direction,
            "suggestions": list(state.suggestions),
        },
    })
    return result


def advance_arc_state(
    state: ArcState,
    *,
    event: str,
    running_outline_append: str = "",
    next_direction: str | None = None,
) -> ArcState:
    """纯状态机：根据事件推进弧状态（无 LLM、无 IO，可单测）。

    event:
      - "chapter_written": chapters_written+1，追加 running_outline，清 next_direction，
        写满→completed 否则→awaiting_direction。
      - "set_direction": 仅当未 completed 时，写入 next_direction 并回到 active。
    """
    s = ArcState(
        title=state.title,
        core_setup=state.core_setup,
        opening_scene=state.opening_scene,
        target_chapters=state.target_chapters,
        status=state.status,
        chapters_written=state.chapters_written,
        running_outline=state.running_outline,
        next_direction=state.next_direction,
        suggestions=list(state.suggestions),
    )
    if event == "chapter_written":
        s.chapters_written += 1
        if running_outline_append:
            s.running_outline = (
                f"{s.running_outline}\n{running_outline_append}".strip()
                if s.running_outline else running_outline_append.strip()
            )
        s.next_direction = None
        if s.chapters_written >= s.target_chapters:
            s.status = ARC_STATUS_COMPLETED
        else:
            s.status = ARC_STATUS_AWAITING
    elif event == "set_direction":
        if s.status != ARC_STATUS_COMPLETED:
            s.next_direction = next_direction
            s.status = ARC_STATUS_ACTIVE
    return s


async def build_arc_kickoff_questions(
    *, idea: str, background: str, db: object, project_id: object = None,
) -> list[str]:
    """生成补全初始设定的几个问题。失败返回 []（跳过补全，不阻断）。"""
    prompt = (
        "作者要用以下点子和背景开写一部中文小说。请提出至多4个确有必要、"
        "材料尚未说明的问题，帮助作者明确人物所求、当下处境或想写到的结果。"
        "已给定的信息不重复追问，没有必要补充时返回空数组；不强制增加威胁或特殊能力。"
        "只输出 JSON：{\"questions\": [\"...\"]}。\n\n"
        f"【点子】{idea}\n【背景设定】{background}"
    )
    try:
        parsed = await run_structured_prompt(
            "arc_kickoff", prompt, db, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_arc_kickoff_questions failed; skipping: %s", exc)
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
        return [q for q in parsed["questions"] if isinstance(q, str) and q.strip()]
    return []


async def build_arc_completion_suggestions(
    *, background: str, running_outline: str, db: object, project_id: object = None,
) -> list[str]:
    """弧写满后，根据背景设定给几个下一弧的开场建议。失败返回 []。"""
    prompt = (
        "作者准备结束当前弧（一段连贯故事）。请参考背景设定与作者填写的进展，"
        "给作者 3 个「下一段可以怎么走」的开场建议（每个一句话，互不雷同，"
        "保留原有长线，不擅自提前兑现；也可建议自然完结）。建议仍是规划，不是正文事实。只输出 JSON："
        "{\"suggestions\": [\"...\"]}。\n\n"
        f"{ARC_SCOPE_GUIDANCE}\n【背景设定】{background}\n【作者填写的本弧进展，仍须与正文核对】{running_outline}"
    )
    try:
        parsed = await run_structured_prompt(
            "arc_suggest", prompt, db, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_arc_completion_suggestions failed: %s", exc)
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("suggestions"), list):
        return [s for s in parsed["suggestions"] if isinstance(s, str) and s.strip()]
    return []


def require_arc_chapter_order(content, state: ArcState) -> list[str] | None:
    """A reported chapter count cannot silently follow a reordered manuscript.

    New arcs retain their chapter identities. Before the first progress report,
    an explicit author reorder may establish the starting order; after that,
    a change needs the author to reconcile progress. Legacy arcs lack this
    evidence and retain their separate, strictly numbered beat path.
    """
    arc = content.get("_arc") if isinstance(content, dict) else None
    if not isinstance(arc, dict) or ("arc_plan_version" not in content and "chapter_order" not in arc):
        return None
    expected = arc.get("chapter_order")
    if not isinstance(expected, list) or len(expected) != state.target_chapters or any(not isinstance(cid, str) for cid in expected):
        raise ValueError("本弧缺少原章节身份，须先核对进度")
    try:
        if any(str(UUID(cid)) != cid for cid in expected) or len(set(expected)) != len(expected):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("本弧原章节身份无效，不能按章号猜测进度") from None
    entries = content.get("chapter_summaries")
    if not isinstance(entries, list):
        raise ValueError("本弧缺少可核对的章节关联")
    positions = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get(UNAVAILABLE):
            raise ValueError("本弧章节关联已变化，须先核对进度")
        index, reference = entry.get("chapter_idx"), entry.get(REF)
        if type(index) is not int or index in positions or not 1 <= index <= state.target_chapters or not isinstance(reference, str):
            raise ValueError("本弧章节编号或关联无效，须先核对进度")
        positions[index] = reference
    ordered = [positions.get(index) for index in range(1, state.target_chapters + 1)]
    if set(ordered) != set(expected) or (state.chapters_written > 0 and ordered != expected):
        raise ValueError("本弧章节顺序已变化，旧进度不能直接用于续写，请先核对")
    return ordered


def build_next_chapter_brief(state: ArcState, *, arc_beats: list[dict] | None = None,
                             arc_outline: dict | None = None, chapter_idx: int | None = None,
                             chapter_id: str | None = None) -> str:
    """Use the current chapter's complete plan, with explicit legacy handling."""
    validate_arc_target(state.target_chapters)
    if type(state.chapters_written) is not int or state.chapters_written < 0:
        raise ValueError("弧进度无效，不能猜测下一章")
    explicit = chapter_idx is not None or chapter_id is not None
    if explicit and (type(chapter_idx) is not int or chapter_idx < 1 or not chapter_id):
        raise ValueError("当前章缺少明确身份和章号")
    index = chapter_idx if explicit else state.chapters_written + 1
    if (not explicit and index > state.target_chapters) or state.status == ARC_STATUS_COMPLETED:
        raise ValueError("本弧已结束，没有下一章计划")
    if not explicit:
        require_arc_chapter_order(arc_outline, state)
    source = outline_for_generation(arc_outline) if arc_outline is not None else None
    has_author_text = isinstance(source, dict) and isinstance(source.get("raw_text"), str) and source["raw_text"].strip()
    if has_author_text:
        plan = "【作者当前弧大纲】\n" + source["raw_text"]
    elif isinstance(source, dict) and "arc_plan_version" in source:
        if explicit:
            entries = arc_outline.get("chapter_summaries")
            bindings = [entry for entry in entries if isinstance(entry, dict) and entry.get(REF) == chapter_id] if isinstance(entries, list) else []
            if len(bindings) != 1 or bindings[0].get(UNAVAILABLE) or bindings[0].get("chapter_idx") != index:
                raise ValueError("当前章计划与实际章节身份不一致，请核对卷纲")
        if type(source["arc_plan_version"]) is not int or source["arc_plan_version"] != ARC_PLAN_VERSION:
            raise ValueError("弧规划版本无效，不能借用旧版方向")
        if "available" in source and source["available"] is not True:
            raise ValueError("弧规划尚未完整交付")
        chapters = source.get("chapter_summaries")
        if not isinstance(chapters, list):
            raise ValueError("本弧缺少章节计划")
        matches = [item for item in chapters if isinstance(item, dict)
                   and type(item.get("chapter_idx")) is int and item["chapter_idx"] == index]
        if len(matches) != 1:
            raise ValueError("当前章节规划缺失或重复，须先核对大纲")
        chapter = validate_arc_chapter(matches[0], index, generated=False)
        meta = {key: value for key, value in source.items()
                if key not in ("chapter_summaries", "beats", "arc_plan_version", "volume_id", "volume_idx")
                and not key.startswith("_")}
        plan = ("【本弧安排，尚待正文落实】\n" + json.dumps(meta, ensure_ascii=False)
                + f"\n【本章（[CH-{index}]）完整规划，尚待正文落实】\n"
                + json.dumps(chapter, ensure_ascii=False))
    else:
        if explicit:
            raise ValueError("旧版方向没有稳定章节关联，请先核对并编辑本弧大纲")
        beats = source.get("beats") if isinstance(source, dict) else arc_beats
        if not isinstance(beats, list):
            raise ValueError("旧弧大纲缺少当前章节方向")
        if any(not isinstance(item, dict) or type(item.get("chapter")) is not int for item in beats):
            raise ValueError("旧弧章号无效，不能自动转换或借用")
        matches = [item for item in beats if item["chapter"] == index]
        if len(matches) != 1 or not isinstance(matches[0].get("beat"), str) or not matches[0]["beat"].strip():
            raise ValueError("旧弧当前章节方向缺失、重复或不是文字")
        plan = f"【本章（[CH-{index}]）旧版方向，未补写为完整细纲】\n" + matches[0]["beat"]
    author = render_arc_author_source(arc_outline, chapter_id=chapter_id)
    if author.replaces_volume_outline:
        plan = ""
    parts = [f"【本弧】{state.title}", author.text,
             f"【作者填写的本弧进展摘要，仍须与实际正文核对】\n{state.running_outline}" if state.running_outline else "",
             plan,
             f"【作者指定的下一步方向】{state.next_direction}" if state.next_direction else "",
             "请据此写这一章。实际正文决定已经发生的事实；规划只是安排。承接已有长线，本章到期的事写到结果，未到期的事不抢先兑现。"]
    return "\n".join(part for part in parts if part)
