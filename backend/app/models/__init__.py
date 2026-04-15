"""ORM models package.

Import all models here so that Alembic and the application can discover them
via a single import of this package.
"""

from app.db.session import Base
from app.models.project import (
    BookSource,
    Chapter,
    Character,
    CrawlTask,
    Foreshadow,
    ModelConfig,
    Outline,
    Project,
    ReferenceBook,
    StyleProfile,
    TextChunk,
    Volume,
    VolumeSummary,
    WorldRule,
)

__all__ = [
    "Base",
    "BookSource",
    "Chapter",
    "Character",
    "CrawlTask",
    "Foreshadow",
    "ModelConfig",
    "Outline",
    "Project",
    "ReferenceBook",
    "StyleProfile",
    "TextChunk",
    "Volume",
    "VolumeSummary",
    "WorldRule",
]
