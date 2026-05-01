"""Backfill Postgres settings tables into Neo4j (v1.9).

Goal: help converge to the "Neo4j is source of truth" architecture by copying
existing Postgres settings data into Neo4j, using idempotent MERGE semantics.

Scope (current):
  - characters (project_id, name) + optional profile_json
  - world_rules (project_id, category, rule_text)
  - relationships (project_id, source_name, target_name, rel_type)

Usage::

  docker exec -e PYTHONPATH=/app -w /app ai-write-backend-1 \
    python -m app.scripts.backfill_settings_to_neo4j --project-id <uuid> --dry-run

  docker exec -e PYTHONPATH=/app -w /app ai-write-backend-1 \
    python -m app.scripts.backfill_settings_to_neo4j --project-id <uuid>

Notes:
  - This script is safe to re-run. It uses Neo4j MERGE on unique keys.
  - It intentionally does NOT delete anything in Postgres.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid

logger = logging.getLogger("backfill_settings_to_neo4j")


async def _amain(args: argparse.Namespace) -> int:
    from sqlalchemy import select, text

    from app.db.neo4j import init_neo4j
    from app.db.session import async_session_factory
    from app.models.project import Character, WorldRule
    from app.services.rel_type import canonicalize_rel_type

    if not args.project_id:
        print("ERROR: --project-id is required")
        return 2

    project_id = str(args.project_id)

    # Load from Postgres
    async with async_session_factory() as db:
        chars_result = await db.execute(
            select(Character).where(Character.project_id == project_id)
        )
        chars = list(chars_result.scalars().all())

        rules_result = await db.execute(
            select(WorldRule).where(WorldRule.project_id == project_id)
        )
        rules = list(rules_result.scalars().all())

        rel_rows = await db.execute(
            text(
                "SELECT c1.name AS source, c2.name AS target, r.rel_type AS rel_type "
                "FROM relationships r "
                "JOIN characters c1 ON c1.id = r.source_id "
                "JOIN characters c2 ON c2.id = r.target_id "
                "WHERE r.project_id = :pid"
            ),
            {"pid": project_id},
        )
        rels = [
            (
                str(row[0] or "").strip(),
                str(row[1] or "").strip(),
                canonicalize_rel_type(str(row[2] or "").strip()),
            )
            for row in rel_rows.all()
            if str(row[0] or "").strip()
            and str(row[1] or "").strip()
            and str(row[2] or "").strip()
        ]

    print(
        f"Discovered pg settings rows: characters={len(chars)} world_rules={len(rules)} relationships={len(rels)}"
    )

    if args.dry_run:
        for c in chars[:10]:
            print(f"  [dry-run] character name={c.name!r}")
        if len(chars) > 10:
            print(f"  ... and {len(chars) - 10} more characters")
        for w in rules[:10]:
            print(f"  [dry-run] world_rule category={w.category!r} text={w.rule_text!r}")
        if len(rules) > 10:
            print(f"  ... and {len(rules) - 10} more world_rules")
        for (src, tgt, rtype) in rels[:10]:
            print(f"  [dry-run] relationship {src!r} -[{rtype}]-> {tgt!r}")
        if len(rels) > 10:
            print(f"  ... and {len(rels) - 10} more relationships")
        return 0

    # Write to Neo4j
    await init_neo4j()
    from app.db import neo4j as _neo4j_mod

    driver = _neo4j_mod._driver
    if driver is None:
        print("ERROR: neo4j driver not initialized")
        return 2

    wrote_chars = 0
    wrote_rules = 0
    wrote_rels = 0

    async with driver.session() as session:
        for c in chars:
            profile = c.profile_json if isinstance(c.profile_json, dict) else {}
            profile_str = json.dumps(profile, ensure_ascii=False)
            res = await session.run(
                "MERGE (n:Character {project_id: $pid, name: $name}) "
                "ON CREATE SET n.id = $id "
                "SET n.profile_json = $profile",
                pid=project_id,
                name=str(c.name).strip(),
                id=str(c.id),
                profile=profile_str,
            )
            await res.consume()
            wrote_chars += 1

        for w in rules:
            cat = str(w.category).strip()
            txt = str(w.rule_text).strip()
            if not cat or not txt:
                continue
            res = await session.run(
                "MERGE (n:WorldRule {project_id: $pid, category: $cat, text: $txt}) "
                "ON CREATE SET n.id = $id",
                pid=project_id,
                cat=cat,
                txt=txt,
                id=str(w.id),
            )
            await res.consume()
            wrote_rules += 1

        # Relationships: write RELATES_TO edges.
        # Use chapter_start=0 for settings relationships (timeless settings model).
        for (src, tgt, rtype) in rels:
            res = await session.run(
                "MERGE (a:Character {project_id: $pid, name: $src}) "
                "ON CREATE SET a.id = $aid",
                pid=project_id,
                src=src,
                aid=str(uuid.uuid4()),
            )
            await res.consume()
            res = await session.run(
                "MERGE (b:Character {project_id: $pid, name: $tgt}) "
                "ON CREATE SET b.id = $bid",
                pid=project_id,
                tgt=tgt,
                bid=str(uuid.uuid4()),
            )
            await res.consume()
            # Avoid cartesian product warnings by matching endpoints in a single connected pattern.
            res = await session.run(
                "MATCH (a:Character {project_id: $pid, name: $src}) "
                "MATCH (b:Character {project_id: $pid, name: $tgt}) "
                "MERGE (a)-[rel:RELATES_TO {project_id: $pid, source_name: $src, target_name: $tgt, type: $rtype, chapter_start: 0}]->(b) "
                "ON CREATE SET rel.chapter_end = null",
                pid=project_id,
                src=src,
                tgt=tgt,
                rtype=rtype,
            )
            await res.consume()
            wrote_rels += 1

    print(
        f"Backfill complete: wrote characters={wrote_chars} world_rules={wrote_rules} relationships={wrote_rels}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

