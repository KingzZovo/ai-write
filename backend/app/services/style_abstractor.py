"""Style abstractor (v0.6).

Given a raw slice, call the `style_abstraction` prompt and return a
structured style-profile JSON. No raw text is retained.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)


async def abstract_style(raw_text: str, db: AsyncSession) -> dict:
    """Return a style-profile JSON dict. Empty dict on failure."""
    if not raw_text or not raw_text.strip():
        return {}
    try:
        result = await run_structured_prompt(
            task_type="style_abstraction",
            user_content=raw_text,
            db=db,
        )
        if result.get("parse_error"):
            logger.warning("style_abstraction returned unparseable output")
            return {}
        return result
    except Exception as exc:
        logger.warning("style_abstraction failed: %s", exc)
        return {}
