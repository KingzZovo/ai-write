"""Current author arc progress, bounded to one volume and its saved proofs.

The latest receipt owns progress. Outline counters are legacy reports only.
Reading a list never loads all book prose or historical manuscript snapshots.
"""
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.models.arc_progress import ArcProgressWrite
from app.models.project import Chapter, Outline, Project, Volume
from app.services.arc_loop import ArcState, parse_arc_state
from app.services.arc_progress_contract import (
    ArcProgressError, completion_source_hash, initial_progress, is_hash, plan_hash, text_hash, validate_result,
)
from app.services.entity_sources import digest
from app.services.outline_order import UNAVAILABLE, positive_index, volume_condition


def receipt_headers():
    """No before_json/manuscript_json: neither is needed for a current view."""
    return select(ArcProgressWrite.id, ArcProgressWrite.project_id, ArcProgressWrite.volume_id,
        ArcProgressWrite.sequence, ArcProgressWrite.action, ArcProgressWrite.chapter_id,
        ArcProgressWrite.manuscript_hash, ArcProgressWrite.request_hash,
        ArcProgressWrite.result_json, ArcProgressWrite.result_hash)


async def receipt_header(db, request_id):
    return (await db.execute(receipt_headers().where(ArcProgressWrite.id == request_id)
        .execution_options(autoflush=False))).one_or_none()


@dataclass
class ArcProgressView:
    volume: object
    outline: object
    legacy: ArcState
    progress: dict
    source: dict
    chapters: list[dict]
    issues: list[dict]
    latest: object | None

    def public(self):
        confirmed = [chapter for chapter in self.chapters if chapter["state"] == "confirmed"]
        pending = [chapter for chapter in self.chapters if chapter["state"] == "pending"]
        ready = not self.issues and self.progress["phase"] != "completed"
        return {"source": deepcopy(self.source), "title": self.legacy.title,
            "volume_title": self.volume.title, "progress": deepcopy(self.progress),
            "chapters": deepcopy(self.chapters), "issues": deepcopy(self.issues),
            "confirmed_count": len(confirmed),
            "chapter_count": len(self.source["chapters"]),
            "next_chapter_id": pending[0]["id"] if ready and pending else None,
            "legacy": {"unverified": self.latest is None and legacy_unverified(self.legacy),
                       "reported_count": self.legacy.chapters_written, "status": self.legacy.status,
                       "history_request_id": self.progress["legacy_request_id"]}}

    def projected_arc(self, *, before_global_idx=None):
        """Compatibility read projection; this is never written into Outline."""
        public = self.public()
        notes = [f"第{chapter['chapter_idx']}章 {chapter['title']}：{chapter['confirmation']['author_note']}"
                 for chapter in self.chapters if chapter["state"] == "confirmed"
                 and (before_global_idx is None or chapter["global_idx"] < before_global_idx)
                 and chapter["confirmation"]["author_note"]]
        return ArcState(title=self.legacy.title, core_setup=self.legacy.core_setup,
            opening_scene=self.legacy.opening_scene, target_chapters=self.legacy.target_chapters,
            status=self.progress["phase"], chapters_written=public["confirmed_count"],
            running_outline="\n".join(notes), next_direction=self.progress["direction"]
                if self.progress.get("direction_chapter_id") == public["next_chapter_id"] else None)


def legacy_unverified(state):
    return (state.chapters_written > 0 or state.status == "completed"
            or bool(state.running_outline.strip()) or bool((state.next_direction or "").strip()))


async def _outline(db, pid, vid, *, lock):
    if await db.scalar(select(Project.id).where(Project.id == pid, Project.deleted_at.is_(None))
                       .execution_options(autoflush=False)) is None:
        raise ArcProgressError("arc_progress_unavailable", "作品不存在或已删除。", 404)
    query = select(Volume).where(Volume.id == vid, Volume.project_id == pid)
    if lock:
        query = query.with_for_update()
    volume = await db.scalar(query.execution_options(autoflush=False, populate_existing=True))
    if volume is None:
        raise ArcProgressError("arc_progress_unavailable", "分卷不存在或不属于本书。", 404)
    positions = list((await db.execute(select(Volume.id).where(Volume.project_id == pid,
        Volume.volume_idx == volume.volume_idx).limit(2).execution_options(autoflush=False))).scalars())
    if not positive_index(volume.volume_idx) or positions != [vid]:
        raise ArcProgressError("arc_progress_invalid", "分卷顺序无效，请先核对。")
    # Only the selected current outline is loaded in full, even after many revisions.
    query = select(Outline).where(Outline.project_id == pid, Outline.level == "volume",
        volume_condition(vid, volume.volume_idx)).order_by(Outline.is_confirmed.desc(),
        Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1)
    if lock:
        query = query.with_for_update()
    outline = await db.scalar(query.execution_options(autoflush=False, populate_existing=True))
    content = outline.content_json if outline is not None else None
    if outline is None or outline.is_confirmed != 1:
        raise ArcProgressError("arc_progress_unavailable", "本卷没有已确认的当前弧大纲。", 404)
    if (not isinstance(content, dict) or content.get(UNAVAILABLE)
            or type(content.get("volume_idx")) is not int or content["volume_idx"] != volume.volume_idx
            or content.get("volume_id") is not None and content["volume_id"] != str(vid)):
        raise ArcProgressError("arc_progress_invalid", "当前弧大纲的分卷来源不一致，请先核对。")
    try:
        legacy = parse_arc_state(content)
        digest(content)
        if legacy is None:
            raise ValueError("没有弧信息")
    except (ValueError, TypeError) as exc:
        raise ArcProgressError("arc_progress_invalid", f"当前弧大纲无效：{exc}") from None
    return volume, outline, legacy


async def _chapters(db, vid, progress, *, lock):
    query = select(Chapter.id, Chapter.title, Chapter.chapter_idx, Chapter.global_idx, Chapter.content_hash
        ).where(Chapter.volume_id == vid).order_by(Chapter.chapter_idx, Chapter.id).limit(41)
    if lock:
        query = query.with_for_update()
    rows = (await db.execute(query.execution_options(autoflush=False))).all()
    if (len(rows) > 40 or any(not positive_index(row.chapter_idx) or not positive_index(row.global_idx) for row in rows)
            or len({row.chapter_idx for row in rows}) != len(rows)
            or len({row.global_idx for row in rows}) != len(rows)):
        raise ArcProgressError("arc_progress_invalid", "本弧章数超过40章，或章节顺序无效，请先整理分卷。")
    chapters = [{"id": str(row.id), "title": row.title, "chapter_idx": row.chapter_idx,
                 "global_idx": row.global_idx, "content_hash": row.content_hash if is_hash(row.content_hash) else None} for row in rows]
    # Trigger-maintained hashes are cheap. Unknown hashes require the complete
    # current prose only for chapters that already have author confirmations.
    acknowledgements = progress["acknowledged"] if progress else {}
    unknown = [UUID(chapter["id"]) for chapter in chapters
               if chapter["id"] in acknowledgements and not is_hash(chapter["content_hash"])]
    if unknown:
        query = select(Chapter.id, Chapter.content_text).where(Chapter.volume_id == vid, Chapter.id.in_(unknown))
        if lock:
            query = query.with_for_update()
        current = {str(row.id): text_hash(row.content_text or "") for row in
                   (await db.execute(query.execution_options(autoflush=False))).all()}
        if set(current) != set(map(str, unknown)):
            raise ArcProgressError("arc_progress_changed", "读取期间章节已改变，请重新核对。")
        for chapter in chapters:
            if chapter["id"] in current:
                chapter["content_hash"] = current[chapter["id"]]
    return chapters


async def _verify_confirmations(db, pid, vid, progress, sequence):
    ids = {UUID(proof["request_id"]) for proof in progress["acknowledged"].values()}
    legacy_id = progress["legacy_request_id"]
    if legacy_id:
        ids.add(UUID(legacy_id))
    if not ids:
        return
    rows = (await db.execute(receipt_headers().where(ArcProgressWrite.id.in_(ids))
        .execution_options(autoflush=False))).all()
    receipts = {str(row.id): row for row in rows}
    for row in rows:
        if row.project_id != pid or row.volume_id != vid or row.sequence > sequence:
            raise ArcProgressError("arc_progress_receipt_invalid", "章节确认的历史来源不属于当前弧。")
        validate_result(row)
    for cid, proof in progress["acknowledged"].items():
        row = receipts.get(proof["request_id"])
        if (row is None or row.action != "acknowledge" or str(row.chapter_id) != cid
                or row.manuscript_hash != proof["content_hash"]
                or row.result_json["progress"]["acknowledged"].get(cid) != proof):
            raise ArcProgressError("arc_progress_receipt_invalid", "章节确认缺少对应的原保存回执，不能按计数继续。")
    if legacy_id and (legacy_id not in receipts or receipts[legacy_id].sequence != 1):
        raise ArcProgressError("arc_progress_receipt_invalid", "旧进度留存位置无法核对，请保留历史。")


async def load_arc_progress(db, project_id, volume_id, *, lock=False):
    pid, vid = UUID(str(project_id)), UUID(str(volume_id))
    volume, outline, legacy = await _outline(db, pid, vid, lock=lock)
    latest = (await db.execute(receipt_headers().where(ArcProgressWrite.project_id == pid,
        ArcProgressWrite.volume_id == vid).order_by(ArcProgressWrite.sequence.desc()).limit(1)
        .execution_options(autoflush=False))).one_or_none()
    progress = validate_result(latest)["progress"] if latest else None
    chapters = await _chapters(db, vid, progress, lock=lock)
    if progress is None:
        progress = initial_progress(vid, outline.content_json, [chapter["id"] for chapter in chapters], legacy)
    else:
        await _verify_confirmations(db, pid, vid, progress, latest.sequence)
    source = {"schema": 1, "project_id": str(pid), "volume_id": str(vid), "volume_idx": volume.volume_idx,
        "volume_title": volume.title,
        "outline": {"id": str(outline.id), "version": outline.version, "hash": digest(outline.content_json)},
        "latest": {"request_id": str(latest.id), "sequence": latest.sequence, "result_hash": latest.result_hash} if latest else None,
        "chapters": chapters}
    issues = []
    if progress["plan_hash"] != plan_hash(outline.content_json):
        issues.append({"code": "plan_changed", "message": "弧大纲已修改，请核对后采用当前安排。"})
    if progress["chapter_order"] != [chapter["id"] for chapter in chapters]:
        issues.append({"code": "order_changed", "message": "章节清单或顺序已改变，请核对后采用当前安排。"})
    if latest is None and legacy_unverified(legacy):
        issues.append({"code": "legacy_unverified", "message": "旧报告尚未对应具体保存稿，旧方向也未绑定章节。请先采用当前安排，再逐章确认并明确指定方向；原记录会完整保留在首次进度历史中。"})
    if (latest is None and "chapter_order" not in outline.content_json["_arc"]
            and len(chapters) != legacy.target_chapters):
        issues.append({"code": "legacy_structure_unverified", "message": "旧弧的原章数与实际章节清单不一致，请明确核对并采用当前安排。"})
    if progress["phase"] == "completed" and progress["completion_source_hash"] != completion_source_hash(progress):
        issues.append({"code": "completion_changed", "message": "弧安排已修改，原结束决定需要重新确认；也可以明确继续本弧。"})
    items = []
    for chapter in chapters:
        proof = progress["acknowledged"].get(chapter["id"])
        state = "pending" if proof is None else ("confirmed" if proof["content_hash"] == chapter["content_hash"]
            and proof["title"] == chapter["title"] else "changed")
        items.append({**chapter, "state": state, "confirmation": deepcopy(proof)})
        if state == "changed":
            issues.append({"code": "manuscript_changed", "chapter_id": chapter["id"],
                           "message": f"第{chapter['chapter_idx']}章的标题或正文已改变，请重读确认或撤销旧确认。"})
    current_ids = {chapter["id"] for chapter in chapters}
    for cid, proof in progress["acknowledged"].items():
        if cid not in current_ids:
            items.append({"id": cid, "title": proof["title"], "chapter_idx": None, "global_idx": None,
                          "content_hash": None, "state": "removed", "confirmation": deepcopy(proof)})
            issues.append({"code": "chapter_removed", "chapter_id": cid,
                           "message": f"已确认的《{proof['title']}》已移出或删除，请明确撤销该章确认后再采用新安排。"})
    return ArcProgressView(volume, outline, legacy, progress, source, items, issues, latest)


def require_current_progress(view, *, completed=False):
    if view.issues:
        raise ArcProgressError("arc_progress_needs_reconciliation", "进度来源已变化，请先逐项核对章节和弧安排。")
    if completed and (view.latest is None or view.progress["phase"] != "completed"):
        raise ArcProgressError("arc_progress_not_completed", "请先完成逐章确认，或明确提前结束当前弧。")
