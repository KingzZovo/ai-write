"""Append one durable author decision; the receipt itself owns progress."""
from copy import deepcopy
from uuid import UUID

from sqlalchemy import select

from app.models.arc_progress import ArcProgressWrite
from app.models.project import Chapter, Project
from app.services.arc_progress_contract import (
    ArcProgressError, completion_source_hash, match_receipt, plan_hash, request_intent, text_hash, validate_progress,
)
from app.services.arc_progress_reader import load_arc_progress, receipt_header, require_current_progress
from app.services.chapter_order import lock_chapter_structure
from app.services.entity_sources import digest


async def prepare_arc_progress_write(db, project_id, volume_id, **values):
    pid, vid, rid, fingerprint, payload = request_intent(project_id, volume_id, values)
    if await db.scalar(select(Project.id).where(Project.id == pid, Project.deleted_at.is_(None))
                       .execution_options(autoflush=False)) is None:
        raise ArcProgressError("arc_progress_unavailable", "作品不存在或已删除。", 404)
    existing = await receipt_header(db, rid)
    if existing is not None:
        return match_receipt(existing, pid, vid, fingerprint)
    await lock_chapter_structure(db, pid)
    existing = await receipt_header(db, rid)
    if existing is not None:
        return match_receipt(existing, pid, vid, fingerprint)
    view = await load_arc_progress(db, pid, vid, lock=True)
    if digest(view.source) != digest(payload["expected_source"]):
        raise ArcProgressError("arc_progress_changed", "保存稿、弧安排或进度已变化，请保留输入并重新核对。")
    progress, action, cid = deepcopy(view.progress), payload["action"], payload["chapter_id"]
    manuscript = {}
    if action == "acknowledge":
        if progress["phase"] == "completed":
            raise ArcProgressError("arc_progress_closed", "本弧已结束，请先明确继续本弧，再重新核对章节。")
        # Rechecking an existing changed manuscript must remain possible before
        # adopting a simultaneously changed plan. Other issues remain visible.
        if cid not in progress["acknowledged"]:
            require_current_progress(view)
        chapter = (await db.execute(select(Chapter.id, Chapter.title, Chapter.content_text)
            .where(Chapter.id == UUID(cid), Chapter.volume_id == vid).with_for_update()
            .execution_options(autoflush=False))).one_or_none()
        if chapter is None:
            raise ArcProgressError("arc_manuscript_unavailable", "章节已移出或删除，请核对原稿和历史。", 404)
        text = chapter.content_text or ""
        if text != payload["expected_content_text"] or chapter.title != payload["expected_title"]:
            raise ArcProgressError("arc_manuscript_changed", "本章标题或正文已改变，请重读完整保存稿再确认。")
        if not text.strip():
            raise ArcProgressError("arc_manuscript_empty", "保存稿仍为空，不能确认本章完成。", 422)
        manuscript = {"chapter_id": cid, "title": chapter.title, "content_text": text, "content_hash": text_hash(text)}
        progress["acknowledged"][cid] = {"request_id": str(rid), "title": chapter.title,
            "content_hash": manuscript["content_hash"], "author_note": payload["author_note"]}
        if progress.get("direction_chapter_id") == cid:
            progress["direction"], progress["direction_chapter_id"] = None, None
        progress["phase"] = "awaiting_direction"
        # Completion depends on identities, never on acknowledgement count alone.
        if (progress["chapter_order"] and set(progress["acknowledged"]) == set(progress["chapter_order"])
                and not [issue for issue in view.issues if issue.get("chapter_id") != cid]):
            progress["phase"] = "completed"
            progress["completion_source_hash"] = completion_source_hash(progress)
    elif action == "retract":
        if cid not in progress["acknowledged"]:
            raise ArcProgressError("arc_confirmation_missing", "本章没有可撤销的作者确认，请重新核对。")
        del progress["acknowledged"][cid]
        progress["phase"], progress["closed_early"], progress["closing_note"] = "active", False, ""
        progress["completion_source_hash"] = None
    elif action == "reconcile":
        if any(issue["code"] in ("manuscript_changed", "chapter_removed") for issue in view.issues):
            raise ArcProgressError("arc_progress_needs_reconciliation", "请先重新确认变更稿，或撤销移出章节的旧确认。")
        progress["plan_hash"] = plan_hash(view.outline.content_json)
        progress["chapter_order"] = [chapter["id"] for chapter in view.source["chapters"]]
        # Adopting a plan cannot silently reopen a deliberately finished arc.
    elif action == "direction":
        require_current_progress(view)
        if progress["phase"] == "completed":
            raise ArcProgressError("arc_progress_closed", "本弧已结束，请先明确继续本弧。")
        next_id = view.public()["next_chapter_id"]
        if next_id is None:
            raise ArcProgressError("arc_progress_no_pending_chapter", "本弧没有待写章节，请先核对章节安排。")
        progress["direction"], progress["phase"] = payload["direction"], "active"
        progress["direction_chapter_id"] = next_id
    elif action == "finish":
        if any(issue["code"] != "completion_changed" for issue in view.issues):
            require_current_progress(view)
        progress["phase"] = "completed"
        progress["closed_early"] = not progress["chapter_order"] or any(
            chapter_id not in progress["acknowledged"] for chapter_id in progress["chapter_order"])
        progress["closing_note"] = payload["closing_note"]
        progress["completion_source_hash"] = completion_source_hash(progress)
    elif action == "reopen":
        if progress["phase"] != "completed":
            raise ArcProgressError("arc_progress_not_closed", "本弧尚未结束，无须重新开启。")
        progress["phase"], progress["closed_early"], progress["closing_note"] = "active", False, ""
        progress["completion_source_hash"] = None
    before = {"source": deepcopy(view.source), "progress": deepcopy(view.progress),
              "author_note": payload["author_note"]}
    if view.latest is None:
        before["legacy"] = deepcopy(view.outline.content_json["_arc"])
        progress["legacy_request_id"] = str(rid)
    sequence = view.latest.sequence + 1 if view.latest else 1
    if sequence > 2147483647:
        raise ArcProgressError("arc_progress_history_full", "历史序号已达到容量限制，请保留历史后维护。")
    validate_progress(progress, vid)
    result = {"schema": 1, "status": "saved", "request_id": str(rid), "project_id": str(pid),
        "volume_id": str(vid), "volume_title": view.volume.title, "volume_idx": view.volume.volume_idx,
        "sequence": sequence, "action": action, "chapter_id": cid,
        "manuscript_hash": manuscript.get("content_hash"), "request_hash": fingerprint,
        "before_hash": digest(before), "manuscript_snapshot_hash": digest(manuscript), "progress": progress}
    db.add(ArcProgressWrite(id=rid, project_id=pid, volume_id=vid, sequence=sequence, action=action,
        chapter_id=UUID(cid) if cid else None, manuscript_hash=manuscript.get("content_hash"),
        request_hash=fingerprint, before_json=before, manuscript_json=manuscript,
        result_json=deepcopy(result), result_hash=digest(result)))
    await db.flush()
    return result
