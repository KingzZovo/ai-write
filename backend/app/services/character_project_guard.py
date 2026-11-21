"""Keep official character graph writes outside a locked planning snapshot."""
import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models.project import Project
from app.services.generation_execution import _drain_cleanup


class CharacterProjectError(ValueError):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code, self.status = code, status


@asynccontextmanager
async def character_graph_write_guard(db, project_id):
    """Use the graph-only API's request session, without committing PG changes.

    SHARE conflicts with the planner's Project UPDATE lock. Release it before
    calling the separate projection connection: that connection takes the
    materialization mutex, which planning acquires before the Project lock.
    The graph's own conditional write still owns profile conflict detection.
    """
    try:
        pid = UUID(str(project_id))
    except (ValueError, TypeError, AttributeError):
        raise CharacterProjectError('character_project_invalid', '作品编号无效。', 422) from None
    try:
        async with asyncio.timeout(90):
            try:
                current = await db.scalar(select(Project.id).where(
                    Project.id == pid, Project.deleted_at.is_(None)).with_for_update(read=True)
                    .execution_options(autoflush=False))
            except SQLAlchemyError:
                raise CharacterProjectError('character_project_unavailable',
                    '作品暂时无法核对，请保留原输入后重试。', 503) from None
            if current is None:
                raise CharacterProjectError('character_project_unavailable', '作品不存在或已删除。', 404)
            yield str(pid)
    finally:
        # Drain rollback even if the request is cancelled again during cleanup.
        await _drain_cleanup(db.rollback())
