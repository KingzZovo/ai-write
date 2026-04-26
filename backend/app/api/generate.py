"""AI content generation endpoints (SSE streaming)."""

import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Project, Chapter, Volume, Outline, WorldRule
from app.services.chapter_generator import ChapterGenerator
from app.services.outline_generator import OutlineGenerator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/generate", tags=["generate"])


class GenerateChapterRequest(BaseModel):
    project_id: str
    chapter_id: str | None = None  # If None, generates from outline
    volume_id: str | None = None
    chapter_idx: int | None = None
    style_id: str | None = None  # Specific StyleProfile to use
    style_instruction: str = ""
    user_instruction: str = ""
    max_tokens: int = 4096
    skip_polish: bool = False
    # v1.5.0 C1: opt-in two-stage scene-by-scene writing.
    # When true, the chapter is generated via scene_planner -> per-scene
    # scene_writer streams (each 800-1200 chars), giving more coherent
    # pacing and easier per-scene rewrite hooks downstream (C2).
    use_scene_mode: bool = False
    # Hint for scene_planner; clamped to 3..6 by SceneOrchestrator. None = auto.
    n_scenes_hint: int | None = None
    # Override the chapter target word count for scene-mode planning.
    target_words: int | None = None


class GenerateOutlineRequest(BaseModel):
    project_id: str | None = None
    level: str  # book, volume, chapter
    user_input: str = ""
    parent_outline_id: str | None = None
    volume_idx: int | None = None
    chapter_idx: int | None = None
    style_id: str | None = None
    # v1.4.2 Task B: opt-in structured staged SSE stream for book outline.
    # Mirrors ?staged_stream=1 query param; body field wins when both set.
    staged_stream: bool | None = None


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/chapter")
async def generate_chapter(
    req: GenerateChapterRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Generate chapter content via SSE streaming."""
    # Query all DB data BEFORE creating the generator to avoid
    # using the session after FastAPI's dependency lifecycle closes it.
    project = await db.get(Project, req.project_id)
    if not project:
        return StreamingResponse(
            iter([f"data: {json.dumps({'error': 'Project not found'})}\n\n"]),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    project_settings = project.settings_json or {}

    rules_result = await db.execute(
        select(WorldRule).where(WorldRule.project_id == req.project_id)
    )
    world_rules = [r.rule_text for r in rules_result.scalars().all()]

    book_outline_result = await db.execute(
        select(Outline).where(
            Outline.project_id == req.project_id,
            Outline.level == "book",
        )
    )
    book_outline = book_outline_result.scalar_one_or_none()
    book_summary = book_outline.content_json.get("main_plot", "") if book_outline and book_outline.content_json else ""

    chapter_outline: dict = {}
    previous_text = ""
    current_text = ""
    target_words: int | None = None

    if req.chapter_id:
        chapter = await db.get(Chapter, req.chapter_id)
        if chapter:
            chapter_outline = chapter.outline_json or {}
            current_text = chapter.content_text or ""
            target_words = chapter.target_word_count
            prev_result = await db.execute(
                select(Chapter).where(
                    Chapter.volume_id == chapter.volume_id,
                    Chapter.chapter_idx == chapter.chapter_idx - 1,
                )
            )
            prev_chapter = prev_result.scalar_one_or_none()
            if prev_chapter:
                previous_text = prev_chapter.content_text or ""

    # Fall back to project default for target_words
    if target_words is None and isinstance(project_settings.get("target_chapter_words"), int):
        target_words = int(project_settings["target_chapter_words"])

    # Resolve style: explicit style_id > manual text > auto-resolve
    resolved_style = req.style_instruction
    if not resolved_style and req.style_id:
        try:
            from app.models.project import StyleProfile
            from app.services.style_compiler import compile_style
            profile = await db.get(StyleProfile, req.style_id)
            if profile:
                resolved_style = compile_style(profile)
        except Exception as e:
            logger.warning("Style compile failed: %s", e)
    if not resolved_style:
        try:
            from app.services.style_runtime import resolve_style_prompt
            resolved_style = await resolve_style_prompt(db, req.project_id, req.chapter_id) or ""
        except Exception as e:
            logger.warning("Style resolve failed: %s", e)

    async def event_stream() -> AsyncGenerator[str, None]:
        collected_text: list[str] = []
        try:
            yield f"data: {json.dumps({'status': 'generating', 'message': 'Starting...'})}\n\n"

            effective_user_instruction = req.user_instruction or ""
            if target_words:
                effective_user_instruction = (
                    effective_user_instruction
                    + f"\n\n【本章目标字数】约 {target_words} 字（允许 ±15% 浮动）。"
                )

            # v0.5: ChapterGenerator takes project_id/volume_id/chapter_idx.
            # Resolve them — prefer loaded `chapter` above, fall back to request fields.
            resolved_volume_id: str | None = None
            resolved_chapter_idx: int | None = None
            if req.chapter_id:
                ch = await db.get(Chapter, req.chapter_id)
                if ch:
                    resolved_volume_id = str(ch.volume_id)
                    resolved_chapter_idx = ch.chapter_idx
            if resolved_volume_id is None:
                resolved_volume_id = req.volume_id
            if resolved_chapter_idx is None:
                resolved_chapter_idx = req.chapter_idx

            if not resolved_volume_id or resolved_chapter_idx is None:
                yield f"data: {json.dumps({'error': 'volume_id and chapter_idx required (directly or via chapter_id)'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            # v1.5.0 C1: opt-in scene-staged streaming. SceneOrchestrator
            # plans 3-6 scene briefs (scene_planner) then streams each scene
            # 800-1200 chars (scene_writer). Falls back to ChapterGenerator's
            # single-shot "generation" prompt when use_scene_mode is False.
            stream_iter: AsyncGenerator[str, None]
            if req.use_scene_mode:
                from app.services.scene_orchestrator import SceneOrchestrator

                orchestrator = SceneOrchestrator()
                effective_target_words = req.target_words or target_words

                async def _on_scene_start(scene) -> None:  # type: ignore[no-untyped-def]
                    pass  # placeholder; SSE "scene" events can be added later

                stream_iter = orchestrator.orchestrate_chapter_stream(
                    project_id=req.project_id,
                    volume_id=resolved_volume_id,
                    chapter_idx=resolved_chapter_idx,
                    db=db,
                    chapter_id=req.chapter_id,
                    user_instruction=effective_user_instruction,
                    target_words=effective_target_words,
                    n_scenes_hint=req.n_scenes_hint,
                    on_scene_start=_on_scene_start,
                )
            else:
                generator = ChapterGenerator()
                stream_iter = generator.generate_stream(
                    project_id=req.project_id,
                    volume_id=resolved_volume_id,
                    chapter_idx=resolved_chapter_idx,
                    db=db,
                    chapter_id=req.chapter_id,
                    user_instruction=effective_user_instruction,
                )
            async for chunk in stream_iter:
                if chunk:
                    collected_text.append(chunk)
                yield f"data: {json.dumps({'text': chunk})}\n\n"

            # Auto-save chapter content to DB (Bug K fix: parity with outline auto-save).
            full_text = "".join(collected_text)
            if full_text:
                try:
                    from app.db.session import async_session_factory
                    async with async_session_factory() as save_db:
                        target_chapter = None
                        if req.chapter_id:
                            target_chapter = await save_db.get(Chapter, req.chapter_id)
                        if target_chapter is None and resolved_volume_id and resolved_chapter_idx is not None:
                            lookup = await save_db.execute(
                                select(Chapter).where(
                                    Chapter.volume_id == resolved_volume_id,
                                    Chapter.chapter_idx == resolved_chapter_idx,
                                )
                            )
                            target_chapter = lookup.scalar_one_or_none()
                        if target_chapter is not None:
                            target_chapter.content_text = full_text
                            target_chapter.word_count = len(full_text)
                            target_chapter.status = "completed"
                            await save_db.commit()
                            await save_db.refresh(target_chapter)
                            yield f"data: {json.dumps({'status': 'saved', 'chapter_id': str(target_chapter.id), 'word_count': target_chapter.word_count})}\n\n"
                            # B2' (v1.5.0): kick entity-extraction task post-commit.
                            try:
                                from app.services.entity_dispatch import dispatch_for_chapter
                                await dispatch_for_chapter(
                                    target_chapter, save_db,
                                    caller="api.generate.stream_generate",
                                )
                            except Exception as dispatch_err:
                                logger.warning(
                                    "Entity dispatch after auto-save failed: %s", dispatch_err
                                )
                        else:
                            logger.warning(
                                "Auto-save chapter: no target row (chapter_id=%s vol=%s idx=%s)",
                                req.chapter_id, resolved_volume_id, resolved_chapter_idx,
                            )
                except Exception as save_err:
                    logger.warning("Failed to auto-save chapter: %s", save_err)

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Generation failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/outline")
async def generate_outline(
    req: GenerateOutlineRequest,
    db: AsyncSession = Depends(get_db),
    staged_stream: int = Query(0),
) -> StreamingResponse:
    """Generate outline at specified level via SSE streaming."""
    # Pre-fetch all DB data before creating the generator (same pattern as generate_chapter)
    book_outline: dict = {}
    volume_outline: dict = {}

    if req.level == "volume":
        if req.parent_outline_id:
            parent = await db.get(Outline, req.parent_outline_id)
            book_outline = parent.content_json if parent else {}
        elif req.project_id:
            book_result = await db.execute(
                select(Outline).where(
                    Outline.project_id == req.project_id,
                    Outline.level == "book",
                )
            )
            parent = book_result.scalar_one_or_none()
            book_outline = parent.content_json if parent else {}

    elif req.level == "chapter":
        if req.project_id:
            book_result = await db.execute(
                select(Outline).where(
                    Outline.project_id == req.project_id,
                    Outline.level == "book",
                )
            )
            book_ol = book_result.scalar_one_or_none()
            if book_ol:
                book_outline = book_ol.content_json or {}

        if req.parent_outline_id:
            vol_ol = await db.get(Outline, req.parent_outline_id)
            if vol_ol:
                volume_outline = vol_ol.content_json or {}

    # Resolve style: explicit style_id > auto-resolve
    style_instruction = ""
    if req.style_id:
        try:
            from app.models.project import StyleProfile
            from app.services.style_compiler import compile_style
            profile = await db.get(StyleProfile, req.style_id)
            if profile:
                style_instruction = compile_style(profile)
        except Exception:
            pass
    if not style_instruction and req.project_id:
        try:
            from app.services.style_runtime import resolve_style_prompt
            style_instruction = await resolve_style_prompt(db, req.project_id) or ""
        except Exception:
            pass

    # Build Anti-AI instruction from filter words
    anti_ai_instruction = ""
    try:
        from app.models.project import FilterWord
        fw_result = await db.execute(
            select(FilterWord).where(FilterWord.enabled == 1).limit(50)
        )
        filter_words = [fw.word for fw in fw_result.scalars().all()]
        if filter_words:
            anti_ai_instruction = f"\n\n【禁用词】严禁使用以下词汇：{'、'.join(filter_words)}\n用更自然、更口语化的表达替代。避免四字成语堆砌。句式要多变，长短交替。"
    except Exception:
        pass

    # Combine user input with style and anti-AI instructions
    enhanced_input = req.user_input
    if style_instruction:
        enhanced_input = f"{style_instruction}\n\n---\n\n用户创意：{req.user_input}"
    if anti_ai_instruction:
        enhanced_input += anti_ai_instruction

    async def event_stream() -> AsyncGenerator[str, None]:
        collected_text = []
        try:
            generator = OutlineGenerator()

            yield f"data: {json.dumps({'status': 'generating', 'level': req.level})}\n\n"

            if req.level == "book":
                # v1.4.2 Task B: opt in to structured staged SSE events.
                # Body field wins over the query param when explicitly set.
                want_staged = (
                    req.staged_stream
                    if req.staged_stream is not None
                    else bool(staged_stream)
                )
                if want_staged:
                    stage_iter = await generator.generate_book_outline(
                        user_input=enhanced_input,
                        stream=True,
                        staged=True,
                    )
                    async for event in stage_iter:
                        if not isinstance(event, dict):
                            # Defensive: legacy stream fall-through.
                            collected_text.append(str(event))
                            yield f"data: {json.dumps({'text': str(event)})}\n\n"
                            continue
                        kind = event.get("event")
                        if kind == "stage_chunk":
                            delta = event.get("delta", "")
                            if delta:
                                collected_text.append(delta)
                        elif kind == "done":
                            full = event.get("full_outline", "")
                            if full:
                                # Replace the per-chunk accumulator with the
                                # canonical reassembled text so auto-save keeps
                                # the in-order 9-section document.
                                collected_text = [full]
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                else:
                    async for chunk in await generator.generate_book_outline(
                        user_input=enhanced_input, stream=True, staged=False
                    ):
                        collected_text.append(chunk)
                        yield f"data: {json.dumps({'text': chunk})}\n\n"

            elif req.level == "volume":
                async for chunk in await generator.generate_volume_outline(
                    book_outline=book_outline,
                    volume_idx=req.volume_idx or 1,
                    user_notes=req.user_input,
                    stream=True,
                ):
                    collected_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            elif req.level == "chapter":
                async for chunk in await generator.generate_chapter_outline(
                    book_outline=book_outline,
                    volume_outline=volume_outline,
                    chapter_idx=req.chapter_idx or 1,
                    user_notes=req.user_input,
                    stream=True,
                ):
                    collected_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            # Auto-save outline to DB
            full_text = "".join(collected_text)
            if full_text and req.project_id:
                try:
                    from app.db.session import async_session_factory
                    async with async_session_factory() as save_db:
                        outline = Outline(
                            project_id=req.project_id,
                            level=req.level,
                            parent_id=req.parent_outline_id,
                            content_json={"raw_text": full_text},
                        )
                        save_db.add(outline)
                        await save_db.commit()
                        await save_db.refresh(outline)
                        yield f"data: {json.dumps({'status': 'saved', 'outline_id': str(outline.id)})}\n\n"
                except Exception as save_err:
                    logger.warning("Failed to auto-save outline: %s", save_err)

            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Outline generation failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# =========================================================================
# Async generation (background task mode — for mobile / persistent progress)
# =========================================================================


class AsyncGenerateRequest(BaseModel):
    project_id: str
    task_type: str = "outline_book"  # outline_book, outline_volume, outline_chapter, chapter
    user_input: str = ""
    style_id: str | None = None
    structure_book_id: str | None = None  # Optional: extract & use plot structure from this book
    enable_polish: bool = False  # Optional: second-pass anti-AI polishing
    chapter_id: str | None = None
    volume_idx: int | None = None
    chapter_idx: int | None = None


@router.post("/async")
async def start_async_generation(
    req: AsyncGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Start a background generation task. Returns task_id for polling."""
    from app.models.generation_task import GenerationTask

    task = GenerationTask(
        project_id=req.project_id,
        task_type=req.task_type,
        status="pending",
        params_json={
            "user_input": req.user_input,
            "style_id": req.style_id,
            "structure_book_id": req.structure_book_id,
            "enable_polish": req.enable_polish,
            "chapter_id": req.chapter_id,
            "volume_idx": req.volume_idx,
            "chapter_idx": req.chapter_idx,
        },
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)

    from app.tasks.knowledge_tasks import run_async_generation
    run_async_generation.delay(str(task.id))

    return {"task_id": str(task.id), "status": "pending"}


@router.get("/async/{task_id}")
async def get_async_generation(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Poll generation task progress."""
    from app.models.generation_task import GenerationTask

    task = await db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "task_id": str(task.id),
        "task_type": task.task_type,
        "status": task.status,
        "char_count": task.char_count,
        "progress_text": task.progress_text or "",
        "result_text": task.result_text or "",
        "polished_text": task.polished_text or "",
        "error_message": task.error_message,
        "created_at": str(task.created_at),
    }


@router.get("/async/project/{project_id}")
async def list_project_tasks(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List generation tasks for a project."""
    from app.models.generation_task import GenerationTask

    result = await db.execute(
        select(GenerationTask)
        .where(GenerationTask.project_id == project_id)
        .order_by(GenerationTask.created_at.desc())
        .limit(10)
    )
    return [
        {
            "task_id": str(t.id),
            "task_type": t.task_type,
            "status": t.status,
            "char_count": t.char_count,
            "created_at": str(t.created_at),
        }
        for t in result.scalars().all()
    ]
