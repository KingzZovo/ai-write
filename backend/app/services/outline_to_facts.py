"""v1.7.4 P0-3: outline -> facts ETL.

Problem (pre-v1.7.4): The 3 fact tables (characters / foreshadows / world_rules)
stay empty for the entire project lifecycle. ContextPack._build_facts queries
these tables (line 715/727/768 of context_pack.py) so when they're empty the
L2 "事实约束" section of the system prompt is just absent. Result: when
generating ch8 the model has no character cards, no foreshadow tracking, and
no world rules — explaining King's complaint that "生成章节这个环节后就没办
法看到人物关系、伏笔、埋点等等都是空白".

Fix: ETL the rich outline data we already have (volume.new_characters,
volume.foreshadows.planted, book.raw_text) into the 3 tables.

- Volume.new_characters[{name, role, identity}] -> characters table
  (deterministic, no LLM needed; profile_json={role, identity})
- Volume.foreshadows.planted[{description, resolve_conditions[]}]
  -> foreshadows table (status='planted', planted_chapter=1,
  type='plot', resolve_conditions_json=conditions, narrative_proximity=0.5)
- Book.raw_text -> world_rules table via LLM extraction
  (uses task_type='extraction' since 'world_rules_extraction' has empty prompt)

Idempotent: skips entities whose name (chars) or description prefix (foreshadows)
or rule_text (rules) already exists for the project.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.neo4j import init_neo4j
from app.db import neo4j as _neo4j_mod
from app.tasks.entity_tasks import _materialize_entities_to_postgres
from app.models.project import Character, Foreshadow, Outline, WorldRule

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


async def etl_characters(
    db: AsyncSession,
    project_id: str | UUID,
) -> tuple[int, int]:
    """Insert volume.new_characters rows into characters table.

    Returns (inserted, skipped).
    """
    pid = str(project_id)
    inserted = 0
    skipped = 0

    # Existing names for idempotency.
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


async def etl_foreshadows(
    db: AsyncSession,
    project_id: str | UUID,
) -> tuple[int, int]:
    """Insert volume.foreshadows.planted rows into foreshadows table.

    Returns (inserted, skipped).
    """
    pid = str(project_id)
    inserted = 0
    skipped = 0

    existing_res = await db.execute(
        select(Foreshadow.description).where(Foreshadow.project_id == pid)
    )
    existing_descs = {(r[0] or "")[:80] for r in existing_res.all()}

    await init_neo4j()
    driver = _neo4j_mod._driver
    if driver is None:
        logger.warning("etl_foreshadows: neo4j driver not initialized")
        return 0, 0

    volumes = await _load_volume_outlines(db, pid)
    for vol in volumes:
        fs = vol.get("foreshadows") or {}
        if not isinstance(fs, dict):
            continue
        planted = fs.get("planted") or []
        if not isinstance(planted, list):
            continue
        vidx = vol.get("volume_idx") or 1
        # Volume always plants in chapter 1 of that volume by default —
        # the outline doesn't say which exact chapter. Setting planted_chapter
        # to 1 keeps the L2 prompt readable; the chapter generator can refine
        # later.
        for f in planted:
            if not isinstance(f, dict):
                continue
            desc = (f.get("description") or "").strip()
            if not desc:
                continue
            key = desc[:80]
            if key in existing_descs:
                skipped += 1
                continue
            cond = f.get("resolve_conditions") or []
            fid = str(uuid.uuid4())
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
                        planted=int(vidx),
                        conds=json.dumps(cond if isinstance(cond, list) else [], ensure_ascii=False),
                        blueprint=json.dumps({}, ensure_ascii=False),
                        prox=0.5,
                        status="planted",
                        resolved=None,
                    )
                    await r.consume()
            except Exception as e:
                logger.warning("etl_foreshadows: neo4j write failed: %s", e)
                skipped += 1
                continue
            existing_descs.add(key)
            inserted += 1

    if inserted:
        await _materialize_entities_to_postgres(
            project_id=pid,
            chapter_idx=0,
            caller="outline_to_facts.etl_foreshadows",
        )
    return inserted, skipped


async def etl_world_rules(
    db: AsyncSession,
    project_id: str | UUID,
    *,
    max_rules: int = 30,
) -> tuple[int, int]:
    """LLM-extract world rules from book.raw_text and insert into world_rules.

    Returns (inserted, skipped).
    Uses task_type='extraction'. Returns (0, 0) if book outline is missing.
    """
    pid = str(project_id)
    raw = await _load_book_outline_text(db, pid)
    if not raw or len(raw) < 200:
        logger.info("etl_world_rules: book outline missing or too short")
        return 0, 0

    existing_res = await db.execute(
        select(WorldRule.rule_text).where(WorldRule.project_id == pid)
    )
    existing = {(r[0] or "")[:60] for r in existing_res.all()}

    # Cap input to keep extraction cost bounded.
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

    # Strip markdown fences if present.
    m = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # Find the first JSON array in the response.
    try:
        items = json.loads(text)
    except Exception:
        m2 = re.search(r"\[[\s\S]*\]", text)
        if not m2:
            logger.warning("etl_world_rules: could not parse JSON from output")
            return 0, 0
        try:
            items = json.loads(m2.group(0))
        except Exception as e:
            logger.warning("etl_world_rules: nested parse failed: %s", e)
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
        db.add(WorldRule(
            id=uuid.uuid4(),
            project_id=uuid.UUID(pid),
            category=cat or "general",
            rule_text=rule[:1000],
            metadata_json={"source": "outline_to_facts.etl_world_rules"},
        ))
        existing.add(rule[:60])
        inserted += 1
    if inserted:
        await db.commit()
    return inserted, skipped


async def run_full_etl(
    db: AsyncSession,
    project_id: str | UUID,
) -> dict[str, Any]:
    """Run all 3 ETLs in sequence and return a structured summary."""
    chars = await etl_characters(db, project_id)
    fores = await etl_foreshadows(db, project_id)
    rules = await etl_world_rules(db, project_id)
    return {
        "characters": {"inserted": chars[0], "skipped": chars[1]},
        "foreshadows": {"inserted": fores[0], "skipped": fores[1]},
        "world_rules": {"inserted": rules[0], "skipped": rules[1]},
    }
