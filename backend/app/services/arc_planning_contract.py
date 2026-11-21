"""Original request identity and reviewable planning receipts."""
from copy import deepcopy
from uuid import UUID

from app.services.arc_outline import validate_arc_target
from app.services.arc_planning_journal import accepted_chapters, next_stage, validate_journal
from app.services.entity_sources import digest
from app.services.planning_book_material import render_book_source
from app.services.planning_character_material import render_character_source
from app.services.planning_cognition_material import validate_cognition_source

MAX_EXECUTIONS = 3
MATERIAL_FIELDS = ("idea", "background", "core_setup", "opening_scene", "target_chapters")
RESUMABLE_ERRORS = {"", "planning_interrupted", "route_unavailable", "materialization_busy",
                    "arc_planning_book_material_unavailable", "arc_planning_character_material_unavailable",
                    "arc_planning_cognition_material_unavailable", "arc_planning_cognition_retrieval_unavailable"}
STATUSES = {"pending", "running", "needs_review", "completed", "cancelled"}


class ArcPlanningError(ValueError):
    def __init__(self, code, message, status=409, **extra):
        super().__init__(message)
        self.code, self.status = code, status
        self.detail = {"code": code, "message": message, **extra}


def request_intent(project_id, values):
    if not isinstance(values, dict) or values.get("request_id") is None or values.get("expected_source") is None:
        raise ArcPlanningError("arc_planning_precondition_required", "请先读取本书当前弧来源，并保留固定请求编号。", 428)
    try:
        pid, rid = str(UUID(str(project_id))), str(UUID(values["request_id"]))
        if set(values) != {"request_id", "expected_source", "mode", *MATERIAL_FIELDS}:
            raise ValueError()
        source = values["expected_source"]
        if (not isinstance(source, dict) or set(source) != {"version", "project_id", "source_hash"}
                or type(source["version"]) is not int or source["version"] != 1 or source["project_id"] != pid
                or not isinstance(source["source_hash"], str) or len(source["source_hash"]) != 64
                or any(c not in "0123456789abcdef" for c in source["source_hash"])):
            raise ValueError()
        if values["mode"] not in {"start", "next"}:
            raise ValueError()
        for key in MATERIAL_FIELDS[:-1]:
            if not isinstance(values[key], str):
                raise ValueError()
        if not values["idea"].strip():
            raise ValueError()
        validate_arc_target(values["target_chapters"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ArcPlanningError("arc_planning_request_invalid", "弧规划请求不完整；请保留原材料并重新核对。", 422) from None
    return {"version": 1, "project_id": pid, **deepcopy(values), "request_id": rid}


def author_material(intent):
    return {key: intent[key] for key in MATERIAL_FIELDS}


def previous_material(source):
    return deepcopy(source["previous"]["material"]) if source["previous"] is not None else None


def book_material(source):
    # Old requests remain readable with their actual inputs. A worker's
    # current-source check refuses to invent this new provenance for them.
    material = render_book_source(source["book_context"], source["project_id"]) if source["version"] in (2, 3, 4, 5) else None
    if source['version'] >= 4:
        material += '\n\n' + render_character_source(source['character_context'], source['project_id'], source['target_volume_idx'])
    return material


def source_token(project_id, source):
    return {"version": 1, "project_id": str(project_id), "source_hash": digest(source)}


def match_request(row, intent):
    if (str(row.project_id) != intent["project_id"] or str(row.id) != intent["request_id"]
            or row.request_hash != digest(intent) or row.request_json != intent):
        raise ArcPlanningError("arc_planning_request_conflict", "这个编号已用于另一份请求，请核对原请求。")
    return row


def validate_record(row):
    try:
        request = row.request_json
        if not isinstance(request, dict) or request.get("version") != 1 or type(request.get("version")) is not int:
            raise ValueError()
        values = {key: value for key, value in request.items() if key not in {"version", "project_id"}}
        intent = request_intent(row.project_id, values)
        match_request(row, intent)
        source = row.source_json
        if (not isinstance(source, dict) or type(source.get("version")) is not int or source["version"] not in (1, 2, 3, 4, 5)
                or source.get("project_id") != str(row.project_id)
                or row.source_hash != digest(source) or intent["expected_source"] != source_token(row.project_id, source)
                or row.target_chapters != intent["target_chapters"] or row.target_volume_idx != source["target_volume_idx"]
                or intent["mode"] != ("next" if source["previous"] is not None else "start")
                or row.status not in STATUSES or type(row.revision) is not int or row.revision < 1
                or type(row.execution_attempts) is not int or not 0 <= row.execution_attempts <= MAX_EXECUTIONS
                or not isinstance(row.execution_history_json, list)
                or row.journal_hash != digest(row.journal_json)):
            raise ValueError()
        keys = {'version', 'project_id', 'target_volume_idx', 'volume_order', 'previous'}
        if source['version'] >= 2: keys.add('book_context')
        if source['version'] >= 4: keys.add('character_context')
        if source['version'] == 5: keys.add('cognition_context')
        if set(source) != keys:
            raise ValueError()
        if source['version'] in (2, 3) and source['book_context']['version'] != source['version'] - 1:
            raise ValueError()
        if source['version'] >= 4 and (source['book_context']['version'] != 2
                or source['character_context']['volume_order'] != sorted(source['volume_order'], key=lambda row: row['id'])):
            raise ValueError()
        cognition = source.get('cognition_context')
        if source['version'] == 5:
            validate_cognition_source(cognition, row.project_id, source['character_context'])
        validate_journal(row.journal_json, author_material(intent), previous_material(source), book_material(source),
                         cognition_source=cognition)
        spec = next_stage(row.journal_json)
        if row.accepted_chapters != accepted_chapters(row.journal_json):
            raise ValueError()
        phase = "done" if row.status == "completed" else spec["key"] if spec else "finalize"
        if row.phase != phase:
            raise ValueError()
        count = 0
        seen = set()
        for entry in row.execution_history_json:
            identity = str(UUID(entry["execution_id"]))
            if identity in seen or entry["kind"] not in {"planning", "finalize"} or not isinstance(entry["started_at"], str):
                raise ValueError()
            seen.add(identity)
            count += entry["kind"] == "planning"
            if type(entry["number"]) is not int or entry["number"] != count:
                raise ValueError()
        if count != row.execution_attempts or (row.execution_id is not None and str(row.execution_id) not in seen):
            raise ValueError()
        if row.status == "running" and row.execution_id is None:
            raise ValueError()
        if row.status == "pending" and (row.execution_id is not None or row.execution_attempts or row.journal_json["stages"]):
            raise ValueError()
        if row.status == "completed":
            if spec is not None or row.accepted_chapters != row.target_chapters:
                raise ValueError()
            validate_receipt(row)
        elif row.result_json is not None or row.result_hash is not None:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise ArcPlanningError("arc_planning_record_invalid", "原规划记录不完整或已变化；保留现有材料，不能重建预算后继续。") from None
    return row


def validate_receipt(row):
    result = row.result_json
    if (not isinstance(result, dict) or row.result_hash != digest(result) or result.get("version") != 1
            or result.get("request_id") != str(row.id) or result.get("project_id") != str(row.project_id)
            or result.get("request_hash") != row.request_hash or result.get("journal_hash") != row.journal_hash
            or result.get("chapter_count") != row.target_chapters or result.get("volume_idx") != row.target_volume_idx
            or not isinstance(result.get("chapter_ids"), list) or len(result["chapter_ids"]) != row.target_chapters
            or len(set(result["chapter_ids"])) != row.target_chapters):
        raise ValueError("arc_planning_receipt_invalid")
    for identity in [result["volume_id"], result["outline_id"], *result["chapter_ids"]]:
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError("arc_planning_receipt_invalid")
    return result


def can_resume(row):
    if row.status not in {"pending", "running", "needs_review"} or row.error_code not in RESUMABLE_ERRORS:
        return False
    try:
        validate_record(row)
        if row.status in {"pending", "running"}:
            # Resume also audits an interrupted final reservation. Only the
            # physical lock can distinguish a dead worker from a live call.
            return True
        spec = next_stage(row.journal_json)
        if spec is None:
            return True  # Saving an already complete plan buys no model call.
        if row.execution_attempts >= MAX_EXECUTIONS:
            return False
        stages = row.journal_json["stages"]
        return not stages or stages[-1]["key"] != spec["key"] or len(stages[-1]["attempts"]) < 2
    except (ValueError, TypeError):
        return False
