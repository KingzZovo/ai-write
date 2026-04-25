"""Beat extractor (v0.6).

Extracts entity-redacted plot beats (subject/goal/obstacle/turn) from a
raw reference slice. The prompt itself is responsible for redacting
proper nouns; this wrapper just invokes it and parses JSON.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)


async def extract_beat(raw_text: str, db: AsyncSession) -> dict:
    if not raw_text or not raw_text.strip():
        return {}
    try:
        result = await run_structured_prompt(
            task_type="beat_extraction",
            user_content=raw_text,
            db=db,
        )
        if result.get("parse_error"):
            logger.warning("beat_extraction returned unparseable output")
            return {}
        # v1.4.x fix: tolerate multiple LLM output schemas:
        #   A) {"scene_type": ..., "subject": ..., ...}   (flat dict)
        #   B) {"beats": [{...}, {...}]}                  (wrapper)
        #   C) {"items": [{...}]}                         (post-Layer1 list)
        # v1.4 ingest writes one beat card per slice, so collapse arrays
        # to their first element when we get shape B or C.
        beats_arr = result.get("beats") or result.get("items")
        if isinstance(beats_arr, list) and beats_arr:
            first = beats_arr[0]
            if isinstance(first, dict):
                return first
            logger.warning(
                "beat_extraction array element is not dict: %r",
                type(first).__name__,
            )
            return {}
        return result
    except Exception as exc:
        logger.warning("beat_extraction failed: %s", exc)
        return {}
