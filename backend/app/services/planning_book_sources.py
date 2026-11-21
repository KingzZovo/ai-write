"""Read the selected full book plan and rules without historical chapter prose."""
from copy import deepcopy
from uuid import UUID

from sqlalchemy import select

from app.models.project import Outline, Project
from app.services.planning_book_material import PlanningBookMaterialError, book_source_material
from app.services.world_rule_sources import read_world_rule_source


async def read_planning_book_source(db, project_id, *, lock=False):
    """lock=True requires the caller's Project/structure lock before child rows.

    The Project FOR UPDATE excludes new child inserts via their FK check;
    child locks protect competing book versions. Official graph rule writes
    take the Project NO KEY UPDATE lock before changing registered settings.
    """
    pid = UUID(str(project_id))
    project = (await db.execute(select(Project.id, Project.title, Project.genre, Project.premise, Project.target_word_count)
        .where(Project.id == pid, Project.deleted_at.is_(None)).execution_options(autoflush=False))).one_or_none()
    if project is None:
        raise PlanningBookMaterialError('本书来源在读取期间变化，请重新核对。')
    if lock:
        # Confirming another saved book candidate can change the winner without
        # locking Project. Lock every candidate ID, but read only the winner's
        # full text, so historical plans do not enter the material or memory.
        (await db.execute(select(Outline.id).where(Outline.project_id == pid, Outline.level == 'book')
            .order_by(Outline.id).with_for_update().execution_options(autoflush=False))).all()
    query = select(Outline.id, Outline.project_id, Outline.version, Outline.is_confirmed, Outline.content_json).where(
        Outline.project_id == pid, Outline.level == 'book').order_by(
        Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1)
    if lock:
        query = query.with_for_update()
    book = (await db.execute(query.execution_options(autoflush=False))).one_or_none()
    rules = await read_world_rule_source(db, pid)
    source = {'version': 2, 'project': {'id': str(project.id), 'title': project.title, 'genre': project.genre,
        'premise': project.premise, 'target_word_count': project.target_word_count},
        'outline': {'id': str(book.id), 'project_id': str(book.project_id), 'version': book.version,
                    'is_confirmed': book.is_confirmed, 'content_json': deepcopy(book.content_json)} if book else None,
        'world_rules': rules}
    book_source_material(source, pid)
    return source
