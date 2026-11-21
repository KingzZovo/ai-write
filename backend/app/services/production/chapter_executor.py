"""Delegate a whole-book chapter to the existing asynchronous generation chain.

No second prose prompt or quality loop lives here. The existing chain owns
ContextPack, style selection, drafting, review, repair and entity dispatch.
This adapter owns durable chapter-task identity and fenced persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.models.generation_task import GenerationTask
from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.models.project import Chapter, Project, Volume
from app.services.production.control import content_hash
from app.services.production.fencing import StaleChapterWrite, chapter_write_fence


@dataclass(frozen=True)
class ChapterOutcome:
    state: str
    word_count: int = 0
    error: str | None = None
    retryable: bool = False


def _outcome(task, chapter, status) -> ChapterOutcome:
    usable = bool(chapter.content_text and chapter.content_text.strip()) and (
        status.saved_hash == content_hash(chapter.content_text)
    )
    if usable:
        if task.status == "completed" and chapter.status == "completed":
            return ChapterOutcome("completed", chapter.word_count)
        return ChapterOutcome("needs_review", chapter.word_count, task.error_message or "草稿已保留，请审核后继续")
    return ChapterOutcome("failed", error=task.error_message or "本次生成没有成功保存可用正文，原稿已保留", retryable=task.status == "failed")


async def execute_chapter(sessions, pipeline_id, command_id, worker_id, status_id) -> ChapterOutcome:
    async with sessions() as db:
        pipeline = await db.scalar(select(PipelineRun).where(PipelineRun.id == UUID(str(pipeline_id))).with_for_update())
        if pipeline is None or str(pipeline.command_id) != str(command_id) or str(pipeline.worker_id) != str(worker_id) or pipeline.state != "generating":
            raise StaleChapterWrite("任务执行权已变更")
        project = await db.get(Project, pipeline.project_id)
        status = await db.get(PipelineChapterStatus, UUID(str(status_id)))
        if status is None or status.pipeline_id != pipeline.id:
            return ChapterOutcome("failed", error="章节检查点不属于当前任务")
        chapter = await db.get(Chapter, status.chapter_id)
        volume = await db.get(Volume, chapter.volume_id) if chapter else None
        if project is None or project.deleted_at is not None or volume is None or volume.project_id != pipeline.project_id or (
            pipeline.volume_id is not None and volume.id != pipeline.volume_id
        ):
            return ChapterOutcome("failed", error="章节所属作品或分卷已变更，已停止写入")
        previous = await db.get(GenerationTask, status.generation_task_id) if status.generation_task_id else None
        if previous and previous.status in ("completed", "needs_review"):
            outcome = _outcome(previous, chapter, status)
            if outcome.state == "completed" or (outcome.state == "needs_review" and status.state == "generating"):
                return outcome  # Worker died after the canonical save, before projection.
        options = pipeline.options_json or {}
        # Runtime identifiers always override caller-supplied options.
        params = {key: options[key] for key in (
            "user_input", "style_id", "structure_book_id", "enable_polish", "skip_polish", "target_words",
        ) if key in options}
        params.update({
            "chapter_id": str(chapter.id), "volume_idx": volume.volume_idx,
            "chapter_idx": chapter.chapter_idx, "pipeline_id": str(pipeline.id),
            "pipeline_command_id": str(command_id),
        })
        task = GenerationTask(project_id=pipeline.project_id, task_type="chapter", params_json=params)
        db.add(task)
        await db.flush()
        task_id, chapter_id, version = task.id, chapter.id, chapter.updated_at
        status.generation_task_id = task.id
        status.attempt_count = (status.attempt_count or 0) + 1
        status.state = "generating"
        status.started_at = datetime.now(timezone.utc)
        status.completed_at = None
        status.error_message = None
        pipeline.current_chapter_idx = chapter.chapter_idx
        await db.commit()
    from app.tasks.generation_tasks import _run_async_generation_impl
    with chapter_write_fence(pipeline_id, command_id, worker_id, chapter_id, version):
        await _run_async_generation_impl(str(task_id), session_factory=sessions)
    async with sessions() as db:
        task = await db.get(GenerationTask, task_id)
        chapter = await db.get(Chapter, chapter_id)
        status = await db.get(PipelineChapterStatus, UUID(str(status_id)))
        if task is None or chapter is None or status is None:
            return ChapterOutcome("failed", error="生成结果或章节已不存在")
        return _outcome(task, chapter, status)
