"""Entity redactor (v0.6).

Replaces people / places / artifacts / sects / techniques with neutral
placeholders, preserving sentence structure and style. Used to produce
the `style_samples_redacted` corpus.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.prompt_registry import run_text_prompt

logger = logging.getLogger(__name__)


async def redact(raw_text: str, db: AsyncSession) -> str:
    """Return redacted text. Returns original text on failure so upstream
    callers can decide whether to skip or fall back."""
    if not raw_text or not raw_text.strip():
        return ""
    try:
        result = await run_text_prompt(
            task_type="redaction",
            user_content=raw_text,
            db=db,
        )
        text = (result.text or "").strip()
        return text or raw_text
    except Exception as exc:
        logger.warning("redaction failed: %s", exc)
        return raw_text
