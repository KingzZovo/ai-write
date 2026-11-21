"""ORM models package.

Import all models here so that Alembic and the application can discover them
via a single import of this package.
"""

from app.db.session import Base
from app.models.ask_user import AskUserPause
from app.models.arc_progress import ArcProgressWrite
from app.models.arc_planning import ArcPlanningRequest
from app.models.author_dossier import AuthorDossier
from app.models.call_log import LLMCallLog
from app.models.cascade_task import CascadeTask
from app.models.decompile import BeatSheetCard, ReferenceBookSlice, StyleProfileCard
from app.models.generation_run import CriticReport, GenerationRun, GenerationRunSubmission  # noqa: F401
from app.models.entity_extraction import ChapterEntityExtraction
from app.models.cognition import (  # noqa: F401
    ChapterCognition,
    CognitionBackfill,
    CognitionRequest,
    CognitionRevision,
)
from app.models.generation_task import GenerationTask
from app.models.outline_write import VolumeOutlineWrite
from app.models.pipeline import PipelineRun, PipelineChapterStatus
from app.models.prompt import PromptAsset
from app.models.settings_change_log import SettingsChangeLog
from app.models.usage_quota import UsageQuota
from app.models.writing_engine import (  # noqa: F401
    AntiAITrap,
    BeatPattern,
    GenreProfile,
    ToolSpec,
    WritingRule,
)
from app.models.project import (
    BookSource,
    Chapter,
    ChapterEvaluation,
    ChapterVariant,
    ChapterVersion,
    Character,
    CrawlTask,
    FilterWord,
    Foreshadow,
    LLMEndpoint,
    Outline,
    Project,
    ReferenceBook,
    StyleProfile,
    TextChunk,
    Volume,
    VolumeSummary,
    WorldRule,
    Organization,
    CharacterOrganization,
)

from app.models.generation_batch import GenerationBatch, GenerationBatchItem

__all__ = [
    "Base",
    "AntiAITrap",
    "AskUserPause",
    "ArcProgressWrite",
    "ArcPlanningRequest",
    "AuthorDossier",
    "BeatPattern",
    "BeatSheetCard",
    "BookSource",
    "CascadeTask",
    "Chapter",
    "ChapterCognition",
    "CognitionRequest",
    "CognitionBackfill",
    # 2026-11-17 审计 F9:该模型只在 app/api/cognition.py 间接进 metadata,
    # 只 import app.models 的上下文(autogenerate/脚本)会把它判成多余表。
    "CognitionRevision",
    "ChapterEntityExtraction",
    "ChapterEvaluation",
    "ChapterVariant",
    "ChapterVersion",
    "Character",
    "CharacterOrganization",
    "CrawlTask",
    "FilterWord",
    "Foreshadow",
    "Organization",
    "GenreProfile",
    "LLMCallLog",
    "LLMEndpoint",
    "Outline",
    "VolumeOutlineWrite",
    "Project",
    "ReferenceBook",
    "ReferenceBookSlice",
    "SettingsChangeLog",
    "StyleProfile",
    "StyleProfileCard",
    "TextChunk",
    "ToolSpec",
    "Volume",
    "VolumeSummary",
    "WorldRule",
    "WritingRule",
    "PromptAsset",
    "PipelineRun",
    "PipelineChapterStatus",
    "GenerationTask",
    "GenerationBatch",
    "GenerationBatchItem",
    "UsageQuota",
]
