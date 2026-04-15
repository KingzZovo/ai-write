"""
Batch Chapter Generation Service

Generates multiple chapters in sequence from confirmed outlines.
Each chapter generates → post-hooks run → next chapter starts.
Supports pause/resume and partial failure handling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from app.services.chapter_generator import ChapterGenerator
from app.services.hook_manager import HookManager

logger = logging.getLogger(__name__)


class BatchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class BatchChapterResult:
    chapter_idx: int
    chapter_id: str
    status: str = "pending"  # pending, generating, completed, error
    word_count: int = 0
    error: str = ""


@dataclass
class BatchJobStatus:
    job_id: str
    project_id: str
    total_chapters: int
    completed_chapters: int = 0
    current_chapter: int = 0
    status: BatchStatus = BatchStatus.PENDING
    results: list[BatchChapterResult] = field(default_factory=list)
    error: str = ""


class BatchGenerator:
    """Generates multiple chapters sequentially with hook integration."""

    def __init__(self):
        self.generator = ChapterGenerator()
        self.hook_manager = HookManager()
        self._paused_jobs: set[str] = set()

    async def generate_batch(
        self,
        project_id: str,
        chapter_configs: list[dict],
        style_instruction: str = "",
        on_progress: callable = None,
    ) -> BatchJobStatus:
        """
        Generate a batch of chapters sequentially.

        Args:
            project_id: Project ID
            chapter_configs: List of dicts with keys:
                - chapter_id, volume_id, chapter_idx, outline
            style_instruction: Style guidance for all chapters
            on_progress: Callback(BatchJobStatus) called after each chapter

        Returns:
            Final BatchJobStatus
        """
        import uuid
        job_id = str(uuid.uuid4())

        job = BatchJobStatus(
            job_id=job_id,
            project_id=project_id,
            total_chapters=len(chapter_configs),
            status=BatchStatus.RUNNING,
            results=[
                BatchChapterResult(
                    chapter_idx=cfg.get("chapter_idx", i + 1),
                    chapter_id=cfg.get("chapter_id", ""),
                )
                for i, cfg in enumerate(chapter_configs)
            ],
        )

        for i, config in enumerate(chapter_configs):
            # Check for pause
            if job_id in self._paused_jobs:
                job.status = BatchStatus.PAUSED
                break

            job.current_chapter = i + 1
            job.results[i].status = "generating"

            if on_progress:
                on_progress(job)

            try:
                # Run pre-hooks
                hook_result = await self.hook_manager.run_pre_hooks(
                    project_id=project_id,
                    volume_id=config.get("volume_id", ""),
                    chapter_idx=config.get("chapter_idx", i + 1),
                    chapter_outline=config.get("outline", {}),
                )

                if not hook_result.can_proceed and hook_result.errors:
                    job.results[i].status = "error"
                    job.results[i].error = "; ".join(hook_result.errors)
                    continue

                # Generate chapter
                text = await self.generator.generate(
                    project_settings=config.get("project_settings", {}),
                    world_rules=config.get("world_rules", []),
                    book_outline_summary=config.get("book_outline_summary", ""),
                    chapter_outline=config.get("outline", {}),
                    previous_chapter_text=config.get("previous_text", ""),
                    style_instruction=style_instruction,
                    user_instruction=config.get("user_instruction", ""),
                )

                job.results[i].status = "completed"
                job.results[i].word_count = len(text)
                job.completed_chapters += 1

                # Run post-hooks
                await self.hook_manager.run_post_hooks(
                    project_id=project_id,
                    volume_id=config.get("volume_id", ""),
                    chapter_idx=config.get("chapter_idx", i + 1),
                    chapter_text=text,
                )

                # Update previous text for next chapter
                if i + 1 < len(chapter_configs):
                    chapter_configs[i + 1]["previous_text"] = text

            except Exception as e:
                logger.exception("Batch generation failed at chapter %d", i + 1)
                job.results[i].status = "error"
                job.results[i].error = str(e)

            if on_progress:
                on_progress(job)

        if job.status != BatchStatus.PAUSED:
            if all(r.status == "completed" for r in job.results):
                job.status = BatchStatus.COMPLETED
            elif any(r.status == "error" for r in job.results):
                job.status = BatchStatus.ERROR
            else:
                job.status = BatchStatus.COMPLETED

        return job

    def pause_job(self, job_id: str) -> None:
        self._paused_jobs.add(job_id)

    def resume_job(self, job_id: str) -> None:
        self._paused_jobs.discard(job_id)
