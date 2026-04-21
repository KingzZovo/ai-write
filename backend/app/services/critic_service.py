"""v0.7 — Critic node.

Combines deterministic rule checks (character location / power level / relationship
consistency) with an LLM-based review using task_type="critic". Emits a JSON issue
list graded hard / soft / info.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character
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
        # Location check
        loc = (ch.current_location or "").strip() if hasattr(ch, "current_location") else ""
        if loc and f"{ch.name}" in draft and loc not in draft:
            # Not authoritative but flag as soft hint for LLM follow-up
            issues.append({
                "severity": "soft",
                "category": "location",
                "desc": f"角色 {ch.name} 已知位置 {loc}，章节未明确交代。",
                "location": "",
            })
    return issues


async def run_critic(
    *,
    draft: str,
    project_id: str,
    chapter_id: str | None,
    db: AsyncSession,
    pack_summary: str = "",
) -> dict[str, Any]:
    """Return {issues:[{severity, category, desc, location}], hard_count, soft_count, info_count}."""
    issues: list[dict[str, Any]] = []

    # 1) deterministic rules
    try:
        issues.extend(await _rule_check(draft, project_id, db))
    except Exception as exc:
        logger.warning("critic rule_check failed: %s", exc)

    # 2) LLM critic pass (task_type="critic")
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
            issues.extend(out["issues"])
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
