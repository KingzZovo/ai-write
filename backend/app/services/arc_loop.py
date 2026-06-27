"""弧式增量创作循环（子项目 A）。

哲学：从点子出发，一弧（≈20 章一段连贯故事）一弧地写，绝不预先规划几百几千
章的大伏笔。弧物理复用 Volume；弧状态寄存 volume-level Outline.content_json
的 _arc 命名空间（零迁移）。章节正文生成复用 B 的 run_chapter_pipeline。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ARC_STATUS_ACTIVE = "active"               # 可继续写下一章
ARC_STATUS_AWAITING = "awaiting_direction"  # 等作者给下一章方向
ARC_STATUS_COMPLETED = "completed"          # 本弧写满，等开下一弧

_DEFAULT_TARGET_CHAPTERS = 20
_MIN_TARGET = 4
_MAX_TARGET = 40


def clamp_target_chapters(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_TARGET_CHAPTERS
    return max(_MIN_TARGET, min(_MAX_TARGET, n))


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
    return ArcState(
        title=str(arc.get("title") or ""),
        core_setup=str(arc.get("core_setup") or ""),
        opening_scene=str(arc.get("opening_scene") or ""),
        target_chapters=clamp_target_chapters(arc.get("target_chapters")),
        status=str(arc.get("status") or ARC_STATUS_ACTIVE),
        chapters_written=int(arc.get("chapters_written") or 0),
        running_outline=str(arc.get("running_outline") or ""),
        next_direction=arc.get("next_direction"),
        suggestions=list(arc.get("suggestions") or []),
    )


def serialize_arc_state(state: ArcState, *, volume_idx: int) -> dict:
    """组装回 content_json（含 volume_idx 供前端分卷映射 + _arc 命名空间）。"""
    return {
        "volume_idx": volume_idx,
        "_arc": {
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
    }


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
