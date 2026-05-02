#!/usr/bin/env python3
"""Backfill rules_json + anti_ai_rules for every StyleProfile that has a
config_json.dosage_profile.

Deterministic and idempotent: re-running this is a no-op once DBs match the
deriver. Default mode is dry-run; pass --apply to persist.

Usage:
  docker exec ai-write-backend-1 python scripts/style_profile/recompile_all_dosage_rules.py [--apply] [--profile-id <uuid>]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import select


async def _run(dry_run: bool, profile_id: str | None) -> int:
    from app.db.session import async_session_factory
    from app.models.project import StyleProfile
    from app.services.dosage_to_rules import (
        derive_rules_from_dosage,
        merge_anti_ai_rules,
    )

    changed = 0
    skipped = 0
    async with async_session_factory() as db:
        stmt = select(StyleProfile)
        if profile_id:
            stmt = stmt.where(StyleProfile.id == profile_id)
        rows = (await db.execute(stmt)).scalars().all()
        for prof in rows:
            cfg = prof.config_json or {}
            dosage: dict[str, Any] | None = (
                cfg.get("dosage_profile") if isinstance(cfg, dict) else None
            )
            if not isinstance(dosage, dict) or not dosage:
                skipped += 1
                continue
            existing_rules = list(prof.rules_json or [])
            existing_anti = list(prof.anti_ai_rules or [])
            derived_rules, derived_anti = derive_rules_from_dosage(
                dosage, profile_version=cfg.get("profile_version")
            )
            merged_anti = merge_anti_ai_rules(existing_anti, derived_anti)
            if derived_rules == existing_rules and merged_anti == existing_anti:
                skipped += 1
                print(f"  [unchanged] {prof.id} ({prof.name})  rules={len(existing_rules)}")
                continue
            print(
                f"  [diff] {prof.id} ({prof.name}): "
                f"rules {len(existing_rules)} -> {len(derived_rules)}, "
                f"anti_ai {len(existing_anti)} -> {len(merged_anti)}"
            )
            if not dry_run:
                prof.rules_json = derived_rules
                prof.anti_ai_rules = merged_anti
                changed += 1
        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    print(f"\n{'DRY-RUN ' if dry_run else ''}done. changed={changed} skipped={skipped}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--profile-id", default=None)
    args = parser.parse_args()
    return asyncio.run(_run(dry_run=not args.apply, profile_id=args.profile_id))


if __name__ == "__main__":
    sys.exit(main())
