"""ConStory v1 — time reversal checker (v1.0 chunk 10).

Detects text references to a character being resurrected / healed / 复活 /
复原 者后续章节，与 Neo4j 中已知的死亡/重伤状态不一致时投出问题。

Output shape matches critic_service issues:
    {severity, category, desc, location, source}
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 触发时间倒退的关键词（当前章节提及某角色‌活着 / 复活 / 复原）
ALIVE_KEYWORDS = ("复活", "重生", "活过来", "被救活", "恢复过来", "恢复如初", "清醒")
DEAD_KEYWORDS = ("死亡", "已死", "身亡", "陕落", "被杀", "碎成粉末")


async def scan_time_reversal(
    draft: str,
    *,
    project_id: str,
    chapter_idx: int | None = None,
    neo4j_driver: Any | None = None,
    db: Any | None = None,
) -> list[dict[str, Any]]:
    """Return list of consistency issues in critic_service format.

    Best-effort: returns [] on any Neo4j/driver failure.
    """
    issues: list[dict[str, Any]] = []
    if not draft or not draft.strip():
        return issues

    # 1) 文本预筛：是否出现 ‘复活’ 类关键词
    alive_hits = [kw for kw in ALIVE_KEYWORDS if kw in draft]
    if not alive_hits:
        return issues

    if chapter_idx is None:
        return issues

    # Prefer Postgres projection if available.
    if db is not None:
        try:
            from sqlalchemy import text

            rows = await db.execute(
                text(
                    """
                    SELECT ch.name
                    FROM character_states cs
                    JOIN characters ch
                      ON ch.id = cs.character_id
                    WHERE cs.project_id = :pid
                      AND cs.chapter_start <= :cidx
                      AND (cs.chapter_end IS NULL OR cs.chapter_end < :cidx)
                      AND cs.status_json::text LIKE '%死亡%'
                    LIMIT 50
                    """
                ),
                {"pid": str(project_id), "cidx": int(chapter_idx)},
            )
            names = [r[0] for r in rows.all() if r and r[0]]
            for name in names:
                if name not in draft:
                    continue
                for kw in alive_hits:
                    idx_n = draft.find(name)
                    idx_k = draft.find(kw)
                    if idx_n < 0 or idx_k < 0:
                        continue
                    if abs(idx_n - idx_k) <= 30:
                        issues.append(
                            {
                                "severity": "hard",
                                "category": "consistency_time_reversal",
                                "desc": f"角色 {name} 在早前章节已标记死亡/陨落，本章出现「{kw}」的描述，怀疑时间线倒退。",
                                "location": name,
                                "source": "consistency:time_reversal:pg",
                            }
                        )
                        break
            return issues
        except Exception as exc:
            logger.debug("time_reversal pg query failed: %s", exc)

    if neo4j_driver is None:
        return issues

    # 2) Neo4j 查询：在当前章之前已经被标记 status.存亡=死亡 的角色
    try:
        query = (
            "MATCH (c:Character {project_id: $pid})-[:HAS_STATE]->(s:CharacterState) "
            "WHERE s.chapter_start <= $cidx "
            "  AND (coalesce(s.chapter_end, 2147483647) < $cidx OR s.chapter_end IS NULL) "
            "  AND s.status_json CONTAINS '死亡' "
            "RETURN c.name AS name, s.chapter_start AS start, s.chapter_end AS endc "
            "LIMIT 50"
        )
        async with neo4j_driver.session() as session:
            result = await session.run(query, pid=str(project_id), cidx=int(chapter_idx))
            records = [r async for r in result]
    except Exception as exc:
        logger.debug("time_reversal neo4j query failed: %s", exc)
        return issues

    for rec in records:
        name = rec.get("name") or ""
        if not name or name not in draft:
            continue
        # name 在 draft 中，且同章 draft 包含 alive 关键词，投 hard
        for kw in alive_hits:
            # 粗略的就近分析：name 与 kw 距离 ≤ 20 字符
            idx_n = draft.find(name)
            idx_k = draft.find(kw)
            if idx_n < 0 or idx_k < 0:
                continue
            if abs(idx_n - idx_k) <= 30:
                issues.append(
                    {
                        "severity": "hard",
                        "category": "consistency_time_reversal",
                        "desc": f"角色 {name} 在早前章节已标记死亡/陕落，本章出现「{kw}」的描述，怀疑时间线倒退。",
                        "location": name,
                        "source": "consistency:time_reversal",
                    }
                )
                break
    return issues
