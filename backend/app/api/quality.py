"""Quality check and writing guidance API endpoints."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter
from app.services.review_contract import draft_digest

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

    text = body.chapter_text if body and body.chapter_text is not None else chapter.content_text or ''
    if not text.strip():
        raise HTTPException(status_code=400, detail="Chapter has no content to check")

    from app.services.checkers.checker_manager import CheckerManager
    mgr = CheckerManager()

    # A review without required source material is not a complete review.
    context = None
    builder = None
    try:
        from app.services.context_pack import ContextPackBuilder
        from app.models.project import Volume
        builder = ContextPackBuilder()
        volume_id = str(chapter.volume_id) if chapter.volume_id else ""
        # Get project_id from volume
        project_id = ""
        if volume_id:
            volume = await db.get(Volume, volume_id)
            if volume:
                project_id = str(volume.project_id)
        context = await builder.build(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter.chapter_idx or 1,
            db=db,
        )
    except Exception as e:
        logger.warning("Could not build context pack for checking (%s)", type(e).__name__)
        from app.services.generation_errors import review_context_message
        await db.rollback()
        raise HTTPException(status_code=503, detail=review_context_message(e)) from e
    finally:
        # v1.10: release any session ``ContextPackBuilder`` self-created
        # so the underlying connection does not stay ``idle in transaction``.
        if builder is not None:
            try:
                await builder.close()
            except Exception:  # noqa: BLE001
                pass

    await db.rollback()
    try:
        await context.resolve_cognition(db=db, user_instruction='本次待评审正文：\n' + text)
    except Exception as exc:
        from app.services.generation_errors import review_context_message
        raise HTTPException(status_code=503, detail=review_context_message(exc)) from exc
    result = await mgr.run_all(text, context)

    # v1.9.1: surface the cross-project Chinese prose-mechanics report so the
    # manual checker dashboard can audit the same rules the generation gate uses.
    from app.services.chinese_prose_mechanics_checker import analyze_chinese_prose_mechanics
    prose_report = analyze_chinese_prose_mechanics(text).to_safe_dict()

    return {
        **result.to_dict(),
        'chapter_id': chapter_id,
        'draft_sha256': draft_digest(text),
        "chinese_prose_mechanics": prose_report,
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

    # 2026-11-17: 这里原本无参构造 → 服务自建会话且从不 aclose,每次请求泄漏一条
    # idle-in-transaction 连接;调用方已经有 get_db 的会话,直接复用它。
    tracker_svc = StrandTrackerService(db)
    try:
        # Get latest chapter index for this project
        from app.models.project import Volume, Chapter as ChapterModel
        vol_result = await db.execute(
            select(Volume.id).where(Volume.project_id == project_id)
        )
        vol_ids = [str(v) for v in vol_result.scalars().all()]
        current_chapter = 1
        if vol_ids:
            # Strand data lives on the book-global axis (Chapter.global_idx);
            # max of the volume-LOCAL chapter_idx underestimates the current
            # position from volume 2 onward.
            ch_result = await db.execute(
                select(ChapterModel.global_idx)
                .where(
                    ChapterModel.volume_id.in_(vol_ids),
                    ChapterModel.global_idx.isnot(None),
                )
                .order_by(ChapterModel.global_idx.desc())
                .limit(1)
            )
            latest = ch_result.scalar_one_or_none()
            if not latest:
                # Pre-backfill rows (NULL global_idx): fall back to the old
                # local-idx max (exact for single-volume projects).
                ch_result = await db.execute(
                    select(ChapterModel.chapter_idx)
                    .where(ChapterModel.volume_id.in_(vol_ids))
                    .order_by(ChapterModel.chapter_idx.desc())
                    .limit(1)
                )
                latest = ch_result.scalar_one_or_none()
            if latest:
                current_chapter = latest

        strand_data = await tracker_svc.analyze_strands(project_id, current_chapter)
        history = await tracker_svc.get_strand_history(project_id)
        recommendations = tracker_svc.get_balance_recommendations(strand_data, current_chapter)

        return {
            "tracker": {
                "last_quest_chapter": strand_data.last_quest_chapter,
                "last_fire_chapter": strand_data.last_fire_chapter,
                "last_constellation_chapter": strand_data.last_constellation_chapter,
                "current_dominant": strand_data.current_dominant,
            },
            "warnings": strand_data.get_warnings(current_chapter),
            "history": [{"chapter": h.chapter_idx, "dominant": h.dominant} for h in history[-20:]],
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


# =========================================================================
# Genre Rules API
# =========================================================================


@router.get("/genre-rules")
async def list_genre_rules() -> dict:
    """List all available genre templates."""
    from app.services.genre_rules import get_all_genres
    return {"genres": get_all_genres()}


@router.get("/genre-rules/{genre}")
async def get_genre_rule(genre: str) -> dict:
    """Get a specific genre template with compiled prompt."""
    from app.services.genre_rules import get_genre_template, compile_genre_prompt
    template = get_genre_template(genre)
    if not template:
        raise HTTPException(status_code=404, detail=f"题材 '{genre}' 不存在")
    return {
        "name": template.name,
        "label": template.label,
        "rules": template.rules,
        "pacing": template.pacing,
        "taboos": template.taboos,
        "hooks": template.hooks,
        "keywords": template.keywords,
        "compiled_prompt": compile_genre_prompt(genre),
    }


# =========================================================================
# BVSR (Multi-variant Generation)
# =========================================================================

class BVSRRequest(BaseModel):
    prompt: str
    system_prompt: str = ""
    count: int = 3
    auto_score: bool = True


@router.post("/bvsr/generate", deprecated=True)
async def bvsr_generate(body: BVSRRequest) -> dict:
    """Retire the unimplemented standalone stub; BVSR belongs to chapter runs.

    This route never had generation/scoring implementations and cannot supply
    the chapter sources required for a review. Existing chapter-run BVSR and
    variant inspection/author selection remain the supported workflow.
    """
    raise HTTPException(status_code=410, detail={
        'code': 'chapter_run_required',
        'message': '独立多候选接口已停用。请在指定作品和章节的生成流程中使用多候选，并查看保留的各稿。',
        'generation_endpoint': '/api/generation-runs/start',
    })


# =========================================================================
# ConStory Cross-chapter Consistency Check
# =========================================================================

@router.post("/constory-check/{project_id}")
async def constory_check(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Run cross-chapter consistency check on a project."""
    from app.models.project import Volume
    from app.services.constory_checker import check_cross_chapter_consistency

    vol_result = await db.execute(
        select(Volume.id).where(Volume.project_id == str(project_id))
    )
    vol_ids = [str(v) for v in vol_result.scalars().all()]

    ch_result = await db.execute(
        select(Chapter)
        .where(Chapter.volume_id.in_(vol_ids) if vol_ids else Chapter.id.is_(None))
        .order_by(Chapter.chapter_idx)
    )
    chapters = list(ch_result.scalars().all())

    if len(chapters) < 2:
        return {"message": "至少需要2个章节才能进行一致性检查", "report": None}

    chapter_texts = [(ch.chapter_idx, ch.title, ch.content_text or "") for ch in chapters]
    report = await check_cross_chapter_consistency(chapter_texts)
    return {"report": report.to_dict()}


# =========================================================================
# Foreshadow Setup (planned foreshadow vs. this chapter's prose)
# =========================================================================

@router.get("/quality/projects/{project_id}/chapters/{chapter_id}/foreshadow-setup")
async def get_chapter_foreshadow_setup(
    project_id: str,
    chapter_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Report which outline-planned foreshadows this chapter's prose never sets up.

    Read-only: no model call and no write. A chapter that does not exist in this
    project is a 404; a chapter without plans is a 200 with planned = 0.
    """
    from app.services.foreshadow_setup_check import (
        ForeshadowSetupChapterUnavailable,
        check_chapter_foreshadow_setup,
    )

    try:
        report = await check_chapter_foreshadow_setup(
            db, project_id=project_id, chapter_id=chapter_id,
        )
    except ForeshadowSetupChapterUnavailable as exc:
        raise HTTPException(status_code=404, detail="Chapter not found") from exc

    return {
        "chapter_id": report.chapter_id,
        "planned": report.planned,
        "grounded": report.grounded,
        "missing": [
            {"plan_key": item.plan_key, "title": item.title, "keywords": list(item.keywords)}
            for item in report.missing
        ],
        "items": [
            {
                "plan_key": item.plan_key,
                "title": item.title,
                "keywords": list(item.keywords),
                "grounded": item.grounded,
                "evidence": item.evidence,
            }
            for item in report.items
        ],
    }
