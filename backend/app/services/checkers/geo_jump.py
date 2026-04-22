"""ConStory v1 — geo jump checker (v1.0 chunk 10).

Checks whether a character appears at a location in the current chapter whose
last known location (Neo4j :AT_LOCATION) is different and no transit
was narrated. Best-effort + returns [] on any Neo4j failure.

Output shape matches critic_service issues:
    {severity, category, desc, location, source}
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 转场/赶路描写关键词：若当前章节出现，则视为有转场交代
TRANSIT_KEYWORDS = (
    "赶往", "前往", "来到", "跋涉", "赶路", "驱车", "乘飞", "飞奔",
    "奔赴", "移动", "转移", "离开", "出发", "越过", "穿越", "入城",
    "出城", "赶回", "回到",
)


async def scan_geo_jump(
    draft: str,
    *,
    project_id: str,
    chapter_idx: int | None = None,
    neo4j_driver: Any | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not draft or not draft.strip():
        return issues
    if neo4j_driver is None or chapter_idx is None:
        return issues

    # 查询每个角色在 <= chapter_idx 的最近一条 AT_LOCATION
    try:
        query = (
            "MATCH (c:Character {project_id: $pid})-[r:AT_LOCATION]->(l:Location) "
            "WHERE r.chapter_start <= $cidx "
            "  AND (r.chapter_end IS NULL OR r.chapter_end >= $cidx) "
            "RETURN c.name AS name, l.name AS loc, r.chapter_start AS cs "
            "ORDER BY r.chapter_start DESC "
            "LIMIT 100"
        )
        async with neo4j_driver.session() as session:
            result = await session.run(query, pid=str(project_id), cidx=int(chapter_idx))
            records = [r async for r in result]
    except Exception as exc:
        logger.debug("geo_jump neo4j query failed: %s", exc)
        return issues

    # 去重：每角色取最近一条
    latest_by_char: dict[str, str] = {}
    for rec in records:
        name = rec.get("name") or ""
        loc = rec.get("loc") or ""
        if name and name not in latest_by_char and loc:
            latest_by_char[name] = loc

    has_transit = any(kw in draft for kw in TRANSIT_KEYWORDS)

    for name, known_loc in latest_by_char.items():
        if name not in draft:
            continue
        # 若已知地点仍在 draft 中出现，视为连贯
        if known_loc and known_loc in draft:
            continue
        # name 在 draft 里但已知地点不在，且未提及 transit 关键词
        if not has_transit:
            issues.append(
                {
                    "severity": "soft",
                    "category": "consistency_geo_jump",
                    "desc": f"角色 {name} 上一知所在「{known_loc}」，本章未提及该地点且无转场/赶路描写，怀疑空间跳接。",
                    "location": known_loc,
                    "source": "consistency:geo_jump",
                }
            )
    return issues
