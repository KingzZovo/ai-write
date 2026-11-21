"""Explicit request-session double; PostgreSQL blocking has separate tests."""
from unittest.mock import AsyncMock
from uuid import UUID


class CharacterProjectDB:
    def __init__(self, project_id, *, available=True):
        self.project_id, self.available = UUID(str(project_id)), available
        self.held = False
        self.scalar = AsyncMock(side_effect=self._scalar)
        self.rollback = AsyncMock(side_effect=self._rollback)
        self.commit = AsyncMock()

    async def _scalar(self, statement):
        self.held = True
        return self.project_id if self.available else None

    async def _rollback(self):
        self.held = False
