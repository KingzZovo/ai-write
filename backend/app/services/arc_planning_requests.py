"""Short durable submission and author commands; no model call or broker I/O."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.models.arc_planning import ArcPlanningRequest
from app.models.project import Project
from app.services.arc_planning_contract import (
    ArcPlanningError, author_material, book_material, can_resume, match_request, previous_material, request_intent,
    source_token, validate_record,
)
from app.services.arc_planning_journal import new_journal
from app.services.arc_planning_sources import read_planning_source
from app.services.chapter_order import lock_chapter_structure
from app.services.entity_sources import digest


async def existing_request(db, request_id, *, lock=False):
    query = select(ArcPlanningRequest).where(ArcPlanningRequest.id == UUID(str(request_id)))
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True, autoflush=False))


async def require_project(db, project_id, *, lock=False):
    query = select(Project.id).where(Project.id == UUID(str(project_id)), Project.deleted_at.is_(None))
    if lock:
        query = query.with_for_update()
    if await db.scalar(query.execution_options(autoflush=False)) is None:
        raise ArcPlanningError("arc_planning_project_unavailable", "作品不存在或已删除。", 404)


async def prepare_arc_request(db, project_id, values):
    intent = request_intent(project_id, values)
    await require_project(db, project_id)
    existing = await existing_request(db, intent["request_id"])
    if existing is not None:
        return match_request(existing, intent), False
    await lock_chapter_structure(db, project_id)
    existing = await existing_request(db, intent["request_id"])
    if existing is not None:
        return match_request(existing, intent), False
    active = await db.scalar(select(ArcPlanningRequest.id).where(
        ArcPlanningRequest.project_id == UUID(str(project_id)), ArcPlanningRequest.status.in_(("pending", "running")))
        .limit(1).execution_options(autoflush=False))
    if active is not None:
        raise ArcPlanningError("arc_planning_active", "本书已有未完成的弧规划，请先查看或恢复原请求。", request_id=str(active))
    source = await read_planning_source(db, project_id, lock=True)
    if (intent["expected_source"] != source_token(project_id, source)
            or intent["mode"] != ("next" if source["previous"] is not None else "start")):
        raise ArcPlanningError("arc_planning_source_changed", "全书材料、分卷或前弧已变化，请重新核对后提交。")
    journal = new_journal(author_material(intent), previous_material(source), book_material(source),
                          cognition_source=source.get('cognition_context'))
    row = ArcPlanningRequest(id=UUID(intent["request_id"]), project_id=UUID(str(project_id)),
        request_json=intent, request_hash=digest(intent), source_json=source, source_hash=digest(source),
        journal_json=journal, journal_hash=digest(journal), status="pending", phase="meta",
        target_chapters=intent["target_chapters"], target_volume_idx=source["target_volume_idx"],
        accepted_chapters=0, revision=1, execution_attempts=0, execution_history_json=[], error_code="", error_message="")
    db.add(row)
    await db.flush()
    validate_record(row)
    return row, True


async def author_request_command(db, project_id, request_id, action):
    if action not in {"resume", "cancel"}:
        raise ArcPlanningError("arc_planning_command_invalid", "无法识别本次操作。", 422)
    # Cancellation can interrupt an active worker; it must not attempt to own
    # the long-lived generation mutex. Every worker save rechecks this row.
    await require_project(db, project_id, lock=True)
    row = await existing_request(db, request_id, lock=True)
    if row is None or row.project_id != UUID(str(project_id)):
        raise ArcPlanningError("arc_planning_request_unavailable", "找不到这份原规划请求。", 404)
    if row.status in {"completed", "cancelled"}:
        return row, False
    if action == "cancel":
        row.status, row.execution_id = "cancelled", None
        row.error_code, row.error_message = "cancelled", "作者已停止本次规划；原请求与已有内容保留。"
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        return row, False
    if not can_resume(row):
        raise ArcPlanningError("arc_planning_resume_unavailable", "原请求需要审阅，无法继续调用；请查看完整记录与停止原因。")
    # Do not clear status, failure information, attempts or the execution ID.
    # The worker can resume only after actually acquiring the physical lock.
    return row, True
