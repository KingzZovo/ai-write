"""PR-FORESHADOW-LIFECYCLE: Persist foreshadows from outline JSON during the generation pipeline.

The pipeline emits structured ``foreshadows`` blocks at three levels:
  - Volume outline:  content_json["foreshadows"]["planted"|"resolved"]
  - Chapter outline: content_json["foreshadows_planted"|"foreshadows_resolved"]
  - Chapter content/outline.outline_json: same shape as chapter outline

This module:
  1) Reads those blocks immediately after each LLM response is committed.
  2) Inserts new foreshadows into the ``foreshadows`` PG table.
  3) Marks resolved foreshadows accordingly.
  4) Provides a load_active_foreshadows() helper so subsequent volume / chapter
     outline + chapter content prompts can reference existing active foreshadows.

Idempotent by description + planted_chapter (per project): repeated runs for the
same outline don't create dups.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ===========================================================================
# Helpers
# ===========================================================================

async def get_volume_first_global_idx(
    db: AsyncSession,
    project_id: str | UUID,
    volume_idx: int,
) -> int:
    """Sum chapter counts of vols < volume_idx to compute the global offset.

    Returns 0 for vol1 (the first vol). Useful when the chapter idx is local
    (per-volume 0-based) but PG ``foreshadows.planted_chapter`` should be
    book-global (0..total_chapters-1).
    """
    rows = (
        await db.execute(
            sql_text(
                """
                SELECT v.volume_idx, COUNT(c.id) AS cnt
                FROM volumes v
                LEFT JOIN chapters c ON c.volume_id = v.id
                WHERE v.project_id = :pid AND v.volume_idx < :vi
                GROUP BY v.volume_idx
                ORDER BY v.volume_idx
                """
            ),
            {"pid": str(project_id), "vi": int(volume_idx)},
        )
    ).all()
    return int(sum((r[1] or 0) for r in rows))


async def chapter_global_idx(
    db: AsyncSession,
    project_id: str | UUID,
    volume_idx: int,
    chapter_idx_local: int,
) -> int:
    """Convert (volume_idx, chapter_idx_local) into a book-global chapter idx."""
    base = await get_volume_first_global_idx(db, project_id, volume_idx)
    return base + max(0, int(chapter_idx_local))


def _normalize_planted_entry(entry: Any, default_planted: int) -> dict | None:
    """Normalize one item from outline['foreshadows_planted'] / ['foreshadows']['planted']."""
    if entry is None:
        return None
    if isinstance(entry, str):
        s = entry.strip()
        if not s:
            return None
        return {
            "description": s[:600],
            "type": "伏笔",
            "planted_chapter": default_planted,
            "resolve_conditions": None,
        }
    if not isinstance(entry, dict):
        return None
    desc = entry.get("description") or entry.get("desc") or entry.get("text")
    if not isinstance(desc, str):
        return None
    desc = desc.strip()
    if not desc:
        return None
    typ = entry.get("type") or entry.get("category") or "伏笔"
    if not isinstance(typ, str):
        typ = "伏笔"
    typ = typ.strip()[:20] or "伏笔"
    pch_v = entry.get("planted_chapter")
    if isinstance(pch_v, (int, float)):
        planted = int(pch_v)
    else:
        planted = default_planted
    rc = (
        entry.get("resolve_conditions")
        or entry.get("hint")
        or entry.get("resolve_hint")
    )
    return {
        "description": desc[:600],
        "type": typ,
        "planted_chapter": max(0, planted),
        "resolve_conditions": rc,
    }


# ===========================================================================
# Persist (write side)
# ===========================================================================

async def _insert_foreshadow_if_new(
    db: AsyncSession,
    project_id: str | UUID,
    item: dict,
) -> bool:
    """INSERT one foreshadow if not already present (by description + project)."""
    desc = item["description"]
    # Idempotency: skip if a row with the same project_id + description already exists.
    existing = (
        await db.execute(
            sql_text(
                """
                SELECT 1 FROM foreshadows
                WHERE project_id = :pid AND description = :desc
                LIMIT 1
                """
            ),
            {"pid": str(project_id), "desc": desc},
        )
    ).first()
    if existing:
        return False
    rh: Any = item.get("resolve_conditions")
    rh_obj: dict | list = {}
    if isinstance(rh, list):
        rh_obj = rh
    elif isinstance(rh, dict):
        rh_obj = rh
    elif isinstance(rh, str) and rh.strip():
        rh_obj = {"hint": rh.strip()[:500]}
    import json as _json
    await db.execute(
        sql_text(
            """
            INSERT INTO foreshadows (
              id, project_id, type, description, planted_chapter,
              status, created_at, resolve_conditions_json, resolution_blueprint_json
            ) VALUES (
              gen_random_uuid(), :pid, :type, :desc, :pch,
              'pending', now(), CAST(:rh AS json), CAST(:rb AS json)
            )
            """
        ),
        {
            "pid": str(project_id),
            "type": item["type"],
            "desc": desc,
            "pch": int(item["planted_chapter"]),
            "rh": _json.dumps(rh_obj, ensure_ascii=False),
            "rb": "{}",
        },
    )
    return True


async def _resolve_foreshadows(
    db: AsyncSession,
    project_id: str | UUID,
    resolved_descriptions: list[str],
    resolved_chapter: int,
) -> int:
    """Update active foreshadows whose description fuzzy-matches one of the resolved entries."""
    if not resolved_descriptions:
        return 0
    cnt = 0
    for d in resolved_descriptions:
        if not isinstance(d, str):
            continue
        d_norm = d.strip()
        if len(d_norm) < 6:
            continue
        # Match by ILIKE on a 12-char window — good enough for short LLM-emitted refs.
        sample = d_norm[:24]
        result = await db.execute(
            sql_text(
                """
                UPDATE foreshadows SET
                  status = 'resolved',
                  resolved_chapter = :rc
                WHERE project_id = :pid
                  AND status != 'resolved'
                  AND description ILIKE '%' || :sample || '%'
                """
            ),
            {"pid": str(project_id), "rc": int(resolved_chapter), "sample": sample},
        )
        cnt += int(getattr(result, "rowcount", 0) or 0)
    return cnt


async def persist_foreshadows_from_volume_outline(
    db: AsyncSession,
    project_id: str | UUID,
    volume_idx: int,
    content_json: dict,
) -> tuple[int, int]:
    """Read content_json['foreshadows']['planted'|'resolved'] and persist.

    Returns (inserted, resolved_marked).
    """
    if not isinstance(content_json, dict):
        return 0, 0
    fs_block = content_json.get("foreshadows")
    if not isinstance(fs_block, dict):
        return 0, 0
    # Volume-level foreshadows default to the first chapter of that volume.
    base_idx = await get_volume_first_global_idx(db, project_id, int(volume_idx))
    inserted = 0
    for entry in fs_block.get("planted") or []:
        item = _normalize_planted_entry(entry, default_planted=base_idx)
        if not item:
            continue
        # If LLM gave a per-volume idx, convert to global.
        if 0 <= item["planted_chapter"] < 200:
            item["planted_chapter"] = base_idx + item["planted_chapter"]
        ok = await _insert_foreshadow_if_new(db, project_id, item)
        if ok:
            inserted += 1
    # Resolved: mark prior active ones with description match.
    resolved_descs = [
        d for d in (fs_block.get("resolved") or [])
        if isinstance(d, str)
    ]
    resolved = await _resolve_foreshadows(db, project_id, resolved_descs, base_idx)
    if inserted or resolved:
        try:
            await db.commit()
        except Exception as e:
            logger.warning("persist_foreshadows_from_volume_outline commit fail: %s", e)
    logger.info(
        "foreshadow lifecycle [volume] project=%s vol=%s inserted=%d resolved=%d",
        project_id, volume_idx, inserted, resolved,
    )
    return inserted, resolved


async def persist_foreshadows_from_chapter_outline(
    db: AsyncSession,
    project_id: str | UUID,
    global_chapter_idx_arg: int,
    content_json: dict,
) -> tuple[int, int]:
    """Read content_json['foreshadows_planted'|'foreshadows_resolved'] and persist."""
    if not isinstance(content_json, dict):
        return 0, 0
    planted_arr = content_json.get("foreshadows_planted") or []
    resolved_arr = content_json.get("foreshadows_resolved") or []
    inserted = 0
    for entry in planted_arr:
        item = _normalize_planted_entry(entry, default_planted=int(global_chapter_idx_arg))
        if not item:
            continue
        ok = await _insert_foreshadow_if_new(db, project_id, item)
        if ok:
            inserted += 1
    resolved = await _resolve_foreshadows(
        db, project_id, [d for d in resolved_arr if isinstance(d, str)], int(global_chapter_idx_arg)
    )
    if inserted or resolved:
        try:
            await db.commit()
        except Exception as e:
            logger.warning("persist_foreshadows_from_chapter_outline commit fail: %s", e)
    logger.info(
        "foreshadow lifecycle [chapter] project=%s ch_global=%s inserted=%d resolved=%d",
        project_id, global_chapter_idx_arg, inserted, resolved,
    )
    return inserted, resolved


# ===========================================================================
# Load (read side) — used by chapter outline expander + chapter content prompt
# ===========================================================================

async def load_active_foreshadows_for_context(
    db: AsyncSession,
    project_id: str | UUID,
    upcoming_global_chapter_idx: int | None = None,
    limit: int = 25,
) -> list[dict]:
    """Return active (non-resolved) foreshadows planted before/at the upcoming chapter.

    Each item: {description, type, planted_chapter, hint, status, age}
    """
    where_clause = "project_id = :pid AND status != 'resolved'"
    params: dict = {"pid": str(project_id), "limit": int(limit)}
    if upcoming_global_chapter_idx is not None:
        where_clause += " AND planted_chapter <= :upper"
        params["upper"] = int(upcoming_global_chapter_idx)
    sql = sql_text(
        f"""
        SELECT description, type, planted_chapter, status, resolve_conditions_json
        FROM foreshadows
        WHERE {where_clause}
        ORDER BY planted_chapter DESC, created_at DESC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, params)).all()
    out: list[dict] = []
    for desc, typ, pch, status, rh in rows:
        rc = rh
        hint = ""
        if isinstance(rc, dict):
            hint = str(rc.get("hint") or "")[:200]
        elif isinstance(rc, list) and rc:
            hint = str(rc[0])[:200]
        item = {
            "description": desc,
            "type": typ,
            "planted_chapter": int(pch) if pch is not None else 0,
            "status": status,
            "hint": hint,
        }
        if upcoming_global_chapter_idx is not None and pch is not None:
            item["age"] = max(0, int(upcoming_global_chapter_idx) - int(pch))
        out.append(item)
    return out


def format_active_foreshadows_for_prompt(items: list[dict]) -> str:
    """Render active foreshadows as a directive block for prompt injection."""
    if not items:
        return ""
    lines: list[str] = [
        "【现有活跃伏笔 (status 非 resolved)】",
        "以下伏笔已被前文埋下，在后续生成中请：",
        "  - 合适时推进（含蔓延、提示、反转、部分揭示）",
        "  - 不要重复埋同样的伏笔（避免冲突或凒余）",
        "  - 是否收束需交由剧情节奏决定，如本章发生了明确揭示请在 foreshadows_resolved 中列出与下面描述近似的项。",
    ]
    for i, it in enumerate(items, 1):
        age = it.get("age")
        age_text = f" 已埋 {age} 章" if age is not None else ""
        hint = it.get("hint") or ""
        hint_part = f"  提示：{hint}" if hint else ""
        lines.append(
            f"  {i}. [{it.get('type','伏笔')}] (埋于 [CH-{it.get('planted_chapter')}]{age_text}) {it.get('description','')}"
        )
        if hint_part:
            lines.append(hint_part)
    return "\n".join(lines)
