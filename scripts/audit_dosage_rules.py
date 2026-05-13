"""PR-DOSAGE-AUDIT (2026-05-13).

Audit which ``style_profiles`` rows have a v8 ``dosage_profile`` populated
but still carry an empty ``rules_json``. This is the documented failure
mode behind the long-standing "styles UI shows 0 rules" complaint: the
v8 剂量画像 pipeline writes ``config_json['dosage_profile']`` but never
called :func:`app.services.dosage_to_rules.derive_rules_from_dosage` to
populate the human-readable rule list.

A quick ``grep -rn derive_rules_from_dosage backend/`` shows there is no
production caller for the deriver — it is effectively dead code today.
This script is the (deliberately small) operational tool that closes the
loop:

    # report-only
    docker exec -w /app ai-write-backend-1 python3 /app/scripts/audit_dosage_rules.py

    # apply: derive rules_json and merge anti_ai_rules in place
    docker exec -w /app ai-write-backend-1 python3 /app/scripts/audit_dosage_rules.py --apply

    # scope to a single profile id
    docker exec -w /app ai-write-backend-1 python3 /app/scripts/audit_dosage_rules.py \\
        --profile-id <uuid>

    # JSON output for piping into other tooling
    docker exec -w /app ai-write-backend-1 python3 /app/scripts/audit_dosage_rules.py --json

The script never deletes rules — ``--apply`` only fills empty rule lists
and merges anti-AI entries via the existing ``merge_anti_ai_rules`` helper.

DB strategy: the production engine is async (``postgresql+asyncpg://…``)
and the container only ships ``asyncpg`` — there is no sync DB driver to
fall back on. So the audit reuses the app's own
``async_session_factory`` and runs through ``asyncio.run``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

# Allow running from repo root or inside the backend container. The docker
# image and uvicorn entrypoint both add /app, so prepend that if available.
for _candidate in ("/app", "backend"):
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:
    from sqlalchemy import select

    from app.db.session import async_session_factory
    from app.models.project import StyleProfile
    from app.services.dosage_to_rules import (
        derive_rules_from_dosage,
        merge_anti_ai_rules,
    )
except Exception as exc:  # pragma: no cover - convenience message
    print(
        "ERROR: could not import backend modules. Run inside the backend"
        " container (docker exec -w /app ai-write-backend-1 python3 "
        "/app/scripts/audit_dosage_rules.py)."
        f"\nDetail: {exc}",
        file=sys.stderr,
    )
    raise


def _classify(profile: StyleProfile) -> dict[str, Any]:
    cfg = profile.config_json if isinstance(profile.config_json, dict) else {}
    dosage = cfg.get("dosage_profile") if isinstance(cfg, dict) else None
    has_dosage = isinstance(dosage, dict) and len(dosage) > 0
    rules = profile.rules_json if isinstance(profile.rules_json, list) else []
    has_rules = len(rules) > 0
    anti_ai = (
        profile.anti_ai_rules if isinstance(profile.anti_ai_rules, list) else []
    )
    return {
        "id": str(profile.id),
        "name": profile.name,
        "source_book_id": str(profile.source_book_id)
        if profile.source_book_id is not None
        else None,
        "has_dosage": has_dosage,
        "dosage_top_level_keys": sorted(dosage.keys()) if has_dosage else [],
        "rules_count": len(rules),
        "anti_ai_count": len(anti_ai),
        "needs_derive": has_dosage and not has_rules,
    }


def _apply(profile: StyleProfile) -> dict[str, int]:
    cfg = profile.config_json if isinstance(profile.config_json, dict) else {}
    dosage = cfg.get("dosage_profile") if isinstance(cfg, dict) else {}
    version = cfg.get("profile_version") if isinstance(cfg, dict) else None
    rules, anti_additions = derive_rules_from_dosage(
        dosage if isinstance(dosage, dict) else {}, profile_version=version
    )
    merged_anti = merge_anti_ai_rules(
        profile.anti_ai_rules if isinstance(profile.anti_ai_rules, list) else [],
        anti_additions,
    )
    profile.rules_json = rules
    profile.anti_ai_rules = merged_anti
    return {"derived_rules": len(rules), "final_anti_ai": len(merged_anti)}


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async with async_session_factory() as session:
        stmt = select(StyleProfile)
        if args.profile_id:
            stmt = stmt.filter(StyleProfile.id == args.profile_id)
        result = await session.execute(stmt)
        profiles = list(result.scalars().all())
        report = [_classify(p) for p in profiles]
        needs = [r for r in report if r["needs_derive"]]

        applied: list[dict[str, Any]] = []
        if args.apply and needs:
            wanted_ids = {r["id"] for r in needs}
            for p in profiles:
                if str(p.id) not in wanted_ids:
                    continue
                result_info = _apply(p)
                applied.append({"id": str(p.id), "name": p.name, **result_info})
            await session.commit()

        return {
            "total": len(report),
            "with_dosage": sum(1 for r in report if r["has_dosage"]),
            "with_rules": sum(1 for r in report if r["rules_count"] > 0),
            "needs_derive": len(needs),
            "applied_count": len(applied),
            "profiles": report,
            "needs": needs,
            "applied": applied,
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write derived rules back to DB")
    ap.add_argument("--profile-id", help="only audit a single style profile id")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    payload = asyncio.run(_run(args))

    if args.json:
        # Drop internal helper key before dumping.
        out = {k: v for k, v in payload.items() if k != "needs"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"Scanned {payload['total']} style profile(s).")
    print(f"  with dosage_profile:        {payload['with_dosage']}")
    print(f"  with rules_json non-empty:  {payload['with_rules']}")
    print(f"  needs derive (dosage + 0 rules): {payload['needs_derive']}")
    for r in payload["needs"]:
        print(
            f"    - {r['id']}  name={r['name']!r}  rules={r['rules_count']}"
            f"  anti_ai={r['anti_ai_count']}"
        )
    if payload["applied"]:
        print("Applied derive_rules_from_dosage to:")
        for a in payload["applied"]:
            print(
                f"  - {a['id']}  {a['name']!r}  +{a['derived_rules']} rules,"
                f" anti_ai now {a['final_anti_ai']}"
            )
    elif args.apply:
        print("Nothing to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
