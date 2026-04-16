"""StyleRuntime: Resolves active style rules for a given generation context.

Priority resolution order:
  1. Chapter-level binding (highest priority)
  2. Book/project-level binding
  3. Global active profiles (lowest priority)

Compiles the resolved profile(s) into prompt instructions.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import StyleProfile
from app.services.style_compiler import compile_style, compile_anti_ai_instructions

logger = logging.getLogger(__name__)


async def resolve_style_prompt(
    db: AsyncSession,
    project_id: str | UUID,
    chapter_id: str | UUID | None = None,
) -> str:
    """Resolve and compile the active style profile for a generation context.

    Returns a compiled prompt string ready for injection into the system prompt.
    """
    profile = await resolve_active_profile(db, project_id, chapter_id)
    if profile is None:
        return ""
    return compile_style(profile)


async def resolve_anti_ai_prompt(
    db: AsyncSession,
    project_id: str | UUID,
    chapter_id: str | UUID | None = None,
) -> str:
    """Resolve only the Anti-AI instructions for a generation context."""
    profile = await resolve_active_profile(db, project_id, chapter_id)
    if profile is None:
        return ""
    return compile_anti_ai_instructions(profile)


async def resolve_active_profile(
    db: AsyncSession,
    project_id: str | UUID,
    chapter_id: str | UUID | None = None,
) -> StyleProfile | None:
    """Find the highest-priority active profile for the given context.

    Resolution order:
    1. Chapter-bound profile (bind_level='chapter', bind_target_id=chapter_id)
    2. Project-bound profile (bind_level='book', bind_target_id=project_id)
    3. Global active profile (bind_level='global')
    """
    # 1. Chapter-level
    if chapter_id:
        result = await db.execute(
            select(StyleProfile).where(
                StyleProfile.is_active == 1,
                StyleProfile.bind_level == "chapter",
                StyleProfile.bind_target_id == str(chapter_id),
            ).limit(1)
        )
        profile = result.scalar_one_or_none()
        if profile:
            return profile

    # 2. Project-level
    result = await db.execute(
        select(StyleProfile).where(
            StyleProfile.is_active == 1,
            StyleProfile.bind_level == "book",
            StyleProfile.bind_target_id == str(project_id),
        ).limit(1)
    )
    profile = result.scalar_one_or_none()
    if profile:
        return profile

    # 3. Global
    result = await db.execute(
        select(StyleProfile).where(
            StyleProfile.is_active == 1,
            StyleProfile.bind_level == "global",
        ).order_by(StyleProfile.updated_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()
