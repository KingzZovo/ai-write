"""ORM models package.

Import all models here so that Alembic and the application can discover them
via a single import of this package.
"""

from app.db.session import Base
from app.models.pipeline import PipelineRun, PipelineChapterStatus
from app.models.prompt import PromptAsset
from app.models.project import (
    BookSource,
    Chapter,
    ChapterEvaluation,
    ChapterVersion,
    Character,
    CrawlTask,
    FilterWord,
    Foreshadow,
    LLMEndpoint,
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
    "ChapterEvaluation",
    "ChapterVersion",
    "Character",
    "CrawlTask",
    "FilterWord",
    "Foreshadow",
    "LLMEndpoint",
    "ModelConfig",
    "Outline",
    "Project",
    "ReferenceBook",
    "StyleProfile",
    "TextChunk",
    "Volume",
    "VolumeSummary",
    "WorldRule",
    "PromptAsset",
    "PipelineRun",
    "PipelineChapterStatus",
]
