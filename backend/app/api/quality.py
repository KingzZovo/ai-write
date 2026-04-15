"""Quality check and writing guidance API endpoints."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, Project, Foreshadow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["quality"])


# =============================================================================
# Checker Dashboard
# =============================================================================

class CheckQualityRequest(BaseModel):
    chapter_text: str | None = None  # If None, loads from DB


@router.post("/chapters/{chapter_id}/check-quality")
async def check_quality(
    chapter_id: str,
    body: CheckQualityRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Run all 6 quality checkers on a chapter."""
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    text = (body.chapter_text if body and body.chapter_text else None) or chapter.content_text or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="Chapter has no content to check")

    from app.services.checkers.checker_manager import CheckerManager
    mgr = CheckerManager()

    # Try to build context pack if possible
    context = None
    try:
        from app.services.context_pack import ContextPackBuilder
        builder = ContextPackBuilder()
        volume_id = str(chapter.volume_id) if chapter.volume_id else ""
        context = await builder.build(
            project_id="",
            volume_id=volume_id,
            chapter_idx=chapter.chapter_idx or 1,
            db=db,
        )
    except Exception as e:
        logger.warning("Could not build context pack for checking: %s", e)

    result = await mgr.run_all(text, context)

    return {
        "overall_score": result.overall_score,
        "passed": result.passed,
        "total_issues": result.total_issues,
        "checkers": [
            {
                "name": r.checker_name,
                "score": r.score,
                "passed": r.passed,
                "issues": r.issues,
            }
            for r in result.checker_results
        ],
    }


# =============================================================================
# Strand Tracker
# =============================================================================

@router.get("/projects/{project_id}/strand-status")
async def get_strand_status(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get current strand balance status for a project."""
    from app.services.strand_tracker import StrandTrackerService

    tracker = StrandTrackerService()
    try:
        strand_data = await tracker.analyze_strands(project_id, db)
        history = await tracker.get_strand_history(project_id, db)
        recommendations = tracker.get_balance_recommendations(strand_data)

        return {
            "tracker": {
                "last_quest_chapter": strand_data.last_quest_chapter,
                "last_fire_chapter": strand_data.last_fire_chapter,
                "last_constellation_chapter": strand_data.last_constellation_chapter,
                "current_dominant": strand_data.current_dominant,
            },
            "warnings": strand_data.get_warnings(
                max(strand_data.last_quest_chapter,
                    strand_data.last_fire_chapter,
                    strand_data.last_constellation_chapter) + 1
            ),
            "history": history[-20:],  # Last 20 chapters
            "recommendations": recommendations,
        }
    except Exception as e:
        logger.warning("Strand analysis failed: %s", e)
        return {
            "tracker": {
                "last_quest_chapter": 0,
                "last_fire_chapter": 0,
                "last_constellation_chapter": 0,
                "current_dominant": "quest",
            },
            "warnings": [],
            "history": [],
            "recommendations": [],
        }


# =============================================================================
# Writing Guides
# =============================================================================

@router.get("/writing-guides")
async def get_writing_guides() -> dict:
    """Get all available writing modules, prohibitions, and genre templates."""
    from app.services.writing_guides import (
        WRITING_MODULES, WRITING_PROHIBITIONS, HOOK_TECHNIQUES,
        GENRE_TEMPLATES, AI_WORD_BLACKLIST,
    )

    return {
        "modules": {
            key: {
                "name": mod.get("name", key),
                "description": mod.get("description", ""),
                "rules": mod.get("rules", []),
            }
            for key, mod in WRITING_MODULES.items()
        },
        "prohibitions": WRITING_PROHIBITIONS,
        "hook_techniques": [
            {"key": key, "name": tech.get("name", key), "description": tech.get("description", "")}
            for key, tech in HOOK_TECHNIQUES.items()
        ],
        "genres": [
            {"key": key, "name": tmpl.get("name", key)}
            for key, tmpl in GENRE_TEMPLATES.items()
        ],
        "ai_word_count": len(AI_WORD_BLACKLIST),
    }


class BuildPromptRequest(BaseModel):
    active_modules: list[str] = []
    genre: str = ""
    chapter_position: str = "middle"


@router.post("/writing-guides/build-prompt")
async def build_prompt(body: BuildPromptRequest) -> dict:
    """Build a writing prompt from selected modules and genre."""
    from app.services.writing_guides import build_writing_prompt

    prompt = build_writing_prompt(
        active_modules=body.active_modules,
        genre=body.genre,
        chapter_position=body.chapter_position,
    )
    return {"prompt": prompt, "length": len(prompt)}


# =============================================================================
# Anti-AI Check
# =============================================================================

@router.post("/chapters/{chapter_id}/check-anti-ai")
async def check_anti_ai(
    chapter_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Run Anti-AI checker specifically on a chapter."""
    chapter = await db.get(Chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    text = chapter.content_text or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="No content")

    from app.services.checkers.anti_ai_checker import AntiAIChecker
    checker = AntiAIChecker()
    result = await checker.check(text, None)

    return {
        "score": result.score,
        "passed": result.passed,
        "issues": result.issues,
        "checker_name": result.checker_name,
    }
