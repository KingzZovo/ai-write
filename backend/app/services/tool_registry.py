"""v0.8 — Agent Tool Registry v1.

Five registered tools a drafting agent can call when
``AGENT_TOOL_LOOP_ENABLED=true``:

- search_memory            (qdrant)
- check_character_fact     (sql, characters.profile_json)
- lookup_relation          (sql, relationships)
- suggest_beat             (sql, beat_patterns)
- classify_rule_violation  (python_callable, regex/keyword scan)

Each tool returns a plain ``dict`` suitable to round-trip through an OpenAI
tools protocol. Every call is logged to ``llm_call_logs`` (best effort) so
tool usage is auditable alongside model calls.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character
from app.models.writing_engine import (
    AntiAITrap,
    BeatPattern,
    ToolSpec,
    WritingRule,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def search_memory(
    db: AsyncSession,
    *,
    query: str,
    project_id: str,
    top_k: int = 5,
    **_: Any,
) -> dict[str, Any]:
    """Semantic search across chapter_summaries + compacted collections.

    If Qdrant or embedding backends are unavailable we return an empty list so
    the tool loop can keep going.
    """
    snippets: list[str] = []
    try:
        from app.services.feature_extractor import generate_embedding
        from qdrant_client import AsyncQdrantClient

        from app.config import settings

        emb = await generate_embedding(query or "")
        if not emb:
            return {"snippets": []}
        client = AsyncQdrantClient(
            host=getattr(settings, "QDRANT_HOST", "localhost"),
            port=getattr(settings, "QDRANT_PORT", 6333),
        )
        for collection in ("chapter_summaries", "compacted"):
            try:
                res = await client.search(
                    collection_name=collection,
                    query_vector=emb,
                    limit=top_k,
                    score_threshold=0.35,
                )
            except Exception:
                continue
            for hit in res:
                payload = getattr(hit, "payload", None) or {}
                text = payload.get("summary") or payload.get("text") or ""
                if text:
                    snippets.append(text)
    except Exception as exc:
        logger.debug("search_memory skipped: %s", exc)
    return {"snippets": snippets[: max(1, top_k)]}


async def check_character_fact(
    db: AsyncSession,
    *,
    character_name: str,
    project_id: str,
    **_: Any,
) -> dict[str, Any]:
    """Return location / power_level / relationships for a character."""
    if not character_name:
        return {"location": "", "power_level": "", "relationships": {}}
    rows = await db.execute(
        select(Character).where(
            Character.project_id == project_id,
            Character.name == character_name,
        )
    )
    char = rows.scalars().first()
    if char is None:
        return {"location": "", "power_level": "", "relationships": {}}
    profile = char.profile_json or {}
    rels = profile.get("relationships", {}) or {}
    if isinstance(rels, list):
        # normalise list form -> {target: type}
        norm: dict[str, str] = {}
        for r in rels:
            if isinstance(r, dict):
                target = r.get("target") or r.get("name")
                rtype = r.get("type") or r.get("relation")
                if target and rtype:
                    norm[target] = rtype
        rels = norm
    return {
        "location": profile.get("location", "") or "",
        "power_level": profile.get("power_level", "") or "",
        "relationships": rels if isinstance(rels, dict) else {},
    }


async def lookup_relation(
    db: AsyncSession,
    *,
    character_a: str,
    character_b: str,
    volume_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Return the relationship between two characters (if any).

    We read from the relationships table if it exists; otherwise we fall back
    to ``characters.profile_json.relationships``.
    """
    if not character_a or not character_b:
        return {"rel_type": "", "notes": ""}
    # Try PG relationships table via raw SQL so we don't hard-depend on its ORM.
    try:
        from sqlalchemy import text

        stmt = text(
            "SELECT rel_type, COALESCE(description,'') FROM relationships "
            "WHERE (character_a_name = :a AND character_b_name = :b) "
            "   OR (character_a_name = :b AND character_b_name = :a) "
            "LIMIT 1"
        )
        row = (await db.execute(stmt, {"a": character_a, "b": character_b})).first()
        if row is not None:
            return {"rel_type": row[0] or "", "notes": row[1] or ""}
    except Exception as exc:
        logger.debug("lookup_relation relationships-table path failed: %s", exc)

    # Fallback: profile_json on characters.
    res = await check_character_fact(db, character_name=character_a, project_id="")
    rel = (res.get("relationships") or {}).get(character_b, "")
    return {"rel_type": rel, "notes": ""}


async def suggest_beat(
    db: AsyncSession,
    *,
    chapter_progress: float,
    genre: str = "",
    project_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Return a beat_pattern row matching progress (0..1) and genre.

    Rough mapping:
      progress < 0.2  -> stage='opening'
      progress < 0.6  -> stage='turning'
      progress < 0.85 -> stage='climax'
      else            -> stage='volume_end'
    """
    try:
        p = float(chapter_progress)
    except (TypeError, ValueError):
        p = 0.0
    if p < 0.2:
        stage = "opening"
    elif p < 0.6:
        stage = "turning"
    elif p < 0.85:
        stage = "climax"
    else:
        stage = "volume_end"

    stmt = select(BeatPattern).where(
        BeatPattern.stage == stage,
        BeatPattern.is_active.is_(True),
    )
    if genre:
        stmt = stmt.where((BeatPattern.genre == genre) | (BeatPattern.genre == ""))
    rows = await db.execute(stmt.limit(5))
    candidates = rows.scalars().all()
    if not candidates:
        return {"beat_title": "", "beat_description": ""}
    # Prefer genre-specific first if any.
    candidates.sort(key=lambda b: (0 if b.genre == genre else 1, b.title))
    chosen = candidates[0]
    return {"beat_title": chosen.title, "beat_description": chosen.description or ""}


def _regex_match(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return False


async def classify_rule_violation(
    db: AsyncSession,
    *,
    text: str,
    genre: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Return {violated_rules, anti_ai_hits} for the given chunk of text.

    ``violated_rules`` is best-effort: we flag an active rule when the rule
    title or a negative example substring appears in the text; the LLM layer
    can refine this later.
    """
    if not text:
        return {"violated_rules": [], "anti_ai_hits": []}

    violated: list[str] = []
    stmt = select(WritingRule).where(WritingRule.is_active.is_(True))
    if genre:
        stmt = stmt.where((WritingRule.genre == genre) | (WritingRule.genre == ""))
    for rule in (await db.execute(stmt)).scalars().all():
        triggered = False
        # title heuristic
        if rule.title and rule.title in text:
            triggered = True
        # example `bad` substrings are strong signals
        if not triggered:
            for ex in (rule.examples_json or []):
                if isinstance(ex, dict) and isinstance(ex.get("bad"), str):
                    if ex["bad"] and ex["bad"] in text:
                        triggered = True
                        break
        if triggered:
            violated.append(rule.title)

    anti_ai_hits: list[str] = []
    traps = (
        await db.execute(
            select(AntiAITrap).where(AntiAITrap.is_active.is_(True))
        )
    ).scalars().all()
    for trap in traps:
        hit = False
        if trap.pattern_type == "keyword":
            hit = trap.pattern in text
        elif trap.pattern_type == "regex":
            hit = _regex_match(trap.pattern, text)
        elif trap.pattern_type == "ngram":
            hit = any(p.strip() and p.strip() in text for p in trap.pattern.split("|"))
        if hit:
            anti_ai_hits.append(f"[{trap.pattern_type}] {trap.pattern}")

    return {"violated_rules": violated, "anti_ai_hits": anti_ai_hits}


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------

ToolFn = Callable[..., Awaitable[dict[str, Any]]]

_BUILTIN_TOOLS: dict[str, ToolFn] = {
    "search_memory": search_memory,
    "check_character_fact": check_character_fact,
    "lookup_relation": lookup_relation,
    "suggest_beat": suggest_beat,
    "classify_rule_violation": classify_rule_violation,
}


async def list_active_tools(db: AsyncSession) -> list[ToolSpec]:
    rows = await db.execute(
        select(ToolSpec).where(ToolSpec.is_active.is_(True)).order_by(ToolSpec.name)
    )
    return list(rows.scalars().all())


async def build_openai_tools(db: AsyncSession) -> list[dict[str, Any]]:
    """Return tool specs shaped for OpenAI chat.completions ``tools`` param."""
    out: list[dict[str, Any]] = []
    for spec in await list_active_tools(db):
        if spec.name not in _BUILTIN_TOOLS:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description or spec.name,
                    "parameters": spec.input_schema_json or {"type": "object"},
                },
            }
        )
    return out


async def run_tool(
    name: str,
    args: dict[str, Any],
    db: AsyncSession,
    *,
    project_id: str | None = None,
    chapter_id: str | None = None,
) -> dict[str, Any]:
    """Dispatch a tool call and log it to ``llm_call_logs``."""
    fn = _BUILTIN_TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    args = dict(args or {})
    if project_id and "project_id" not in args:
        args["project_id"] = project_id
    t0 = time.monotonic()
    try:
        result = await fn(db, **args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool %s raised", name)
        result = {"error": str(exc)[:500]}
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    try:
        from app.services.llm_call_logger import log_llm_call

        await log_llm_call(
            db,
            task_type=f"tool:{name}",
            provider="agent_tool",
            model="builtin",
            prompt=json.dumps(args, ensure_ascii=False)[:4000],
            response=json.dumps(result, ensure_ascii=False)[:4000],
            project_id=project_id,
            chapter_id=chapter_id,
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        logger.debug("tool log skipped: %s", exc)
    return result
