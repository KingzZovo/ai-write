"""v0.8 — 3-layer Critic node.

Layers (in order):
1. Deterministic rule check  — character state vs draft (v0.7 behaviour).
2. Anti-AI scan              — loads active ``anti_ai_traps`` and reports hits.
3. LLM critic pass           — task_type="critic", aggregates the above into
                                 a structured issue list.

The combined output keeps the v0.7 shape so ``generation_runner`` and the API
don't need to change: ``{issues, hard_count, soft_count, info_count}``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character
from app.services.anti_ai_scanner import scan_anti_ai
from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)


def _critic_split_enabled() -> bool:
    """v1.4 — env toggle for the critic_hard + critic_soft split (default on)."""
    raw = os.getenv("CRITIC_SPLIT_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _critic_consistency_llm_enabled() -> bool:
    """v1.4 — env toggle for consistency_llm_check deep-verdict pass (default off)."""
    raw = os.getenv("CRITIC_CONSISTENCY_LLM_ENABLED", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


_CONSISTENCY_CATEGORIES = {
    "consistency",
    "time_reversal",
    "geo_jump",
    "item_missing",
    "location",
}


def _has_consistency_hit(issues: list[dict[str, Any]]) -> bool:
    """v1.4 — return True if any LLM-critic issue looks consistency-related."""
    for it in issues:
        if not isinstance(it, dict):
            continue
        cat = str(it.get("category", "")).lower()
        if cat in _CONSISTENCY_CATEGORIES:
            return True
        tags = it.get("tags") or []
        if isinstance(tags, list) and any(
            isinstance(t, str) and t.lower() in _CONSISTENCY_CATEGORIES for t in tags
        ):
            return True
    return False


async def _run_consistency_llm_check(
    user_content: str,
    db: AsyncSession,
    *,
    project_id: str,
    chapter_id: str | None,
) -> list[dict[str, Any]]:
    """v1.4 — deep consistency verdict pass, task_type=consistency_llm_check.

    Appends ``source="llm"`` and ``critic_stream="consistency_llm_check"`` to each
    returned issue so downstream counts and UIs can tell the streams apart.
    """
    try:
        out = await run_structured_prompt(
            "consistency_llm_check",
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
    except Exception as exc:
        logger.warning("critic consistency_llm_check failed: %s", exc)
        return []
    items: list[dict[str, Any]] = []
    if isinstance(out, dict) and isinstance(out.get("issues"), list):
        for it in out["issues"]:
            if isinstance(it, dict):
                it.setdefault("source", "llm")
                it.setdefault("critic_stream", "consistency_llm_check")
                items.append(it)
    return items


async def _run_llm_critic(
    user_content: str,
    db: AsyncSession,
    *,
    project_id: str,
    chapter_id: str | None,
) -> list[dict[str, Any]]:
    """v1.4 — call the LLM critic layer, splitting critic_hard + critic_soft when enabled.

    Returns a flat list of issue dicts (already stamped with ``source="llm"``).
    Failures are logged and an empty list is returned; callers decide how to aggregate.
    """

    async def _single(task_type: str) -> list[dict[str, Any]]:
        out = await run_structured_prompt(
            task_type,
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
        items: list[dict[str, Any]] = []
        if isinstance(out, dict) and isinstance(out.get("issues"), list):
            for it in out["issues"]:
                if isinstance(it, dict):
                    it.setdefault("source", "llm")
                    it.setdefault("critic_stream", task_type)
                    items.append(it)
        return items

    if _critic_split_enabled():
        results = await asyncio.gather(
            _single("critic_hard"),
            _single("critic_soft"),
            return_exceptions=True,
        )
        merged: list[dict[str, Any]] = []
        all_failed = True
        for r in results:
            if isinstance(r, Exception):
                logger.warning("critic split stream failed: %s", r)
                continue
            all_failed = False
            merged.extend(r)
        if not all_failed:
            return merged
        logger.warning("critic split: both streams failed, falling back to single critic")

    # Fallback: single critic (legacy path).
    try:
        return await _single("critic")
    except Exception as exc:
        logger.warning("critic LLM pass failed: %s", exc)
        return []


async def _rule_check(draft: str, project_id: str, db: AsyncSession) -> list[dict[str, Any]]:
    """Rule-based consistency: characters referenced in draft must not contradict known facts."""
    issues: list[dict[str, Any]] = []
    rows = await db.execute(select(Character).where(Character.project_id == project_id))
    chars = rows.scalars().all()
    for ch in chars:
        if not ch.name or ch.name not in draft:
            continue
        profile = ch.profile_json or {}
        loc = (profile.get("location") or "").strip() if isinstance(profile, dict) else ""
        if loc and loc not in draft:
            issues.append(
                {
                    "severity": "soft",
                    "category": "location",
                    "desc": f"角色 {ch.name} 已知位置 {loc}，章节未明确交代。",
                    "location": "",
                    "source": "rule",
                }
            )
    return issues


async def run_critic(
    *,
    draft: str,
    project_id: str,
    chapter_id: str | None,
    db: AsyncSession,
    pack_summary: str = "",
    chapter_idx: int | None = None,
    item_names: list[str] | None = None,
) -> dict[str, Any]:
    """Return aggregated critic output across the 3 layers."""
    issues: list[dict[str, Any]] = []

    # 1) deterministic rules
    try:
        issues.extend(await _rule_check(draft, project_id, db))
    except Exception as exc:
        logger.warning("critic rule_check failed: %s", exc)

    # 2) v0.8 anti-AI scan
    try:
        issues.extend(await scan_anti_ai(draft, db))
    except Exception as exc:
        logger.warning("critic anti_ai scan failed: %s", exc)

    # 2.5) v1.0 ConStory v1 consistency checks (best-effort, Neo4j-backed)
    try:
        from app.db.neo4j import get_neo4j
        from app.services.checkers.time_reversal import scan_time_reversal
        from app.services.checkers.geo_jump import scan_geo_jump
        from app.services.checkers.item_missing import scan_item_missing
        _driver = None
        try:
            async for d in get_neo4j():
                _driver = d
                break
        except Exception as exc:
            logger.debug("critic: neo4j unavailable: %s", exc)
        try:
            issues.extend(
                await scan_time_reversal(
                    draft,
                    project_id=project_id,
                    chapter_idx=chapter_idx,
                    neo4j_driver=_driver,
                    db=db,
                )
            )
        except Exception as exc:
            logger.warning("critic time_reversal failed: %s", exc)
        try:
            issues.extend(
                await scan_geo_jump(
                    draft,
                    project_id=project_id,
                    chapter_idx=chapter_idx,
                    neo4j_driver=_driver,
                )
            )
        except Exception as exc:
            logger.warning("critic geo_jump failed: %s", exc)
        try:
            issues.extend(
                await scan_item_missing(
                    draft,
                    project_id=project_id,
                    chapter_idx=chapter_idx,
                    item_names=item_names,
                )
            )
        except Exception as exc:
            logger.warning("critic item_missing failed: %s", exc)
    except Exception as exc:
        logger.warning("critic consistency suite failed: %s", exc)

    # 3) LLM critic pass
    try:
        user_content = (
            f"<上下文摘要>\n{pack_summary}\n</上下文摘要>\n\n"
            f"<draft>\n{draft}\n</draft>\n\n"
            "请按照输出 schema 给出 issues 列表。"
        )
        llm_issues = await _run_llm_critic(
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
        issues.extend(llm_issues)
        # v1.4 — optional deep consistency verdict when critic_hard flagged something
        if _critic_consistency_llm_enabled():
            hard_stream = [
                it
                for it in llm_issues
                if it.get("critic_stream") == "critic_hard"
                or it.get("severity") == "hard"
            ]
            if _has_consistency_hit(hard_stream):
                issues.extend(
                    await _run_consistency_llm_check(
                        user_content,
                        db,
                        project_id=project_id,
                        chapter_id=chapter_id,
                    )
                )
    except Exception as exc:
        logger.warning("critic LLM pass failed: %s", exc)

    hard = sum(1 for i in issues if i.get("severity") == "hard")
    soft = sum(1 for i in issues if i.get("severity") == "soft")
    info = sum(1 for i in issues if i.get("severity") == "info")
    return {
        "issues": issues,
        "hard_count": hard,
        "soft_count": soft,
        "info_count": info,
    }
