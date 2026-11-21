"""Pure, versioned arc checkpoints. A reserved model call already costs a try."""
from copy import deepcopy
from datetime import datetime

from app.services.arc_outline import (
    ARC_BATCH_SIZE, ARC_PLAN_VERSION, arc_batch_prompt, arc_meta_prompt, arc_repair_prompt,
    build_arc_outline_prompt, validate_arc_batch, validate_arc_meta, validate_arc_target,
    validate_complete_arc_outline,
)
from app.services.prompt_execution_snapshot import validate_prompt_snapshot
from app.services.planning_cognition_material import (
    CognitionSelectionVerifier, append_cognition_material, cognition_reference,
    validate_cognition_reference, validate_cognition_selection,
)

PHASE_ATTEMPTS = 2
INTERRUPTED = "上次调用中断，未取得可确认的完整输出"


class ArcPlanningBudgetExhausted(ValueError):
    pass


def new_journal(material, previous_arc, book_material=None, *, cognition_source=None):
    validate_arc_target(material["target_chapters"])
    result = {"version": 1, "target_chapters": material["target_chapters"],
            "source_prompt": build_arc_outline_prompt(**material, previous_arc=previous_arc, book_material=book_material), "stages": []}
    if cognition_source is not None:
        result.update(version=2, cognition_reference=cognition_reference(cognition_source),
            # Recall by the author's task and actual plan, not by every name
            # in the full reference library. Every author word is retained.
            cognition_query_base=build_arc_outline_prompt(**material, previous_arc=previous_arc))
    return result


def _specs(target):
    return [{"key": "meta", "task": "arc_outline_meta", "start": None, "end": None}] + [
        {"key": f"chapters_{start}_{min(start + ARC_BATCH_SIZE - 1, target)}", "task": "arc_outline_batch",
         "start": start, "end": min(start + ARC_BATCH_SIZE - 1, target)}
        for start in range(1, target + 1, ARC_BATCH_SIZE)]


def validate_stage_output(spec, output, target):
    if spec["key"] == "meta":
        return validate_arc_meta(output, target)
    return {"chapter_summaries": validate_arc_batch(output, spec["start"], spec["end"])}


def _stamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("arc_planning_timestamp_invalid")


def _stage_input(value, spec, meta, chapters, *, query=False):
    base = value['cognition_query_base'] if query else value['source_prompt']
    return (arc_meta_prompt(base, value['target_chapters']) if spec['key'] == 'meta' else
            arc_batch_prompt(base, value['target_chapters'], meta, chapters, spec['start'], spec['end']))


def _walk(value, *, cognition_source=None):
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] not in (1, 2):
        raise ValueError("arc_planning_checkpoint_invalid")
    keys = {"version", "target_chapters", "source_prompt", "stages"}
    if value['version'] == 2: keys.update(('cognition_reference', 'cognition_query_base'))
    if (set(value) != keys
            or not isinstance(value["source_prompt"], str) or not value["source_prompt"].strip()
            or not isinstance(value["stages"], list)):
        raise ValueError("arc_planning_checkpoint_invalid")
    if value['version'] == 2:
        validate_cognition_reference(value['cognition_reference'])
        if not isinstance(value['cognition_query_base'], str) or not value['cognition_query_base'].strip():
            raise ValueError('arc_planning_cognition_query_invalid')
    verify_cognition = (CognitionSelectionVerifier(cognition_source, value['cognition_reference'])
        if value['version'] == 2 and cognition_source is not None else None)
    target = validate_arc_target(value["target_chapters"])
    specs, meta, chapters = _specs(target), {}, []
    if len(value["stages"]) > len(specs):
        raise ValueError("arc_planning_checkpoint_invalid")
    finished = 0
    for index, stage in enumerate(value["stages"]):
        spec = specs[index]
        original = _stage_input(value, spec, meta, chapters)
        query_original = _stage_input(value, spec, meta, chapters, query=True) if value['version'] == 2 else None
        if (not isinstance(stage, dict) or set(stage) != {"key", "task", "original_input", "route", "attempts"}
                or stage["key"] != spec["key"] or stage["task"] != spec["task"]
                or stage["original_input"] != original or not isinstance(stage["attempts"], list)
                or not 1 <= len(stage["attempts"]) <= PHASE_ATTEMPTS):
            raise ValueError("arc_planning_stage_invalid")
        validate_prompt_snapshot(stage["route"], spec["task"])
        attempts = stage["attempts"]
        for number, attempt in enumerate(attempts, 1):
            expected = original if number == 1 else arc_repair_prompt(original, attempts[number-2]["error"], attempts[number-2]["raw_text"])
            attempt_keys = {"number", "state", "input", "output", "raw_text", "error", "started_at", "finished_at"}
            if value['version'] == 2:
                attempt_keys.add('cognition')
                query = query_original if number == 1 else arc_repair_prompt(
                    query_original, attempts[number-2]['error'], attempts[number-2]['raw_text'])
                if verify_cognition is not None:
                    verify_cognition(attempt['cognition'], query)
                else:
                    validate_cognition_selection(attempt['cognition'], value['cognition_reference'], query)
                expected = append_cognition_material(expected, attempt['cognition']['text'])
            if (not isinstance(attempt, dict) or set(attempt) != attempt_keys
                    or type(attempt["number"]) is not int or attempt["number"] != number
                    or attempt["state"] not in {"reserved", "accepted", "rejected", "interrupted"}
                    or attempt["input"] != expected or not isinstance(attempt["raw_text"], str)
                    or not isinstance(attempt["error"], str)):
                raise ValueError("arc_planning_attempt_invalid")
            _stamp(attempt["started_at"])
            state = attempt["state"]
            if state == "reserved":
                if (number != len(attempts) or attempt["finished_at"] is not None
                        or attempt["output"] is not None or attempt["raw_text"] or attempt["error"]):
                    raise ValueError("arc_planning_attempt_invalid")
            else:
                _stamp(attempt["finished_at"])
                if state == "accepted":
                    normalized = validate_stage_output(spec, attempt["output"], target)
                    if number != len(attempts) or attempt["error"] or not attempt["raw_text"] or normalized != attempt["output"]:
                        raise ValueError("arc_planning_attempt_invalid")
                elif attempt["output"] is not None or not attempt["error"]:
                    raise ValueError("arc_planning_attempt_invalid")
                if state == "interrupted" and (attempt["error"] != INTERRUPTED or attempt["raw_text"]):
                    raise ValueError("arc_planning_attempt_invalid")
        last = attempts[-1]
        if last["state"] != "accepted":
            if index != len(value["stages"]) - 1:
                raise ValueError("arc_planning_stage_gap")
            break
        finished += 1
        if index == 0:
            meta = deepcopy(last["output"])
        else:
            chapters.extend(deepcopy(last["output"]["chapter_summaries"]))
    return specs, finished, meta, chapters


def validate_journal(value, material=None, previous_arc=None, book_material=None, *, cognition_source=None):
    try:
        _walk(value, cognition_source=cognition_source)
        if material is not None:
            expected = new_journal(material, previous_arc, book_material, cognition_source=cognition_source)
            if (value['version'] != expected['version'] or value["target_chapters"] != expected["target_chapters"]
                    or value["source_prompt"] != expected["source_prompt"]):
                raise ValueError("arc_planning_source_changed")
            if value['version'] == 2 and any(value[key] != expected[key] for key in ('cognition_reference', 'cognition_query_base')):
                raise ValueError('arc_planning_cognition_source_changed')
    except (KeyError, IndexError, AttributeError) as exc:
        raise ValueError("arc_planning_checkpoint_invalid") from exc
    return value


def next_stage(value):
    validate_journal(value)
    specs, finished, _, _ = _walk(value)
    return deepcopy(specs[finished]) if finished < len(specs) else None


def next_attempt_query(value):
    """The query is tied to this attempt, including the complete repair draft."""
    validate_journal(value)
    specs, finished, meta, chapters = _walk(value)
    if finished == len(specs):
        raise ValueError('arc_planning_already_complete')
    attempts = value['stages'][finished]['attempts'] if len(value['stages']) > finished else []
    if len(attempts) >= PHASE_ATTEMPTS:
        raise ArcPlanningBudgetExhausted('本阶段两次尝试已用完；完整原材料与已有规划保留待审。')
    if value['version'] == 1:
        return None
    original = _stage_input(value, specs[finished], meta, chapters, query=True)
    if not attempts:
        return original
    previous = attempts[-1]
    return arc_repair_prompt(original, INTERRUPTED if previous['state'] == 'reserved' else previous['error'], previous['raw_text'])


def reserve_attempt(value, route, *, now, cognition=None):
    validate_journal(value); _stamp(now)
    query = next_attempt_query(value)
    if value['version'] == 2:
        validate_cognition_selection(cognition, value['cognition_reference'], query)
    elif cognition is not None:
        raise ValueError('legacy_arc_cognition_not_recorded')
    result = deepcopy(value)
    specs, finished, meta, chapters = _walk(result)
    if finished == len(specs):
        raise ValueError("arc_planning_already_complete")
    spec = specs[finished]
    if len(result["stages"]) == finished:
        validate_prompt_snapshot(route, spec["task"])
        original = _stage_input(result, spec, meta, chapters)
        result["stages"].append({"key": spec["key"], "task": spec["task"], "original_input": original,
                                 "route": deepcopy(route), "attempts": []})
    stage = result["stages"][-1]
    attempts = stage["attempts"]
    if len(attempts) >= PHASE_ATTEMPTS:
        raise ArcPlanningBudgetExhausted("本阶段两次尝试已用完；完整原材料与已有规划保留待审。")
    if attempts and attempts[-1]["state"] == "reserved":
        attempts[-1].update(state="interrupted", error=INTERRUPTED, finished_at=now)
    user = stage["original_input"] if not attempts else arc_repair_prompt(
        stage["original_input"], attempts[-1]["error"], attempts[-1]["raw_text"])
    if result['version'] == 2:
        user = append_cognition_material(user, cognition['text'])
    attempts.append({"number": len(attempts) + 1, "state": "reserved", "input": user, "output": None,
                     "raw_text": "", "error": "", "started_at": now, "finished_at": None})
    if result['version'] == 2:
        attempts[-1]['cognition'] = deepcopy(cognition)
    validate_journal(result)
    return result


def pending_call(value):
    spec = next_stage(value)
    if spec is None or not value["stages"] or value["stages"][-1]["key"] != spec["key"]:
        raise ValueError("arc_planning_call_not_reserved")
    stage = value["stages"][-1]
    attempt = stage["attempts"][-1]
    if attempt["state"] != "reserved":
        raise ValueError("arc_planning_call_not_reserved")
    return {**spec, "number": attempt["number"], "input": attempt["input"], "route": deepcopy(stage["route"]),
            "temperature": 0.0 if attempt["number"] > 1 else stage["route"]["temperature"]}


def finish_attempt(value, key, number, *, output, raw_text, error, now):
    call = pending_call(value); _stamp(now)
    if call["key"] != key or call["number"] != number:
        raise ValueError("arc_planning_call_changed")
    if not isinstance(raw_text, str) or not isinstance(error, str):
        raise ValueError("arc_planning_output_invalid")
    normalized = validate_stage_output(call, output, value["target_chapters"]) if not error else None
    result = deepcopy(value)
    result["stages"][-1]["attempts"][-1].update(
        state="rejected" if error else "accepted", output=normalized, raw_text=raw_text, error=error, finished_at=now)
    validate_journal(result)
    return result


def accepted_chapters(value):
    validate_journal(value)
    return len(_walk(value)[3])


def complete_outline(value):
    validate_journal(value)
    specs, finished, meta, chapters = _walk(value)
    if finished != len(specs):
        raise ValueError("arc_planning_incomplete")
    return {**validate_complete_arc_outline({**meta, "arc_plan_version": ARC_PLAN_VERSION,
        "chapter_summaries": chapters}, value["target_chapters"]), "available": True}
