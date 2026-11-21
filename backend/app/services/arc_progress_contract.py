"""Author manuscript confirmation is not a model score or a chapter counter."""
from copy import deepcopy
import hashlib
import re
from uuid import UUID

from app.services.entity_sources import digest
from app.services.outline_authority import outline_for_generation

ACTIONS = ("acknowledge", "retract", "reconcile", "direction", "finish", "reopen")
PHASES = ("active", "awaiting_direction", "completed")


class ArcProgressError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status
        self.detail = {"code": code, "message": message}


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_hash(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def is_id(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def plan_hash(content):
    """Hash the writing material, excluding legacy counters and model verdicts."""
    value = outline_for_generation(content)
    if isinstance(value, dict):
        arc = value.pop("_arc", None)
        if isinstance(arc, dict):
            value["author_arc_material"] = {key: deepcopy(arc[key])
                for key in ("core_setup", "opening_scene", "input_material") if key in arc}
        for key in ("_quality_status", "_consistency_report"):
            value.pop(key, None)
    return digest(value)


def initial_progress(volume_id, content, chapter_ids, legacy):
    original_order = content.get("_arc", {}).get("chapter_order")
    if original_order is not None:
        if (not isinstance(original_order, list) or len(original_order) > 40
                or not all(is_id(cid) for cid in original_order) or len(set(original_order)) != len(original_order)):
            raise ArcProgressError("arc_progress_invalid", "本弧原章节身份无效，请先保留并核对大纲。")
        # A reorder before the first report may establish the initial order.
        # Lost/added members, however, require explicit adoption of a new plan.
        if set(original_order) != set(chapter_ids):
            chapter_ids = original_order
    return {"schema": 1, "volume_id": str(volume_id), "plan_hash": plan_hash(content),
        "chapter_order": list(chapter_ids), "acknowledged": {}, "phase": "active",
        # A legacy direction has no original chapter identity. Keep it in the
        # first history snapshot; only a new author action may bind its target.
        "direction": None, "direction_chapter_id": None,
        "closed_early": False, "closing_note": "",
        "legacy_request_id": None, "completion_source_hash": None}


def completion_source_hash(progress):
    return digest({"plan_hash": progress["plan_hash"], "chapter_order": progress["chapter_order"]})


def validate_progress(value, volume_id):
    valid = isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1
    if not valid or value.get("volume_id") != str(volume_id):
        raise ArcProgressError("arc_progress_invalid", "保存的弧进度无法核对，请保留并检查历史回执。")
    order = value.get("chapter_order")
    acknowledgements = value.get("acknowledged")
    if (not isinstance(order, list) or len(order) > 40 or not all(is_id(cid) for cid in order)
            or len(set(order)) != len(order) or not isinstance(acknowledgements, dict)
            or len(acknowledgements) > 40 or value.get("phase") not in PHASES
            or not is_hash(value.get("plan_hash")) or type(value.get("closed_early")) is not bool
            or not isinstance(value.get("closing_note"), str)
            or value.get("direction") is not None and not isinstance(value["direction"], str)
            or value.get("direction_chapter_id") is not None and not is_id(value["direction_chapter_id"])
            or (not is_hash(value.get("completion_source_hash")) if value.get("phase") == "completed"
                else value.get("completion_source_hash") is not None)
            or value.get("legacy_request_id") is not None and not is_id(value["legacy_request_id"])):
        raise ArcProgressError("arc_progress_invalid", "保存的弧进度结构不完整，不能按旧计数继续。")
    for cid, proof in acknowledgements.items():
        if (not is_id(cid) or not isinstance(proof, dict) or not is_id(proof.get("request_id"))
                or not is_hash(proof.get("content_hash")) or not isinstance(proof.get("title"), str)
                or not isinstance(proof.get("author_note"), str)):
            raise ArcProgressError("arc_progress_invalid", "章节确认缺少完整的保存稿来源，请核对原回执。")
    return deepcopy(value)


def request_intent(project_id, volume_id, values):
    if values.get("request_id") is None or values.get("expected_source") is None:
        raise ArcProgressError("arc_progress_precondition_required", "请先读取当前弧进度，并保留本次确认请求编号。", 428)
    try:
        pid, vid, rid = (UUID(str(value)) for value in (project_id, volume_id, values["request_id"]))
        allowed = {"request_id", "expected_source", "action", "chapter_id", "expected_content_text",
                   "expected_title", "author_note", "direction", "closing_note"}
        if set(values) - allowed:
            raise ValueError("unknown request fields")
        payload = {key: deepcopy(values.get(key)) for key in allowed}
        for key in ("author_note", "closing_note"):
            if payload[key] is None:
                payload[key] = ""
        payload["request_id"] = str(rid)
        if payload.get("chapter_id") is not None:
            payload["chapter_id"] = str(UUID(str(payload["chapter_id"])))
        fingerprint = digest({"schema": 1, "project_id": str(pid), "volume_id": str(vid), "request": payload})
    except (ValueError, TypeError, AttributeError):
        raise ArcProgressError("arc_progress_request_invalid", "弧进度请求无效，请保留输入并重新核对。", 422) from None
    source = payload["expected_source"]
    if (not isinstance(source, dict) or type(source.get("schema")) is not int or source["schema"] != 1
            or payload.get("action") not in ACTIONS):
        raise ArcProgressError("arc_progress_request_invalid", "弧进度请求缺少有效的操作或来源。", 422)
    action = payload["action"]
    cid = payload.get("chapter_id")
    if action in ("acknowledge", "retract") and cid is None:
        raise ArcProgressError("arc_progress_request_invalid", "请选择明确的章节。", 422)
    if action not in ("acknowledge", "retract") and cid is not None:
        raise ArcProgressError("arc_progress_request_invalid", "当前操作不接受章节编号。", 422)
    for key in ("expected_content_text", "expected_title"):
        if action == "acknowledge" and not isinstance(payload.get(key), str):
            raise ArcProgressError("arc_manuscript_precondition_required", "请先读取本章完整保存稿，再确认本章完成。", 428)
        if action != "acknowledge" and payload.get(key) is not None:
            raise ArcProgressError("arc_progress_request_invalid", "当前操作不接受替代正文。", 422)
    for key in ("author_note", "direction", "closing_note"):
        if payload.get(key) is not None and not isinstance(payload[key], str):
            raise ArcProgressError("arc_progress_request_invalid", "作者填写的说明必须为文字。", 422)
    if action == "direction" and not (payload.get("direction") or "").strip():
        raise ArcProgressError("arc_progress_request_invalid", "请填写下一步方向。", 422)
    if (action != "direction" and payload.get("direction") is not None
            or action != "finish" and payload.get("closing_note")
            or action not in ("acknowledge", "retract") and payload.get("author_note")):
        raise ArcProgressError("arc_progress_request_invalid", "说明与当前操作不一致，请保留输入并核对。", 422)
    return pid, vid, rid, fingerprint, payload


def validate_result(row):
    result = row.result_json
    try:
        valid = (isinstance(result, dict) and type(result.get("schema")) is int and result["schema"] == 1
            and digest(result) == row.result_hash
            and result.get("request_id") == str(row.id) and result.get("project_id") == str(row.project_id)
            and result.get("volume_id") == str(row.volume_id) and result.get("status") == "saved"
            and isinstance(result.get("volume_title"), str) and type(result.get("volume_idx")) is int and result["volume_idx"] > 0
            and type(result.get("sequence")) is int and result["sequence"] == row.sequence
            and type(row.sequence) is int and row.sequence > 0 and result.get("action") == row.action
            and row.action in ACTIONS and result.get("request_hash") == row.request_hash
            and is_hash(row.request_hash) and is_hash(result.get("before_hash"))
            and is_hash(result.get("manuscript_snapshot_hash"))
            and result.get("chapter_id") == (str(row.chapter_id) if row.chapter_id else None)
            and result.get("manuscript_hash") == row.manuscript_hash
            and (is_hash(row.manuscript_hash) if row.action == "acknowledge" else row.manuscript_hash is None)
            and (row.chapter_id is not None if row.action in ("acknowledge", "retract") else row.chapter_id is None))
        if valid:
            validate_progress(result.get("progress"), row.volume_id)
            valid = result["progress"]["legacy_request_id"] is not None
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ArcProgressError("arc_progress_receipt_invalid", "弧进度回执已损坏或缺少来源，不能回退为旧计数。")
    return deepcopy(result)


def match_receipt(row, pid, vid, fingerprint):
    if row.project_id != pid or row.volume_id != vid or row.request_hash != fingerprint:
        raise ArcProgressError("arc_progress_request_conflict", "该请求编号已用于另一项弧进度操作，请核对原回执。")
    return validate_result(row)
