"""Freeze only material actually supplied to arc planning, with stable owners."""
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from uuid import UUID

from app.models.project import Project, Volume
from app.services.arc_planning_contract import ArcPlanningError, book_material, source_token
from app.services.arc_progress_contract import ArcProgressError
from app.services.arc_progress_reader import load_arc_progress, require_current_progress
from app.services.arc_sources import ArcSourceError, arc_source_snapshot, load_current_arc
from app.services.outline_authority import outline_for_generation
from app.services.outline_order import positive_index
from app.services.planning_book_material import PlanningBookMaterialError
from app.services.planning_book_sources import read_planning_book_source
from app.services.planning_character_material import PlanningCharacterMaterialError, PlanningCharacterUnavailable
from app.services.planning_character_sources import read_planning_character_source
from app.services.planning_cognition_material import (
    PlanningCognitionMaterialError, PlanningCognitionUnavailable, render_cognition_archive,
)
from app.services.planning_cognition_sources import read_planning_cognition_source
from app.services.world_rule_material import WorldRuleSourceError


async def read_planning_source(db, project_id, *, lock=False):
    pid = UUID(str(project_id))
    if await db.scalar(select(Project.id).where(Project.id == pid, Project.deleted_at.is_(None))
                       .execution_options(autoflush=False)) is None:
        raise ArcPlanningError("arc_planning_project_unavailable", "作品不存在或已删除。", 404)
    volumes = list((await db.execute(select(Volume.id, Volume.volume_idx).where(Volume.project_id == pid)
        .order_by(Volume.volume_idx, Volume.id).execution_options(autoflush=False))).all())
    if (any(not positive_index(v.volume_idx) for v in volumes)
            or len({v.volume_idx for v in volumes}) != len(volumes)):
        raise ArcPlanningError("arc_planning_source_invalid", "当前分卷顺序无效，请先核对。")
    target = (volumes[-1].volume_idx if volumes else 0) + 1
    if target > 2147483647:
        raise ArcPlanningError("arc_planning_source_invalid", "没有可用的后续卷号，请先整理分卷。")
    previous = None
    try:
        index, outline, state = await load_current_arc(db, pid, lock=lock)
        if state is not None:
            volume = next((volume for volume in volumes if volume.volume_idx == index), None)
            if volume is None:
                raise ArcSourceError("前弧在读取期间变化，请重新核对。")
            progress = await load_arc_progress(db, pid, volume.id, lock=lock)
            if progress.outline.id != outline.id or progress.volume.volume_idx != index:
                raise ArcSourceError("前弧大纲在读取期间变化，请重新核对。")
            require_current_progress(progress, completed=True)
            previous = {"arc": arc_source_snapshot(index, outline), "progress": progress.source,
                "material": {"volume_idx": index, "reported_progress": progress.projected_arc().running_outline,
                    "outline": outline_for_generation(outline.content_json)}}
    except (ArcSourceError, ArcProgressError) as exc:
        raise ArcPlanningError("arc_planning_previous_requires_review", str(exc)) from None
    try:
        book_context = await read_planning_book_source(db, pid, lock=lock)
    except PlanningBookMaterialError as exc:
        raise ArcPlanningError("arc_planning_book_material_invalid", str(exc)) from None
    except WorldRuleSourceError as exc:
        code = 'arc_planning_book_material_unavailable' if exc.status == 503 else 'arc_planning_book_material_invalid'
        raise ArcPlanningError(code, str(exc), exc.status) from None
    except SQLAlchemyError:
        raise ArcPlanningError("arc_planning_book_material_unavailable", "全书创作材料暂时无法读取，请保留原请求后重试。", 503) from None
    try:
        character_context = await read_planning_character_source(db, pid, target, lock=lock)
    except PlanningCharacterMaterialError as exc:
        raise ArcPlanningError('arc_planning_character_material_invalid', str(exc)) from None
    except PlanningCharacterUnavailable as exc:
        raise ArcPlanningError('arc_planning_character_material_unavailable', str(exc), 503) from None
    try:
        cognition_context = await read_planning_cognition_source(db, pid, character_context)
    except PlanningCognitionMaterialError as exc:
        raise ArcPlanningError('arc_planning_cognition_material_invalid', str(exc)) from None
    except PlanningCognitionUnavailable as exc:
        raise ArcPlanningError('arc_planning_cognition_material_unavailable', str(exc), 503) from None
    source = {"version": 5, "project_id": str(pid), "target_volume_idx": target,
              "volume_order": [{"id": str(v.id), "volume_idx": v.volume_idx} for v in volumes], "previous": previous,
              "book_context": book_context, "character_context": character_context, "cognition_context": cognition_context}
    if character_context['volume_order'] != sorted(source['volume_order'], key=lambda row: row['id']):
        raise ArcPlanningError('arc_planning_source_changed', '读取期间分卷顺序已变化，请重新核对。')
    return source


def public_planning_source(source):
    return {"source": source_token(source["project_id"], source), "target_volume_idx": source["target_volume_idx"],
            "mode": "next" if source["previous"] is not None else "start",
            "book_material_text": book_material(source),
            "cognition_material_text": render_cognition_archive(source['cognition_context']) if source['version'] == 5 else None,
            "previous_material": source["previous"]["material"] if source["previous"] is not None else None}
