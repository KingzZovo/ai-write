"""Pass current author arc decisions to the actual writing context, by ID."""
from app.services.arc_author_material import ArcWritingMaterial, render_arc_author_source
from app.services.arc_progress_contract import ArcProgressError
from app.services.arc_progress_reader import load_arc_progress, require_current_progress
from app.services.entity_sources import digest


async def load_arc_writing_context(db, project_id, volume_id, chapter_id, outline):
    content = outline.content_json if outline is not None else None
    if not isinstance(content, dict) or "_arc" not in content:
        return ArcWritingMaterial()
    legacy = content["_arc"]
    if isinstance(legacy, dict) and not legacy.get("is_arc"):
        return ArcWritingMaterial()
    expected = {"id": str(outline.id), "version": outline.version, "hash": digest(content)}
    view = await load_arc_progress(db, project_id, volume_id)
    if view.source["outline"] != expected:
        raise ArcProgressError("arc_progress_changed", "弧大纲在准备写作材料时已改变，请重新核对。")
    require_current_progress(view)
    target = next((chapter for chapter in view.source["chapters"] if chapter["id"] == str(chapter_id)), None)
    if target is None:
        raise ArcProgressError("arc_progress_changed", "目标章已不属于当前弧，请重新核对章节。")
    try:
        author = render_arc_author_source(view.outline.content_json, chapter_id=str(chapter_id))
    except (ValueError, TypeError) as exc:
        raise ArcProgressError("arc_author_material_invalid", f"当前弧作者材料无法完整用于写作：{exc}") from None
    prior = view.projected_arc(before_global_idx=target["global_idx"]).running_outline
    sections = [author.text] if author.text else []
    if prior:
        sections.append("【作者对本弧前文的进展说明，仍以完整保存正文为准】\n" + prior)
    if (view.progress["phase"] != "completed" and view.progress.get("direction_chapter_id") == str(chapter_id)
            and view.progress["direction"]):
        sections.append("【作者为本章指定的方向，属于创作安排而非已经发生的事实】\n" + view.progress["direction"])
    return ArcWritingMaterial("\n\n".join(sections), author.replaces_volume_outline)


async def load_arc_writing_material(db, project_id, volume_id, chapter_id, outline):
    """Text-only projection for callers that do not also render a volume block."""
    return (await load_arc_writing_context(db, project_id, volume_id, chapter_id, outline)).text
