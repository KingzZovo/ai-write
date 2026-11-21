"""Small progress reads; complete original inputs are loaded only on request."""
from app.models.arc_planning import ArcPlanningRequest as Request
from app.services.arc_planning_contract import book_material, can_resume, validate_receipt, validate_record
from app.services.planning_cognition_material import render_cognition_archive

STATUS_COLUMNS = (Request.id, Request.project_id, Request.request_hash, Request.journal_hash,
    Request.status, Request.phase, Request.target_chapters, Request.target_volume_idx, Request.accepted_chapters,
    Request.revision, Request.execution_attempts, Request.error_code, Request.error_message,
    Request.result_json, Request.result_hash, Request.created_at, Request.updated_at, Request.heartbeat_at)


def status_view(row):
    status, error_code, error_message = row.status, row.error_code, row.error_message
    result = row.result_json
    if status == "completed":
        try:
            validate_receipt(row)
        except (ValueError, TypeError, KeyError):
            status, result = "needs_review", None
            error_code, error_message = "arc_planning_record_invalid", "创建回执不完整，请查看保留的原记录。"
    return {"request_id": str(row.id), "project_id": str(row.project_id), "request_hash": row.request_hash,
        "status": status, "phase": row.phase, "target_chapters": row.target_chapters,
        "target_volume_idx": row.target_volume_idx, "accepted_chapters": row.accepted_chapters,
        "revision": row.revision, "execution_attempts": row.execution_attempts,
        "error_code": error_code, "error_message": error_message, "result": result,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


def detail_view(row):
    try:
        validate_record(row)
        valid = True
    except (ValueError, TypeError):
        valid = False
    accepted = None
    if valid:
        accepted = {"chapter_summaries": []}
        for stage in row.journal_json["stages"]:
            attempt = stage["attempts"][-1]
            if attempt["state"] != "accepted":
                break
            if stage["key"] == "meta":
                accepted.update(attempt["output"])
            else:
                accepted["chapter_summaries"].extend(attempt["output"]["chapter_summaries"])
    return {**status_view(row), "integrity_valid": valid, "can_resume": valid and can_resume(row),
        "request": row.request_json, "source": row.source_json, "journal": row.journal_json,
        "execution_history": row.execution_history_json, "original_result": row.result_json,
        "book_material_text": book_material(row.source_json) if valid else None,
        "cognition_material_text": render_cognition_archive(row.source_json['cognition_context'])
            if valid and row.source_json['version'] == 5 else None,
        "accepted_outline": accepted}
