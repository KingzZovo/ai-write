"""Complete arc plans in bounded stages, using canonical volume/ chapter data."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import math

from app.services.outline_contract import CHAPTER_CONTRACT_FIELDS
from app.services.prompt_registry import run_structured_prompt
from app.services.prompts.arc_planning import ARC_SCOPE_GUIDANCE

logger = logging.getLogger(__name__)

ARC_PLAN_VERSION = 1
ARC_MIN_CHAPTERS = 4
ARC_MAX_CHAPTERS = 40
ARC_BATCH_SIZE = 5
ARC_PHASE_TIMEOUT_SECONDS = 240
ARC_TITLE_MAX_LENGTH = 500  # Volume.title and Chapter.title share this database limit.
ARC_META_FIELDS = (
    "core_conflict", "emotional_arc", "volume_start_state", "volume_end_state",
    "transition_to_next", "foreshadow_progression",
)


def validate_arc_target(target) -> int:
    if type(target) is not int or not ARC_MIN_CHAPTERS <= target <= ARC_MAX_CHAPTERS:
        raise ValueError(f"target_chapters 必须为 {ARC_MIN_CHAPTERS}–{ARC_MAX_CHAPTERS} 的整数，不得替换请求章数")
    return target


def _content(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, dict):
        return any(_content(v) for v in value.values())
    if isinstance(value, list):
        return any(_content(v) for v in value)
    return False


def _require_content(value, label: str) -> None:
    if not isinstance(value, (str, dict, list)) or not _content(value):
        raise ValueError(f"{label} 必须是有内容的文字或JSON台账")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是可保存的JSON材料") from exc


def _require_text(value, label: str, *, max_length: int | None = None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须为非空文字")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{label} 超过可保存的 {max_length} 字符，请完整重拟标题，不截断")


def _model_object(raw, *, reserved=()) -> dict:
    if not isinstance(raw, dict) or raw.get("parse_error") or raw.get("_parse_error"):
        raise ValueError("需要完整JSON对象")
    if any(key in reserved for key in raw):
        raise ValueError("模型规划不得携带保存身份、内部状态或其他阶段的字段")
    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (not isinstance(key, str) or key.startswith("_")
                        or key in ("chapter_id", "volume_id", "global_idx", "raw_text")):
                    raise ValueError("模型规划不得在台账内注入保存身份或作者文本标记")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(raw)
    # Unknown descriptive fields stay intact; they cannot inject identifiers
    # used by source selection, author text authority or order maintenance.
    json.dumps(raw, ensure_ascii=False, allow_nan=False)
    return raw


def validate_arc_meta(raw, target: int) -> dict:
    validate_arc_target(target)
    _model_object(raw, reserved=("volume_id", "volume_idx", "raw_text", "available",
                                 "arc_plan_version", "chapter_summaries", "beats"))
    if type(raw.get("chapter_count")) is not int or raw["chapter_count"] != target:
        raise ValueError(f"chapter_count 必须为原请求的 {target}")
    _require_text(raw.get("title"), "title", max_length=ARC_TITLE_MAX_LENGTH)
    for key in ARC_META_FIELDS:
        _require_content(raw.get(key), key)
    return deepcopy(raw)


def validate_arc_chapter(raw, expected: int, *, generated: bool = True) -> dict:
    if generated:
        _model_object(raw, reserved=("chapter_id", "chapter", "volume_id", "volume_idx", "raw_text"))
    elif not isinstance(raw, dict):
        raise ValueError(f"第{expected}章计划必须为对象")
    if type(raw.get("chapter_idx")) is not int or raw["chapter_idx"] != expected:
        raise ValueError(f"chapter_idx 必须按原顺序为 {expected}")
    _require_text(raw.get("title"), f"第{expected}章.title", max_length=ARC_TITLE_MAX_LENGTH)
    _require_text(raw.get("summary"), f"第{expected}章.summary")
    events = raw.get("key_events")
    if not isinstance(events, list) or not events:
        raise ValueError(f"第{expected}章.key_events 必须包含实际剧情过程")
    for event in events:
        _require_text(event, f"第{expected}章.key_events条目")
    for key in ("start_state", "end_state", "handoff_to_next"):
        _require_content(raw.get(key), f"第{expected}章.{key}")
    for key in (*CHAPTER_CONTRACT_FIELDS, "character_choices", "narrative_focus"):
        value = raw.get(key)
        if key in raw and value not in (None, "", [], {}):
            if key == "time_delta" and (type(value) is int or type(value) is float and math.isfinite(value)):
                continue
            _require_content(value, f"第{expected}章.{key}")
    return deepcopy(raw)


def validate_arc_batch(raw, start: int, end: int) -> list[dict]:
    if not isinstance(raw, dict) or raw.get("parse_error") or raw.get("_parse_error"):
        raise ValueError("本批必须为完整JSON对象")
    keys = [key for key in ("chapter_summaries", "items") if key in raw]
    if len(keys) != 1 or set(raw) != set(keys):
        raise ValueError("本批只返回一个明确的chapter_summaries数组，不能夹带或丢弃其他规划")
    items = raw[keys[0]]
    if not isinstance(items, list) or len(items) != end - start + 1:
        raise ValueError(f"chapter_summaries 必须准确包含第{start}–{end}章，共{end-start+1}章")
    return [validate_arc_chapter(raw, index) for index, raw in zip(range(start, end + 1), items)]


def validate_complete_arc_outline(raw, target: int) -> dict:
    if (not isinstance(raw, dict) or raw.get("parse_error") or raw.get("_parse_error")
            or "available" in raw and raw["available"] is not True):
        raise ValueError("弧大纲不可用")
    if type(raw.get("arc_plan_version")) is not int or raw["arc_plan_version"] != ARC_PLAN_VERSION:
        raise ValueError("缺少当前版本的完整弧规划")
    meta = {key: value for key, value in raw.items() if key not in ("arc_plan_version", "available", "chapter_summaries")}
    meta = validate_arc_meta(meta, target)
    chapters = validate_arc_batch({"chapter_summaries": raw.get("chapter_summaries")}, 1, target)
    return {**meta, "arc_plan_version": ARC_PLAN_VERSION, "chapter_summaries": chapters}


def build_arc_outline_prompt(*, idea: str, background: str, core_setup: str,
                             opening_scene: str, target_chapters: int, previous_arc: dict | None = None,
                             book_material: str | None = None) -> str:
    if book_material is not None:
        _require_text(book_material, "全书创作材料")
    return (
        ARC_SCOPE_GUIDANCE
        + f"\n本弧准确规划 {target_chapters} 章。正文尚未发生，不能把本次规划当作事实。"
        + "\n【作者本次完整材料】\n"
        + json.dumps({"idea": idea, "background": background, "core_setup": core_setup,
                      "opening_scene": opening_scene}, ensure_ascii=False)
        + "\n【前弧材料：进展由作者填写，大纲是计划；均不能替代实际正文核对】\n"
        + json.dumps(previous_arc or {}, ensure_ascii=False)
        + ("\n" + book_material if book_material is not None else "")
    )


def arc_meta_prompt(source: str, target_chapters: int) -> str:
    return (
        source + "\n【本阶段：弧整体规划】只返回以下元信息，不返回章节数组：\n"
        + json.dumps({"title": "本弧标题", "chapter_count": target_chapters,
                      "core_conflict": "本弧要处理的处境或问题，可为日常合作而非对抗",
                      "emotional_arc": "人物态度如何随事情发展变化，不强制反转",
                      "volume_start_state": "起始人物、物件和未完成的事",
                      "volume_end_state": "本弧计划达到的结果和应保留的未完成事项",
                      "transition_to_next": "本弧自然收束或必要衔接，不另造悬念",
                      "foreshadow_progression": "已有线索在本弧处理什么、什么留待原定后续；无则明确无"}, ensure_ascii=False)
    )


def arc_batch_prompt(source: str, target_chapters: int, meta: dict, accepted: list,
                     start: int, end: int) -> str:
    return (
        source + "\n【本弧完整规划：尚待正文落实】\n" + json.dumps(meta, ensure_ascii=False)
        + "\n【此前完整章节规划：不是已发生事实，不重排或重复已安排过程】\n"
        + json.dumps(accepted, ensure_ascii=False)
        + f"\n【本批】start={start}, end={end}, count={end-start+1}。"
        "只返回chapter_summaries数组，准确覆盖本批编号，不返回其他批次。"
        "每章summary写清人物为何行动、关键回应及结果，key_events写可展开的过程；"
        "事件可为谈话、认识或关系变化，不设最少两次外部事件。"
        "start_state/end_state/handoff_to_next保留状态与未完成事项。"
        "character_choices与narrative_focus按本章需要补充，不强制心理转折。"
        "台账允许完整JSON对象/数组，标题和summary必须为文字，编号为JSON整数。"
        f"最后的第{target_chapters}章落实弧末目标；若为作者指定全书结尾则自然完结。"
        "\n【单章字段示例；本批仍须输出全部指定章节】\n"
        + json.dumps({"chapter_summaries": [{"chapter_idx": start, "title": "标题", "summary": "剧情过程及选择依据",
            "key_events": ["一个完整的情节过程，可按需要增加条目"], "start_state": "承接状态",
            "end_state": "本章计划结果", "handoff_to_next": "未完事项或自然收束"}]}, ensure_ascii=False)
    )


def arc_repair_prompt(original: str, error: str, raw_text: str) -> str:
    return (
        original + f"\n【本阶段完整修复】校验问题：{error}。"
        "按原章数与编号重新返回本阶段全部材料，保留关键过程、选择和结果，"
        "不为补格式减少章节、压成一句话或新增与作者要求冲突的事件。"
        "\n【上一版完整输出，仅供修复，不是已发生事实】\n"
        + (raw_text or "（没有取得模型输出）")
    )


async def _phase(task: str, user: str, validator, *, db, project_id):
    original = user
    error = "未取得可用输出"
    raw_text = ""
    for attempt in range(2):
        try:
            data = await asyncio.wait_for(
                run_structured_prompt(task, user, db, project_id=project_id,
                                      strict_json=True, retry_on_validation_error=False,
                                      output_validator=validator,
                                      **({"temperature": 0.0} if attempt else {})),
                timeout=ARC_PHASE_TIMEOUT_SECONDS,
            )
            if isinstance(data, dict) and (data.get("parse_error") or data.get("_parse_error")):
                error = str(data.get("validation_error") or "JSON未通过校验")
                raw_text = data.get("raw_text") if isinstance(data.get("raw_text"), str) else ""
            else:
                raw_text = json.dumps(data, ensure_ascii=False)
                return validator(data)
        except asyncio.TimeoutError:
            error = "模型阶段超时"
        except (ValueError, TypeError) as exc:
            error = str(exc)
        except Exception as exc:
            error = "模型阶段调用失败"
            logger.warning("arc planning phase %s failed: %s", task, type(exc).__name__)
        if attempt == 0:
            user = arc_repair_prompt(original, error, raw_text)
    raise ValueError(f"{task}: {error}")


async def generate_arc_outline(*, idea: str, background: str, core_setup: str, opening_scene: str,
                               target_chapters: int, db, project_id=None, previous_arc: dict | None = None) -> dict:
    """Deliver a complete plan or an explicit failure; never return a partial success."""
    accepted: list[dict] = []
    meta: dict = {}
    stage = "request"
    try:
        validate_arc_target(target_chapters)
        source = build_arc_outline_prompt(idea=idea, background=background, core_setup=core_setup,
                                          opening_scene=opening_scene, target_chapters=target_chapters,
                                          previous_arc=previous_arc)
        stage = "meta"
        meta_user = arc_meta_prompt(source, target_chapters)
        meta = await _phase("arc_outline_meta", meta_user, lambda value: validate_arc_meta(value, target_chapters),
                            db=db, project_id=project_id)
        for start in range(1, target_chapters + 1, ARC_BATCH_SIZE):
            end = min(start + ARC_BATCH_SIZE - 1, target_chapters)
            stage = f"chapters_{start}_{end}"
            user = arc_batch_prompt(source, target_chapters, meta, accepted, start, end)
            accepted.extend(await _phase("arc_outline_batch", user, lambda value: validate_arc_batch(value, start, end),
                                         db=db, project_id=project_id))
        result = validate_complete_arc_outline({**meta, "arc_plan_version": ARC_PLAN_VERSION,
                                                "chapter_summaries": accepted}, target_chapters)
        return {**result, "available": True}
    except (ValueError, TypeError) as exc:
        return {"available": False, "failed_stage": stage, "validation_error": str(exc),
                "partial_outline": {**meta, "chapter_summaries": accepted}}
