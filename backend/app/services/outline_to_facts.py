"""v1.7.4 P0-3 + Phase I (2026-05-03): outline -> facts ETL.

Pre-Phase-I coverage: characters / foreshadows / world_rules only (3 tables).
Problem found in V3B probe (2026-05-03): foreshadows / organizations / items
remained 0 after a full 200万字 run. The ETL only consumed the 5-volume outline
(volume.foreshadows.planted) but never the 750 chapter outlines, and never
touched organizations / items at all.

Phase I additions:
- etl_foreshadows now also reads chapter.outline_json.foreshadow_state
  (one entry per chapter), so each "planted：…" / "resolved：…" line
  becomes a Foreshadow row with planted_chapter = that chapter's index.
- etl_organizations (NEW): LLM-extracts organizations from book.raw_text
  + the concatenated chapter summaries; writes to organizations table.
- etl_items (NEW): LLM-extracts items from book.raw_text + chapter
  summaries; writes to items table via raw SQL (no ORM model).
- run_full_etl now runs all 5 ETLs in sequence.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.neo4j import init_neo4j
from app.db import neo4j as _neo4j_mod
from app.tasks.entity_tasks import _materialize_entities_to_postgres
from app.models.project import (
    Character,
    Chapter,
    Foreshadow,
    Organization,
    Outline,
    Volume,
    WorldRule,
)

logger = logging.getLogger(__name__)


async def _load_book_outline_text(
    db: AsyncSession, project_id: str | UUID
) -> str:
    res = await db.execute(
        select(Outline.content_json)
        .where(Outline.project_id == str(project_id), Outline.level == "book")
        .order_by(Outline.version.desc())
        .limit(1)
    )
    row = res.first()
    if not row:
        return ""
    cj = row[0] or {}
    return cj.get("raw_text") or cj.get("summary") or ""


async def _load_volume_outlines(
    db: AsyncSession, project_id: str | UUID
) -> list[dict]:
    res = await db.execute(
        select(Outline.content_json)
        .where(
            Outline.project_id == str(project_id),
            Outline.level == "volume",
        )
        .order_by(Outline.version.desc())
    )
    out: list[dict] = []
    seen_idx: set[int] = set()
    for row in res.all():
        cj = row[0] or {}
        if not isinstance(cj, dict):
            continue
        vidx = cj.get("volume_idx")
        if isinstance(vidx, int) and vidx in seen_idx:
            continue
        if isinstance(vidx, int):
            seen_idx.add(vidx)
        out.append(cj)
    return out


async def _load_chapter_outlines(
    db: AsyncSession, project_id: str | UUID
) -> list[tuple[int, int, dict]]:
    """Return [(volume_idx, chapter_idx, outline_json)] for all chapters."""
    res = await db.execute(
        select(Volume.volume_idx, Chapter.chapter_idx, Chapter.outline_json)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == str(project_id))
        .order_by(Volume.volume_idx, Chapter.chapter_idx)
    )
    out: list[tuple[int, int, dict]] = []
    for vidx, cidx, oj in res.all():
        if isinstance(oj, dict) and oj:
            out.append((int(vidx or 1), int(cidx or 0), oj))
    return out


async def etl_characters(
    db: AsyncSession,
    project_id: str | UUID,
) -> tuple[int, int]:
    pid = str(project_id)
    inserted = 0
    skipped = 0
    existing_res = await db.execute(
        select(Character.name).where(Character.project_id == pid)
    )
    existing_names = {r[0] for r in existing_res.all() if r[0]}
    volumes = await _load_volume_outlines(db, pid)
    for vol in volumes:
        new_chars = vol.get("new_characters") or []
        if not isinstance(new_chars, list):
            continue
        for nc in new_chars:
            if not isinstance(nc, dict):
                continue
            name = (nc.get("name") or "").strip()
            if not name or name in existing_names:
                skipped += 1
                continue
            profile = {
                "role": nc.get("role", ""),
                "identity": nc.get("identity", ""),
                "introduced_in_volume": vol.get("volume_idx"),
                "source": "outline_to_facts.etl_characters",
            }
            db.add(Character(
                id=uuid.uuid4(),
                project_id=uuid.UUID(pid),
                name=name[:200],
                profile_json=profile,
            ))
            existing_names.add(name)
            inserted += 1
    if inserted:
        await db.commit()
    return inserted, skipped


_FORE_LINE_RE = re.compile(r"^\s*(planted|resolved|partial|reinforced)\s*[：:]\s*(.+?)\s*$")


def _split_foreshadow_state(text: str) -> list[tuple[str, str]]:
    """Parse chapter.outline_json.foreshadow_state into [(status, desc)].

    Each non-empty line typically looks like "planted：…" or "resolved：…".
    Lines without the pattern are still kept as (planted, line).
    """
    out: list[tuple[str, str]] = []
    if not isinstance(text, str):
        return out
    for raw in text.replace("\r", "").split("\n"):
        s = raw.strip()
        if not s:
            continue
        m = _FORE_LINE_RE.match(s)
        if m:
            out.append((m.group(1).lower(), m.group(2).strip()))
        else:
            out.append(("planted", s))
    return out


async def etl_foreshadows(
    db: AsyncSession,
    project_id: str | UUID,
) -> tuple[int, int]:
    """Insert foreshadows from BOTH volume outline AND each chapter's foreshadow_state."""
    pid = str(project_id)
    inserted = 0
    skipped = 0

    existing_res = await db.execute(
        select(Foreshadow.description, Foreshadow.planted_chapter).where(
            Foreshadow.project_id == pid
        )
    )
    existing_keys: set[tuple[str, int]] = set()
    for desc, plc in existing_res.all():
        existing_keys.add(((desc or "")[:80], int(plc or 0)))

    await init_neo4j()
    driver = _neo4j_mod._driver
    if driver is None:
        logger.warning("etl_foreshadows: neo4j driver not initialized; PG-only writes")

    async def _write_one(desc: str, planted_chapter: int, status: str, conds: list, vidx: int) -> bool:
        key = (desc[:80], int(planted_chapter))
        if key in existing_keys or not desc:
            return False
        existing_keys.add(key)
        fid = str(uuid.uuid4())
        # PG insert via ORM
        db.add(Foreshadow(
            id=uuid.UUID(fid),
            project_id=uuid.UUID(pid),
            type="plot",
            description=desc,
            planted_chapter=int(planted_chapter),
            resolve_conditions_json=conds if isinstance(conds, list) else [],
            resolution_blueprint_json={},
            narrative_proximity=0.5,
            status=status if status in ("planted", "resolved", "partial", "reinforced") else "planted",
            resolved_chapter=None,
        ))
        # Neo4j mirror
        if driver is not None:
            try:
                async with driver.session() as session:
                    r = await session.run(
                        "MERGE (f:Foreshadow {project_id: $pid, id: $id}) "
                        "SET f.type = $type, "
                        "    f.description = $desc, "
                        "    f.planted_chapter = $planted, "
                        "    f.resolve_conditions_json = $conds, "
                        "    f.resolution_blueprint_json = $blueprint, "
                        "    f.narrative_proximity = $prox, "
                        "    f.status = $status, "
                        "    f.resolved_chapter = $resolved "
                        "RETURN f.id AS id",
                        pid=pid,
                        id=fid,
                        type="plot",
                        desc=desc,
                        planted=int(planted_chapter),
                        conds=json.dumps(conds if isinstance(conds, list) else [], ensure_ascii=False),
                        blueprint=json.dumps({}, ensure_ascii=False),
                        prox=0.5,
                        status=status,
                        resolved=None,
                    )
                    await r.consume()
            except Exception as e:
                logger.warning("etl_foreshadows: neo4j write failed: %s", e)
        return True

    # 1) Volume-level planted foreshadows (legacy path).
    volumes = await _load_volume_outlines(db, pid)
    for vol in volumes:
        fs = vol.get("foreshadows") or {}
        if not isinstance(fs, dict):
            continue
        planted = fs.get("planted") or []
        if not isinstance(planted, list):
            continue
        vidx = int(vol.get("volume_idx") or 1)
        for f in planted:
            if not isinstance(f, dict):
                continue
            desc = (f.get("description") or "").strip()
            if not desc:
                continue
            cond = f.get("resolve_conditions") or []
            ok = await _write_one(desc, vidx, "planted", cond, vidx)
            if ok:
                inserted += 1
            else:
                skipped += 1

    # 2) Chapter-level foreshadow_state (Phase I new path).
    chapter_outlines = await _load_chapter_outlines(db, pid)
    # Compute global chapter_idx (volume_idx*1000 + chapter_idx) so collisions across volumes don't merge.
    for vidx, cidx, oj in chapter_outlines:
        fs_text = oj.get("foreshadow_state") if isinstance(oj, dict) else None
        if not isinstance(fs_text, str) or not fs_text.strip():
            continue
        for status, desc in _split_foreshadow_state(fs_text):
            if not desc:
                continue
            global_cidx = vidx * 1000 + cidx
            ok = await _write_one(desc, global_cidx, status, [], vidx)
            if ok:
                inserted += 1
            else:
                skipped += 1

    if inserted:
        await db.commit()
        try:
            await _materialize_entities_to_postgres(
                project_id=pid,
                chapter_idx=0,
                caller="outline_to_facts.etl_foreshadows",
            )
        except Exception as me:
            logger.warning("etl_foreshadows: materialize step failed: %s", me)
    return inserted, skipped


async def etl_world_rules(
    db: AsyncSession,
    project_id: str | UUID,
    *,
    max_rules: int = 30,
) -> tuple[int, int]:
    pid = str(project_id)
    raw = await _load_book_outline_text(db, pid)
    if not raw or len(raw) < 200:
        return 0, 0
    existing_res = await db.execute(
        select(WorldRule.rule_text).where(WorldRule.project_id == pid)
    )
    existing = {(r[0] or "")[:60] for r in existing_res.all()}
    await init_neo4j()
    driver = _neo4j_mod._driver
    if driver is None:
        return 0, 0
    if len(raw) > 12000:
        raw = raw[:8000] + "\n\n…(中部省略)…\n\n" + raw[-4000:]
    instr = (
        f"从下面的全书大纲中抽取世界规则。为每条规则给出一个类别。\n"
        f"输出JSON数组，最多 {max_rules} 条，严格使用以下格式，只返回JSON不要加任何说明：\n"
        '[{"category": "必填中文类别如覆写烟/钟塔系统/学宫制度/禁忌等", "rule_text": "一句话描述这条规则，不超80字"}, ...]\n\n'
        f"【全书大纲】\n{raw}"
    )
    try:
        from app.services.prompt_registry import run_text_prompt
        result = await run_text_prompt(
            task_type="extraction",
            user_content=instr,
            db=db,
            project_id=pid,
        )
        text = (result.text or "").strip()
    except Exception as e:
        logger.warning("etl_world_rules: LLM extraction failed: %s", e)
        return 0, 0
    m = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        items = json.loads(text)
    except Exception:
        m2 = re.search(r"\[[\s\S]*\]", text)
        if not m2:
            return 0, 0
        try:
            items = json.loads(m2.group(0))
        except Exception:
            return 0, 0
    if not isinstance(items, list):
        return 0, 0
    inserted = 0
    skipped = 0
    for it in items[:max_rules]:
        if not isinstance(it, dict):
            continue
        cat = (it.get("category") or "general").strip()[:100]
        rule = (it.get("rule_text") or "").strip()
        if not rule or len(rule) < 4:
            continue
        if rule[:60] in existing:
            skipped += 1
            continue
        rid = str(uuid.uuid4())
        try:
            async with driver.session() as session:
                r = await session.run(
                    "MERGE (w:WorldRule {project_id: $pid, category: $cat, text: $txt}) "
                    "ON CREATE SET w.id = $id "
                    "RETURN w.id AS id",
                    id=rid,
                    pid=pid,
                    cat=cat or "general",
                    txt=rule[:1000],
                )
                await r.consume()
        except Exception as e:
            logger.warning("etl_world_rules: neo4j write failed: %s", e)
            skipped += 1
            continue
        existing.add(rule[:60])
        inserted += 1
    if inserted:
        try:
            await _materialize_entities_to_postgres(
                project_id=pid,
                chapter_idx=0,
                caller="outline_to_facts.etl_world_rules",
            )
        except Exception as me:
            logger.warning("etl_world_rules: materialize failed: %s", me)
    return inserted, skipped


async def _build_chapter_summaries_text(db: AsyncSession, pid: str, max_chars: int = 14000) -> str:
    """Concatenate all chapter outlines into one context blob, capped at max_chars."""
    chs = await _load_chapter_outlines(db, pid)
    parts: list[str] = []
    for vidx, cidx, oj in chs:
        if not isinstance(oj, dict):
            continue
        title = (oj.get("title") or "").strip()
        summ = (oj.get("summary") or "").strip()
        keys = (oj.get("key_scene") or "").strip()
        line = f"V{vidx}C{cidx} {title}：{summ} ｜ {keys}".strip()
        parts.append(line)
    blob = "\n".join(parts)
    if len(blob) > max_chars:
        # keep head + tail to preserve intro and resolution context.
        head = blob[: int(max_chars * 0.6)]
        tail = blob[-int(max_chars * 0.4):]
        blob = head + "\n…(省略)…\n" + tail
    return blob


async def etl_organizations(
    db: AsyncSession,
    project_id: str | UUID,
    *,
    max_orgs: int = 25,
) -> tuple[int, int]:
    """Phase I: LLM-extract organizations from book outline + chapter summaries."""
    pid = str(project_id)
    raw_book = await _load_book_outline_text(db, pid)
    chap_blob = await _build_chapter_summaries_text(db, pid, max_chars=10000)
    corpus = (raw_book[:4000] + "\n\n【章节摘要】\n" + chap_blob).strip()
    if len(corpus) < 200:
        return 0, 0

    existing_res = await db.execute(
        select(Organization.name).where(Organization.project_id == pid)
    )
    existing = {(r[0] or "").strip() for r in existing_res.all() if r[0]}

    instr = (
        f"从下面的全书大纲 + 章节摘要中抽取【组织/机构/派系】（如：车站/部门/商会/军统/中统/汇丰银行/海关/76号/同盟会等）。\n"
        f"输出JSON数组，最多 {max_orgs} 条，只返回JSON不要加任何说明：\n"
        '[{"name": "组织名称不超100字"}, ...]\n\n'
        f"【语料】\n{corpus}"
    )
    try:
        from app.services.prompt_registry import run_text_prompt
        result = await run_text_prompt(
            task_type="extraction",
            user_content=instr,
            db=db,
            project_id=pid,
        )
        text = (result.text or "").strip()
    except Exception as e:
        logger.warning("etl_organizations: LLM extraction failed: %s", e)
        return 0, 0
    m = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        items = json.loads(text)
    except Exception:
        m2 = re.search(r"\[[\s\S]*\]", text)
        if not m2:
            return 0, 0
        try:
            items = json.loads(m2.group(0))
        except Exception:
            return 0, 0
    if not isinstance(items, list):
        return 0, 0

    inserted = 0
    skipped = 0
    for it in items[:max_orgs]:
        name = ""
        if isinstance(it, dict):
            name = (it.get("name") or "").strip()
        elif isinstance(it, str):
            name = it.strip()
        if not name or len(name) < 2 or name in existing:
            skipped += 1
            continue
        db.add(Organization(
            id=uuid.uuid4(),
            project_id=uuid.UUID(pid),
            name=name[:200],
        ))
        existing.add(name)
        inserted += 1
    if inserted:
        await db.commit()
        # Mirror to Neo4j as Organization nodes.
        await init_neo4j()
        driver = _neo4j_mod._driver
        if driver is not None:
            try:
                async with driver.session() as session:
                    for n in list(existing):
                        if not n:
                            continue
                        r = await session.run(
                            "MERGE (o:Organization {project_id: $pid, name: $name}) "
                            "ON CREATE SET o.id = $id "
                            "RETURN o.id AS id",
                            pid=pid, name=n[:200], id=str(uuid.uuid4()),
                        )
                        await r.consume()
            except Exception as e:
                logger.warning("etl_organizations: neo4j mirror failed: %s", e)
    return inserted, skipped


async def etl_items(
    db: AsyncSession,
    project_id: str | UUID,
    *,
    max_items: int = 30,
) -> tuple[int, int]:
    """Phase I: LLM-extract items (props/objects) from book + chapter outlines.

    Writes to public.items via raw SQL (no ORM model class).
    """
    pid = str(project_id)
    raw_book = await _load_book_outline_text(db, pid)
    chap_blob = await _build_chapter_summaries_text(db, pid, max_chars=10000)
    corpus = (raw_book[:4000] + "\n\n【章节摘要】\n" + chap_blob).strip()
    if len(corpus) < 200:
        return 0, 0

    existing_res = await db.execute(
        sql_text("SELECT name FROM items WHERE project_id = :pid"),
        {"pid": pid},
    )
    existing = {(row[0] or "").strip() for row in existing_res.all() if row[0]}

    instr = (
        f"从下面的全书大纲 + 章节摘要中抽取【道具/关键物品】（如：外滩怀表、封签母版、被面纱、伪造证件、代号词本等）。\n"
        f"输出JSON数组，最多 {max_items} 条，只返回JSON：\n"
        '[{"name": "道具名", "kind": "prop|document|weapon|currency|other", "first_owner": "人名或组织名可为空"}, ...]\n\n'
        f"【语料】\n{corpus}"
    )
    try:
        from app.services.prompt_registry import run_text_prompt
        result = await run_text_prompt(
            task_type="extraction",
            user_content=instr,
            db=db,
            project_id=pid,
        )
        text = (result.text or "").strip()
    except Exception as e:
        logger.warning("etl_items: LLM extraction failed: %s", e)
        return 0, 0
    m = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        items = json.loads(text)
    except Exception:
        m2 = re.search(r"\[[\s\S]*\]", text)
        if not m2:
            return 0, 0
        try:
            items = json.loads(m2.group(0))
        except Exception:
            return 0, 0
    if not isinstance(items, list):
        return 0, 0

    inserted = 0
    skipped = 0
    for it in items[:max_items]:
        if not isinstance(it, dict):
            if isinstance(it, str):
                it = {"name": it}
            else:
                continue
        name = (it.get("name") or "").strip()
        kind = (it.get("kind") or "").strip()[:50] or None
        first_owner = (it.get("first_owner") or "").strip()[:200] or None
        if not name or len(name) < 1 or name in existing:
            skipped += 1
            continue
        item_id = str(uuid.uuid4())
        try:
            await db.execute(
                sql_text(
                    "INSERT INTO items (id, project_id, name, kind, first_owner, created_at) "
                    "VALUES (:id, :pid, :name, :kind, :owner, NOW())"
                ),
                {
                    "id": item_id,
                    "pid": pid,
                    "name": name[:200],
                    "kind": kind,
                    "owner": first_owner,
                },
            )
            existing.add(name)
            inserted += 1
        except Exception as e:
            logger.warning("etl_items: insert failed (%s): %s", name, e)
            skipped += 1
    if inserted:
        await db.commit()
        # Mirror to Neo4j Item nodes.
        await init_neo4j()
        driver = _neo4j_mod._driver
        if driver is not None:
            try:
                async with driver.session() as session:
                    for n in list(existing):
                        if not n:
                            continue
                        r = await session.run(
                            "MERGE (i:Item {project_id: $pid, name: $name}) "
                            "ON CREATE SET i.id = $id "
                            "RETURN i.id AS id",
                            pid=pid, name=n[:200], id=str(uuid.uuid4()),
                        )
                        await r.consume()
            except Exception as e:
                logger.warning("etl_items: neo4j mirror failed: %s", e)
    return inserted, skipped


async def run_full_etl(
    db: AsyncSession,
    project_id: str | UUID,
) -> dict[str, Any]:
    """Run all 5 ETLs in sequence and return a structured summary."""
    chars = await etl_characters(db, project_id)
    fores = await etl_foreshadows(db, project_id)
    rules = await etl_world_rules(db, project_id)
    orgs = await etl_organizations(db, project_id)
    its = await etl_items(db, project_id)
    return {
        "characters": {"inserted": chars[0], "skipped": chars[1]},
        "foreshadows": {"inserted": fores[0], "skipped": fores[1]},
        "world_rules": {"inserted": rules[0], "skipped": rules[1]},
        "organizations": {"inserted": orgs[0], "skipped": orgs[1]},
        "items": {"inserted": its[0], "skipped": its[1]},
    }
