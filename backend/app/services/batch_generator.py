"""Batch Chapter Generation Service.

Generates multiple chapters in sequence from confirmed outlines and
persists the resulting text back to the ``chapters`` table.

This module was previously calling ``ChapterGenerator.generate`` with the
pre-ContextPack signature (``project_settings/world_rules/...``), which has
been gone for a long time. As a result every ``/api/generate/batch`` call
failed instantly with ``TypeError: unexpected keyword argument`` and no
chapter was ever written. The fix wires the call up to the current
signature, threads through an ``AsyncSession``, and writes the generated
text back to ``chapters.content_text`` (so the user actually sees the
body after the SSE stream finishes).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter
from app.services.chapter_generator import ChapterGenerator
from app.services.hook_manager import HookManager

logger = logging.getLogger(__name__)

# Anything shorter than this (post-strip) is treated as a silent generator
# failure. The default cap on real chapters is ~1-3k characters; this only
# catches genuinely empty / single-line returns.
_MIN_CHAPTER_BYTES = 200


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


ProgressCallback = Callable[[BatchJobStatus], None]


class BatchGenerator:
    """Generates multiple chapters sequentially with hook integration."""

    def __init__(self) -> None:
        self.generator = ChapterGenerator()
        self.hook_manager = HookManager()
        self._paused_jobs: set[str] = set()

    async def generate_batch(
        self,
        project_id: str,
        chapter_configs: list[dict],
        *,
        db: AsyncSession | None = None,
        style_instruction: str = "",
        on_progress: ProgressCallback | None = None,
    ) -> BatchJobStatus:
        """Generate a batch of chapters sequentially.

        ``db`` is required for real LLM generation — it is threaded into
        ``ChapterGenerator.generate`` (which builds a context pack via the
        DB) and used to persist the output back onto the ``chapters`` row.
        It is kept optional only so unit tests can stub the generator and
        still exercise the orchestration logic.
        """
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
            # Pause check
            if job_id in self._paused_jobs:
                job.status = BatchStatus.PAUSED
                break

            job.current_chapter = i + 1
            job.results[i].status = "generating"
            if on_progress:
                on_progress(job)

            try:
                # ----- pre-hooks -----
                hook_result = await self.hook_manager.run_pre_hooks(
                    project_id=project_id,
                    volume_id=config.get("volume_id", ""),
                    chapter_idx=config.get("chapter_idx", i + 1),
                    chapter_outline=config.get("outline", {}),
                )
                # Previously: ``if not can_proceed AND errors:`` — a hook
                # that returned ``can_proceed=False`` with an empty error
                # list fell through to generation, which is exactly the
                # opposite of what «blocked» means. Surface it as an error
                # unconditionally and synthesise a message when missing.
                if not hook_result.can_proceed:
                    job.results[i].status = "error"
                    job.results[i].error = (
                        "; ".join(hook_result.errors)
                        if hook_result.errors
                        else "pre-hook blocked chapter (no message)"
                    )
                    if on_progress:
                        on_progress(job)
                    continue

                # ----- generate -----
                user_instruction = config.get("user_instruction") or ""
                if style_instruction:
                    user_instruction = (
                        f"{style_instruction}\n\n{user_instruction}".strip()
                    )

                text = await self.generator.generate(
                    project_id=project_id,
                    volume_id=config.get("volume_id", ""),
                    chapter_idx=config.get("chapter_idx", i + 1),
                    db=db,
                    chapter_id=config.get("chapter_id"),
                    user_instruction=user_instruction,
                )

                stripped = (text or "").strip()
                if len(stripped) < _MIN_CHAPTER_BYTES:
                    job.results[i].status = "error"
                    job.results[i].error = (
                        f"generator returned {len(stripped)} chars "
                        f"(< {_MIN_CHAPTER_BYTES}); treated as silent failure"
                    )
                    job.results[i].word_count = len(stripped)
                    logger.warning(
                        "Batch generation chapter %d returned only %d chars",
                        config.get("chapter_idx", i + 1),
                        len(stripped),
                    )
                    if on_progress:
                        on_progress(job)
                    continue

                # ----- persist to chapters table -----
                cid = config.get("chapter_id")
                if db is not None and cid:
                    chapter = await db.get(Chapter, cid)
                    if chapter is None:
                        logger.warning(
                            "batch_generator: chapter %s not found, text not persisted",
                            cid,
                        )
                    else:
                        chapter.content_text = text
                        chapter.word_count = len(text)
                        chapter.status = "completed"
                        await db.flush()

                job.results[i].status = "completed"
                job.results[i].word_count = len(text)
                job.completed_chapters += 1

                # ----- post-hooks (best-effort) -----
                try:
                    await self.hook_manager.run_post_hooks(
                        project_id=project_id,
                        volume_id=config.get("volume_id", ""),
                        chapter_idx=config.get("chapter_idx", i + 1),
                        chapter_text=text,
                    )
                except Exception:
                    logger.exception(
                        "post-hooks failed for chapter %d (text already persisted)",
                        config.get("chapter_idx", i + 1),
                    )

                # Threaded continuity for the next chapter
                if i + 1 < len(chapter_configs):
                    chapter_configs[i + 1]["previous_text"] = text

            except Exception as exc:
                logger.exception("Batch generation failed at chapter %d", i + 1)
                job.results[i].status = "error"
                job.results[i].error = str(exc)

            if on_progress:
                on_progress(job)

        if db is not None:
            try:
                await db.commit()
            except Exception:
                logger.exception("batch_generator: final commit failed")
                await db.rollback()

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
