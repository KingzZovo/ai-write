"""Read current graph characters at the real, persisted end-of-book boundary."""
from dataclasses import asdict
import asyncio
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models.project import Chapter, Volume
from app.services.entity_context import verified_world_snapshot
from app.services.planning_character_material import (
    PlanningCharacterMaterialError, PlanningCharacterUnavailable, character_source_material,
)


async def current_character_driver():
    from app.db.neo4j import get_neo4j
    async for driver in get_neo4j():
        return driver
    raise PlanningCharacterUnavailable('当前人物图谱暂时无法读取，请保留原请求后重试。')


async def read_planning_character_source(db, project_id, target_volume_idx, *, lock=False):
    """Project is locked by the caller before locking Volume/Chapter rows.

    Those row locks also exclude the source-locked entity publisher during the
    final source check. Full prose is read only to verify referenced facts, in
    the existing bounded source batches; it is never sent to every arc prompt.
    """
    pid = UUID(str(project_id))
    volumes = select(Volume.id, Volume.volume_idx).where(Volume.project_id == pid).order_by(Volume.id)
    chapters = select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx, Chapter.global_idx, Chapter.status).join(
        Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == pid).order_by(Chapter.id)
    if lock:
        volumes = volumes.with_for_update()
        chapters = chapters.with_for_update(of=Chapter)
    try:
        volume_rows = (await db.execute(volumes.execution_options(autoflush=False))).all()
        chapter_rows = (await db.execute(chapters.execution_options(autoflush=False))).all()
        if any(type(row.global_idx) is not int or row.global_idx <= 0 for row in chapter_rows):
            raise PlanningCharacterMaterialError('人物状态没有可靠的全书时间边界，请先核对卷章顺序。')
        source = {'version': 1, 'project_id': str(pid), 'target_volume_idx': target_volume_idx,
            'before_global_idx': max((row.global_idx for row in chapter_rows), default=0) + 1,
            'volume_order': [dict(id=str(row.id), volume_idx=row.volume_idx) for row in volume_rows],
            'chapter_order': [dict(id=str(row.id), volume_id=str(row.volume_id), chapter_idx=row.chapter_idx,
                                   global_idx=row.global_idx, status=row.status) for row in chapter_rows],
            'profiles': [], 'states': [], 'relationships': [], 'locations': []}
        character_source_material(source, pid, target_volume_idx)
        async with asyncio.timeout(90):
            driver = await current_character_driver()
            snapshot = await verified_world_snapshot(db, driver, pid, source['before_global_idx'], include_profiles=True)
        source.update(profiles=sorted(snapshot.profiles, key=lambda row: (row['name'], row['node_id'])),
                      states=sorted((asdict(state) for state in snapshot.characters), key=lambda row: row['name']),
                      relationships=sorted((asdict(rel) for rel in snapshot.relationships),
                          key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False)),
                      locations=snapshot.location_records)
        character_source_material(source, pid, target_volume_idx)
        return source
    except PlanningCharacterMaterialError:
        raise
    except SQLAlchemyError:
        raise PlanningCharacterUnavailable('当前人物正文来源暂时无法读取，请保留原请求后重试。') from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningCharacterMaterialError('当前人物状态或关系记录无法核对，请保留原材料并检查当前来源。') from None
    except Exception:
        raise PlanningCharacterUnavailable('当前人物图谱暂时无法读取，请保留原请求后重试。') from None
