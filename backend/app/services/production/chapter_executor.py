"""Delegate a whole-book chapter to the existing asynchronous generation chain.

No second prose prompt or quality loop lives here. The existing chain owns
ContextPack, style selection, drafting, review, repair and entity dispatch.
This adapter owns durable chapter-task identity and fenced persistence.
"""
from __future__ import annotations
import logging

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



logger = logging.getLogger(__name__)


def _should_skip_existing_chapter(chapter, options) -> bool:
    """已有正文且已定稿的章默认不重写(见 execute_chapter 里的理由)。

    抽成纯函数是为了能直接单测:它决定了整条链会不会因为无谓重写而自锁。
    ``options.force_rewrite`` 为真时照旧重写(作者显式要求)。
    """
    return bool(
        chapter is not None
        and chapter.content_text and chapter.content_text.strip()
        and chapter.status == "completed"
        and not (options or {}).get("force_rewrite")
    )


async def _ensure_cognition_for_saved_body(db, project_id, chapter_id) -> None:
    """确保这一章**当前正文**的认知提取已入队。

    跳过重写时正文没变,但它的认知记录可能从未建立(例如正文写于换模型之前、
    或当时提取失败过 —— 那些记录按旧 hash 绑定,对当前正文无效)。这里按当前
    source 查一次:没有记录就派发,由 worker/Beat 补齐。否则下一章的认知边界
    依然会缺这一章。

    派发失败只记日志:跳过这个动作本身不该变成 failed 把链路拖死。
    """
    from app.models.cognition import ChapterCognition
    from app.services.cognition_extraction import enqueue_cognition
    from app.services.cognition_reader import resolve_cognition_source

    try:
        source = await resolve_cognition_source(db, project_id, chapter_id)
        if source is None:
            return
        existing = await db.scalar(
            select(ChapterCognition)
            .where(ChapterCognition.chapter_id == chapter_id,
                   ChapterCognition.source_hash == source.source_hash)
            .limit(1)
            .execution_options(autoflush=False)
        )
        if existing is None:
            await enqueue_cognition(db, source)
            logger.info("Cognition enqueued for already-saved chapter=%s", chapter_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cognition enqueue skipped chapter=%s err=%s", chapter_id, exc)
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
        # 已有正文且已定稿的章:**默认不重写**。
        # 每次重跑都会改写正文 → content_hash 变 → 该章此前按旧 hash 绑定的认知
        # 记录全部作废,下一章的认知边界(要求"它之前所有章"当前正文的认知为
        # completed)随即失效,整卷自锁 —— 实测卷 1 反复在 completed=0~2 打转。
        # 需要真的重写时显式传 options.force_rewrite=true。
        if _should_skip_existing_chapter(chapter, options):
            await _ensure_cognition_for_saved_body(db, pipeline.project_id, chapter.id)
            status.state = "completed"
            status.word_count = chapter.word_count
            status.error_message = None
            status.completed_at = datetime.now(timezone.utc)
            pipeline.current_chapter_idx = chapter.chapter_idx
            await db.commit()
            return ChapterOutcome("completed", chapter.word_count or 0)
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
