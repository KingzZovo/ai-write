"""Select an arc from current volume identities and confirmed outline versions."""
from copy import deepcopy
from uuid import UUID

from sqlalchemy import or_, select

from app.models.project import Outline, Volume
from app.services.arc_loop import parse_arc_state
from app.services.outline_order import UNAVAILABLE, positive_index


class ArcSourceError(ValueError):
    pass


def arc_source_snapshot(volume_idx, outline):
    if outline is None:
        return {"volume_idx": None, "outline": None}
    return {"volume_idx": volume_idx, "outline": {
        "id": str(outline.id), "project_id": str(outline.project_id),
        "parent_id": str(outline.parent_id) if outline.parent_id else None,
        "version": outline.version, "is_confirmed": outline.is_confirmed,
        "content_json": deepcopy(outline.content_json),
    }}


def _declares_arc(content):
    if not isinstance(content, dict) or "_arc" not in content:
        return False
    state = content["_arc"]
    return not isinstance(state, dict) or bool(state.get("is_arc"))


async def load_current_arc(db, project_id, *, lock=False):
    """Never resurrect a superseded version or guess an orphan's current owner.

    Selection matches the volume-outline editor: confirmed, then version,
    created_at and ID. Only that chosen version may declare a volume an arc.
    The caller owns the project structure lock for writes. Only selection
    metadata is read for older volumes, never their full plans or prose.
    """
    pid = UUID(str(project_id))
    volumes = list((await db.execute(select(Volume.id, Volume.volume_idx).where(
        Volume.project_id == pid).execution_options(autoflush=False))).all())
    by_id = {str(volume.id): volume for volume in volumes}
    by_index = {volume.volume_idx: volume for volume in volumes}
    if len(by_index) != len(volumes) or any(not positive_index(v.volume_idx) for v in volumes):
        raise ArcSourceError("分卷编号重复或无效，请先核对原分卷")

    query = select(Outline.id, Outline.version, Outline.is_confirmed,
        Outline.content_json["volume_id"].label("owner_id"),
        Outline.content_json["volume_idx"].label("owner_idx"),
        or_(Outline.content_json["_arc"].as_string().is_not(None),
            Outline.content_json["arc_plan_version"].as_string().is_not(None)).label("potential_arc"),
    ).where(Outline.project_id == pid, Outline.level == "volume").order_by(
        Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc())
    rows = (await db.execute(query.execution_options(autoflush=False))).all()

    async def full_outline(row):
        query = select(Outline).where(Outline.id == row.id, Outline.project_id == pid, Outline.level == "volume")
        if lock:
            query = query.with_for_update()
        result = await db.scalar(query.execution_options(populate_existing=True, autoflush=False))
        if result is None or result.version != row.version or result.is_confirmed != row.is_confirmed:
            raise ArcSourceError("弧大纲在读取期间改变，请重新核对")
        return result

    selected = {}
    for row in rows:
        if row.owner_id is not None:
            volume = by_id.get(str(row.owner_id))
        else:
            volume = by_index.get(row.owner_idx) if positive_index(row.owner_idx) else None
        if volume is None:
            if row.is_confirmed == 1 and row.potential_arc:
                orphan = await full_outline(row)
                if _declares_arc(orphan.content_json):
                    raise ArcSourceError("弧大纲的原分卷已无法确认，不能按旧卷号借用其他分卷")
            continue
        selected.setdefault(str(volume.id), (volume, row))

    for volume, row in sorted(selected.values(), key=lambda item: item[0].volume_idx, reverse=True):
        if row.is_confirmed != 1 or not row.potential_arc:
            continue
        outline = await full_outline(row)
        content = outline.content_json
        if not _declares_arc(content):
            continue
        if (content.get(UNAVAILABLE) or not positive_index(content.get("volume_idx"))
                or content["volume_idx"] != volume.volume_idx
                or content.get("volume_id") is not None and str(content["volume_id"]) != str(volume.id)):
            raise ArcSourceError("弧大纲与当前分卷顺序不一致，请先核对大纲")
        try:
            state = parse_arc_state(content)
        except (ValueError, TypeError) as exc:
            raise ArcSourceError(f"弧进度记录无效：{exc}") from None
        if state is None:
            raise ArcSourceError("弧进度记录无效，不能猜测下一章")
        return volume.volume_idx, outline, state
    return None, None, None
