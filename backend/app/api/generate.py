"""AI content generation endpoints (SSE streaming)."""

import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
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


class GenerateOutlineRequest(BaseModel):
    project_id: str | None = None
    level: str  # book, volume, chapter
    user_input: str = ""
    parent_outline_id: str | None = None
    volume_idx: int | None = None
    chapter_idx: int | None = None
    style_id: str | None = None


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

    if req.chapter_id:
        chapter = await db.get(Chapter, req.chapter_id)
        if chapter:
            chapter_outline = chapter.outline_json or {}
            current_text = chapter.content_text or ""
            prev_result = await db.execute(
                select(Chapter).where(
                    Chapter.volume_id == chapter.volume_id,
                    Chapter.chapter_idx == chapter.chapter_idx - 1,
                )
            )
            prev_chapter = prev_result.scalar_one_or_none()
            if prev_chapter:
                previous_text = prev_chapter.content_text or ""

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
        try:
            yield f"data: {json.dumps({'status': 'generating', 'message': 'Starting...'})}\n\n"

            generator = ChapterGenerator()
            async for chunk in generator.generate_stream(
                project_settings=project_settings,
                world_rules=world_rules,
                book_outline_summary=book_summary,
                chapter_outline=chapter_outline,
                previous_chapter_text=previous_text,
                current_chapter_text=current_text,
                style_instruction=resolved_style,
                user_instruction=req.user_instruction,
                max_tokens=req.max_tokens,
            ):
                yield f"data: {json.dumps({'text': chunk})}\n\n"

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
                async for chunk in await generator.generate_book_outline(
                    user_input=enhanced_input, stream=True
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
