"""弧式增量创作循环（子项目 A）。

哲学：从点子出发，一弧（≈20 章一段连贯故事）一弧地写，绝不预先规划几百几千
章的大伏笔。弧物理复用 Volume；弧状态寄存 volume-level Outline.content_json
的 _arc 命名空间（零迁移）。章节正文生成复用 B 的 run_chapter_pipeline。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.prompt_registry import run_structured_prompt, run_text_prompt

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


_ARC_OUTLINE_CONTRACT = """\
你是网文增量创作的弧规划师。只规划「当前这一段连贯故事」（一个弧，约 {target} 章），
绝不规划几百几千章之后的大伏笔、终局或跨弧悬念。

硬约束：
1. 只规划本弧 {target} 章，每章给一个 beat 方向（一句话）。
2. 禁止埋设超出本弧的长线伏笔、终局铺垫、跨弧悬念。
3. 钩子只做弧内钩子（本弧内能回收）。
4. 大纲是软骨架：作者每章可用新方向改写后续走向，不要写死。

只输出 JSON，不要解释或 Markdown：
{{
  "title": "本弧标题（如：边境小城御敌）",
  "beats": [{{"chapter": 1, "beat": "一句话方向"}}]
}}"""


def build_arc_outline_prompt(
    *,
    idea: str,
    background: str,
    core_setup: str,
    opening_scene: str,
    target_chapters: int,
) -> str:
    contract = _ARC_OUTLINE_CONTRACT.format(target=target_chapters)
    return (
        f"{contract}\n\n"
        f"【点子】{idea}\n"
        f"【背景设定】{background}\n"
        f"【本弧核心设定】{core_setup}\n"
        f"【开场场景】{opening_scene}\n"
        f"【本弧章数】{target_chapters}"
    )


async def generate_arc_outline(
    *,
    idea: str,
    background: str,
    core_setup: str,
    opening_scene: str,
    target_chapters: int,
    db: object,
    project_id: object = None,
) -> dict:
    """生成小弧大纲。失败返回 {"available": False}（调用方回滚，不建半截 Volume）。"""
    prompt = build_arc_outline_prompt(
        idea=idea, background=background, core_setup=core_setup,
        opening_scene=opening_scene, target_chapters=target_chapters,
    )
    try:
        parsed = await run_structured_prompt(
            "arc_outline", prompt, db, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("generate_arc_outline failed; degrading: %s", exc)
        return {"available": False}
    if not isinstance(parsed, dict):
        return {"available": False}
    parsed.setdefault("available", True)
    parsed.setdefault("title", "")
    parsed.setdefault("beats", [])
    return parsed


async def build_arc_kickoff_questions(
    *, idea: str, background: str, db: object, project_id: object = None,
) -> list[str]:
    """生成补全初始设定的几个问题。失败返回 []（跳过补全，不阻断）。"""
    prompt = (
        "作者要用以下点子和背景开写一部网文。请提出 2-4 个最关键的、"
        "补全初始设定必须先问清楚的问题（如金手指、主角动机、当前最大威胁）。"
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
        return [str(q) for q in parsed["questions"] if str(q).strip()]
    return []


async def build_arc_completion_suggestions(
    *, background: str, running_outline: str, db: object, project_id: object = None,
) -> list[str]:
    """弧写满后，根据背景设定给几个下一弧的开场建议。失败返回 []。"""
    prompt = (
        "一个弧（一段连贯故事）刚写完。请根据背景设定与已发生的故事，"
        "给作者 3 个「下一段可以怎么走」的开场建议（每个一句话，互不雷同，"
        "符合已建立的设定，不要剧透式规划长线）。只输出 JSON："
        "{\"suggestions\": [\"...\"]}。\n\n"
        f"【背景设定】{background}\n【本弧已发生】{running_outline}"
    )
    try:
        parsed = await run_structured_prompt(
            "arc_suggest", prompt, db, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_arc_completion_suggestions failed: %s", exc)
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("suggestions"), list):
        return [str(s) for s in parsed["suggestions"] if str(s).strip()]
    return []


def build_next_chapter_brief(state: ArcState, *, arc_beats: list[dict] | None = None) -> str:
    """组装下一章 brief（喂给 /api/generate/chapter 的 chapter_outline 文本）。

    纯函数：本弧标题 + 到目前故事线 + 作者下一步方向 + 本章 beat（若有）。
    """
    next_chapter_idx = state.chapters_written + 1
    beat = ""
    for b in (arc_beats or []):
        try:
            if int(b.get("chapter")) == next_chapter_idx:
                beat = str(b.get("beat") or "")
                break
        except (TypeError, ValueError):
            continue
    parts = [
        f"【本弧】{state.title}",
        f"【本弧到目前的故事线】\n{state.running_outline}" if state.running_outline else "",
        f"【作者指定的下一步方向】{state.next_direction}" if state.next_direction else "",
        f"【本章（[CH-{next_chapter_idx}]）大纲 beat】{beat}" if beat else "",
        "请据此写这一章。保持与上文连贯，不要引入本弧之外的长线伏笔。",
    ]
    return "\n".join(p for p in parts if p)
