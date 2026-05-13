"""PR-PROFILE-SEED-RULES (2026-05-14).

Fill in ``rules_json`` (and optionally ``anti_ai_rules`` / ``sample_passages``)
for any ``StyleProfile`` row that:

  * has a non-null ``source_book_id`` pointing at a ``reference_books`` row
    that already has ``text_chunks`` ingested, and
  * still carries an empty ``rules_json`` array.

This is the operational tool that closes the long-standing gap between
« profile exists, points at a book » and « profile actually has rules so
it can be used in generation ». The existing ``POST /api/styles/
{style_id}/regenerate-anti-ai`` endpoint requires ``sample_passages`` to
already be present, and the ``POST /api/styles/detect-from-book/
{book_id}`` endpoint *creates a new profile* rather than backfilling the
bound one. Neither covers the case we keep hitting in production, which
is why this script exists.

Usage::

    docker exec -w /app ai-write-backend-1 python3 /tmp/seed_profile_rules.py
        # report-only: list profiles that need seeding, no LLM calls, no writes.

    docker exec -w /app ai-write-backend-1 python3 /tmp/seed_profile_rules.py --apply
        # run detect_style_features + detect_style_with_llm and write the
        # derived rules_json / anti_ai_rules / sample_passages back. WILL
        # spend LLM tokens (one call per affected profile).

    docker exec -w /app ai-write-backend-1 python3 /tmp/seed_profile_rules.py \
        --profile-id <uuid> --apply
        # restrict to a single profile.

The sampling strategy mirrors ``detect_from_book`` but skips the Qdrant
lookup so the script also works when vectorisation is stale or partial:
we pull chunks at even intervals across the book (skipping the first
10 % which is usually TOC/preface), join 8 of them, and feed the
statistical + LLM analysers exactly like the endpoint does.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

# Work both from repo root and from inside the backend container.
for _cand in ("/app", "backend"):
    if os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.insert(0, _cand)

try:
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.db.session import async_session_factory
    from app.models.project import ReferenceBook, StyleProfile, TextChunk
    from app.services.style_detection import (
        detect_style_features,
        detect_style_with_llm,
        features_to_rules,
    )
except Exception as exc:  # pragma: no cover
    print(
        "ERROR: import failed. Run inside the backend container.\n"
        f"Detail: {exc}",
        file=sys.stderr,
    )
    raise


_TARGET_SAMPLES = 8
_MIN_COMBINED_CHARS = 1500
_MAX_LLM_CHARS = 5000


async def _book_chunks(session, book_id: str) -> list[TextChunk]:
    result = await session.execute(
        select(TextChunk)
        .where(TextChunk.book_id == book_id)
        .order_by(TextChunk.sequence_id)
    )
    return list(result.scalars().all())


def _sample_evenly(chunks: list[TextChunk]) -> list[str]:
    n = len(chunks)
    if n == 0:
        return []
    start = max(1, n // 10)
    step = max(1, (n - start) // _TARGET_SAMPLES)
    return [c.content for c in chunks[start:n:step]][:_TARGET_SAMPLES]


async def _classify(session, profile: StyleProfile) -> dict[str, Any]:
    rules = profile.rules_json if isinstance(profile.rules_json, list) else []
    has_rules = len(rules) > 0
    book_id = profile.source_book_id
    book = None
    chunks_count = 0
    if book_id:
        book = await session.get(ReferenceBook, str(book_id))
        if book is not None:
            stmt = select(TextChunk).where(TextChunk.book_id == str(book_id))
            result = await session.execute(stmt)
            chunks_count = len(list(result.scalars().all()))
    return {
        "id": str(profile.id),
        "name": profile.name,
        "source_book_id": str(book_id) if book_id else None,
        "bound_book": book.title if book else None,
        "chunks_in_book": chunks_count,
        "current_rules": len(rules),
        "current_anti_ai": len(profile.anti_ai_rules or []),
        "current_sample_passages": len(profile.sample_passages or []),
        "needs_seed": (not has_rules) and bool(book_id) and chunks_count > 0,
    }


async def _seed(session, profile: StyleProfile) -> dict[str, Any]:
    chunks = await _book_chunks(session, str(profile.source_book_id))
    samples = _sample_evenly(chunks)
    combined = "\n\n".join(samples)
    if len(combined) < _MIN_COMBINED_CHARS:
        return {
            "id": str(profile.id),
            "name": profile.name,
            "skipped": True,
            "reason": f"only {len(combined)} chars sampled (< {_MIN_COMBINED_CHARS})",
        }

    features = detect_style_features(combined)
    llm_analysis = await detect_style_with_llm(combined[:_MAX_LLM_CHARS])
    rules, anti_ai = features_to_rules(features, llm_analysis)

    if not profile.sample_passages:
        profile.sample_passages = [{"text": s[:1000]} for s in samples[:5]]
        flag_modified(profile, "sample_passages")
    profile.rules_json = rules
    flag_modified(profile, "rules_json")
    if anti_ai:
        profile.anti_ai_rules = anti_ai
        flag_modified(profile, "anti_ai_rules")

    return {
        "id": str(profile.id),
        "name": profile.name,
        "rules_added": len(rules),
        "anti_ai_added": len(anti_ai),
        "sample_passages_added": len(samples[:5]) if not profile.sample_passages else 0,
        "llm_error": llm_analysis.get("llm_error"),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async with async_session_factory() as session:
        stmt = select(StyleProfile)
        if args.profile_id:
            stmt = stmt.filter(StyleProfile.id == args.profile_id)
        result = await session.execute(stmt)
        profiles = list(result.scalars().all())

        report = []
        for p in profiles:
            report.append(await _classify(session, p))

        applied = []
        if args.apply:
            wanted_ids = {r["id"] for r in report if r["needs_seed"]}
            for p in profiles:
                if str(p.id) not in wanted_ids:
                    continue
                applied.append(await _seed(session, p))
            await session.commit()

        return {
            "total": len(report),
            "needs_seed": sum(1 for r in report if r["needs_seed"]),
            "applied_count": len(applied),
            "profiles": report,
            "applied": applied,
        }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually write rules back (calls LLM)")
    ap.add_argument("--profile-id", help="restrict to a single style profile id")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    payload = asyncio.run(_run(args))

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Scanned {payload['total']} style profile(s).")
    print(f"  needs seed (no rules, has bound book with chunks): {payload['needs_seed']}")
    for r in payload["profiles"]:
        marker = "->" if r["needs_seed"] else "  "
        book = r["bound_book"] or "(unbound)"
        print(
            f"  {marker} {r['id']}  name={r['name']!r}  book={book!r}"
            f"  chunks={r['chunks_in_book']}  rules={r['current_rules']}"
            f"  anti_ai={r['current_anti_ai']}"
        )
    if payload["applied"]:
        print("\nSeeded:")
        for a in payload["applied"]:
            if a.get("skipped"):
                print(f"  - {a['id']}  {a['name']!r}  SKIPPED: {a['reason']}")
            else:
                print(
                    f"  - {a['id']}  {a['name']!r}  +{a['rules_added']} rules,"
                    f" +{a['anti_ai_added']} anti_ai, llm_error={a.get('llm_error')!r}"
                )
    elif args.apply:
        print("Nothing to seed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
