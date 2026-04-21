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

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character
from app.services.anti_ai_scanner import scan_anti_ai
from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)


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

    # 3) LLM critic pass
    try:
        user_content = (
            f"<上下文摘要>\n{pack_summary}\n</上下文摘要>\n\n"
            f"<draft>\n{draft}\n</draft>\n\n"
            "请按照输出 schema 给出 issues 列表。"
        )
        out = await run_structured_prompt(
            "critic",
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
        if isinstance(out, dict) and isinstance(out.get("issues"), list):
            for it in out["issues"]:
                if isinstance(it, dict):
                    it.setdefault("source", "llm")
                    issues.append(it)
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
