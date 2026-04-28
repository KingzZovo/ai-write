"""v1.7.4 P0-2 backfill: write chapter.summary for already-generated chapters.

Usage (inside backend container):
    PYTHONPATH=/app python /app/scripts/v174_p02_backfill_summaries.py [vol_id] [--force-dirty]

Flags:
    --force-dirty   also overwrite chapters whose existing summary looks dirty
                    (markdown json fence, raw json wrapper, etc.) — these were
                    written by an upstream caller without prompt-spec cleaning
                    and will pollute ContextPack.recent_summaries.
"""
import asyncio
import sys
from sqlalchemy import select
from app.db.session import async_session_factory
from app.models.project import Chapter
from app.services.chapter_summarizer import summarize_and_save_chapter

DEFAULT_VOL_ID = "eeeb4a68-3eab-4057-a102-fbe460ca2bb7"

DIRTY_MARKERS = ("```json", "```", '"summary"', "{\n")


def looks_dirty(summary: str) -> bool:
    if not summary:
        return False
    s = summary.lstrip()[:80]
    return any(m in s for m in DIRTY_MARKERS)


async def main(vol_id: str, force_dirty: bool) -> int:
    print(f"backfill start vol={vol_id} force_dirty={force_dirty}", flush=True)
    async with async_session_factory() as db:
        rows = await db.execute(
            select(Chapter).where(Chapter.volume_id == vol_id).order_by(Chapter.chapter_idx)
        )
        chapters = rows.scalars().all()
    print(f"found {len(chapters)} chapters", flush=True)
    written = 0
    skipped = 0
    failed = 0
    for ch in chapters:
        clen = len(ch.content_text or "")
        existing = (ch.summary or "").strip()
        if clen < 200:
            print(f"  ch{ch.chapter_idx} {ch.title[:18]!r}: SKIP empty/short clen={clen}", flush=True)
            skipped += 1
            continue
        is_dirty = looks_dirty(existing)
        if existing and not (force_dirty and is_dirty):
            tag = "DIRTY-but-skip" if is_dirty else "clean"
            print(f"  ch{ch.chapter_idx} {ch.title[:18]!r}: SKIP has_summary({tag}) len={len(existing)}", flush=True)
            skipped += 1
            continue
        action = "REWRITE-dirty" if (existing and is_dirty) else "summarize"
        print(f"  ch{ch.chapter_idx} {ch.title[:18]!r}: {action} clen={clen} ...", flush=True)
        try:
            async with async_session_factory() as one_db:
                ok, summ = await summarize_and_save_chapter(
                    chapter_id=ch.id, db=one_db, overwrite=True,
                )
            print(f"    -> ok={ok} summary_len={len(summ)} preview={summ[:60]!r}", flush=True)
            if ok:
                written += 1
            else:
                failed += 1
        except Exception as e:
            print(f"    -> FAIL {type(e).__name__}: {e}", flush=True)
            failed += 1
    print(f"\nbackfill done: written={written} skipped={skipped} failed={failed}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    vol = args[0] if args else DEFAULT_VOL_ID
    sys.exit(asyncio.run(main(vol, force_dirty="--force-dirty" in flags)))
