"""
Celery tasks for style-related periodic processing.

- Periodic DBSCAN clustering on extracted style features
- C2/F1 (v1.9.2): whole-book style-statistics recompute (ainovel stylestat),
  incremental since W14: per-chapter stats/appearances cached in
  chapter_style_stats; only changed chapters are re-read per run.
"""

import logging

from app.tasks import celery_app

logger = logging.getLogger(__name__)

# C2/F1: public task name for the whole-book style-stats recompute. Defined
# here so the dispatch helper in app.services.style_stat can enqueue it without
# importing this module (keeps celery out of sync paths / tests).
STYLE_STATS_TASK = "style.recompute_stats"

# n-gram recency window (chapters). Must match the ``recent_window`` default of
# ``style_stat.compute_style_stats`` so the incremental aggregate reproduces
# the whole-book computation exactly.
_RECENT_WINDOW = 20


def _run_async(coro):
    """Run async function in sync Celery task context.

    v1.7 X2: delegates to the unified _run_async_safe from app.tasks.
    """
    from app.tasks import _run_async_safe
    return _run_async_safe(coro)


@celery_app.task(name="tasks.run_style_clustering")
def run_style_clustering():
    """Periodic task: run DBSCAN clustering on all books that have extracted features."""
    _run_async(_run_style_clustering_async())


async def _run_style_clustering_async():
    from sqlalchemy import select

    from app.db.session import async_session_factory
    from app.models.project import ReferenceBook, TextChunk
    from app.services.feature_extractor import StyleExtractor
    from app.services.style_clustering import cluster_style_features

    style_extractor = StyleExtractor()

    async with async_session_factory() as db:
        # Find all books in "ready" status that have extracted features
        result = await db.execute(
            select(ReferenceBook).where(ReferenceBook.status == "ready")
        )
        books = result.scalars().all()

        if not books:
            logger.info("No ready books found for style clustering")
            return

        for book in books:
            try:
                # Get all chunks with extracted style features
                chunk_result = await db.execute(
                    select(TextChunk)
                    .where(
                        TextChunk.book_id == book.id,
                        TextChunk.style_extracted == 1,
                    )
                    .order_by(TextChunk.sequence_id)
                )
                chunks = chunk_result.scalars().all()

                if len(chunks) < 5:
                    logger.info(
                        "Book %s has too few extracted chunks (%d) for clustering",
                        book.title,
                        len(chunks),
                    )
                    continue

                # Re-extract style features for clustering
                features = []
                block_ids = []
                for chunk in chunks:
                    feat = style_extractor.extract(chunk.content)
                    features.append(feat.to_dict())
                    block_ids.append(str(chunk.id))

                # Run clustering
                profiles = cluster_style_features(
                    features, block_ids, method="dbscan"
                )

                # Store clustering results in book metadata
                metadata = book.metadata_json or {}
                metadata["style_clusters"] = [
                    {
                        "name": p.name,
                        "vocab_whitelist": p.vocab_whitelist[:10],
                        "sentence_ratio": p.sentence_ratio,
                        "dialogue_ratio": p.dialogue_ratio,
                        "pov_type": p.pov_type,
                        "sample_count": len(p.sample_block_ids),
                    }
                    for p in profiles
                ]
                book.metadata_json = metadata
                await db.commit()

                logger.info(
                    "Style clustering for book %s: %d clusters found",
                    book.title,
                    len(profiles),
                )

            except Exception as exc:
                logger.warning(
                    "Style clustering failed for book %s: %s", book.title, exc
                )
                continue


# ---------------------------------------------------------------------------
# C2 / F1: whole-book style statistics recompute (ainovel stylestat).
# ---------------------------------------------------------------------------


@celery_app.task(
    name=STYLE_STATS_TASK,
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
)
def recompute_style_stats(
    self, project_id: str, caller: str = "unknown", full: bool = False
) -> dict:
    """Recompute and upsert whole-book style statistics for one project.

    Deterministic (zero LLM) and incremental (W14): per-chapter style facts
    and appearance counts are cached in ``chapter_style_stats`` keyed by the
    chapter's ``updated_at``; only stale chapters (typically the one just
    accepted) get their content re-read and recomputed, then the whole-book
    ``style_stats`` row and the ``character_appearances`` roster are refreshed
    from the cached rows (cheap aggregate, idempotent).

    ``full=True`` forces every chapter's row to be recomputed -- the manual
    repair / backfill entry point for projects that predate the cache.
    """
    from app.tasks import _run_async_safe

    return _run_async_safe(
        _recompute_style_stats_async(str(project_id), str(caller), full=bool(full))
    )


def _stale_chapters(
    meta: list[tuple],
    existing: dict,
    *,
    full: bool = False,
) -> list[tuple]:
    """Select chapters whose per-chapter stats row must be recomputed.

    ``meta`` is ``[(chapter_id, global_idx, updated_at), ...]`` for every
    chapter of the project; ``existing`` maps ``chapter_id`` to
    ``(source_updated_at, global_idx)`` from chapter_style_stats. A chapter is
    stale when it has no row, its content changed (``updated_at`` mismatch),
    or its global index moved (volume reshuffle). ``full=True`` marks all
    chapters stale (repair/backfill).
    """
    stale = []
    for chapter_id, global_idx, updated_at in meta:
        row = existing.get(chapter_id)
        if full or row is None or row[0] != updated_at or row[1] != global_idx:
            stale.append((chapter_id, global_idx, updated_at))
    return stale


async def _recompute_style_stats_async(
    project_id: str, caller: str, full: bool = False
) -> dict:
    from sqlalchemy import delete, select
    from sqlalchemy.dialects.postgresql import insert

    from app.db.session import async_session_factory
    from app.models.project import (
        Chapter,
        ChapterStyleStat,
        Character,
        Location,
        Organization,
        StyleStat,
        Volume,
    )
    from app.services.character_roster import (
        count_appearances,
        load_alias_map,
        rebuild_roster,
    )
    from app.services.foreshadow_lifecycle import get_volume_first_global_idx
    from app.services.style_stat import (
        aggregate_style_stats,
        compute_chapter_style_stats,
    )

    async with async_session_factory() as db:
        # 1. Chapter metadata only (id/global_idx/updated_at -- no content).
        vol_rows = (
            await db.execute(
                select(Volume.id, Volume.volume_idx)
                .where(Volume.project_id == project_id)
                .order_by(Volume.volume_idx)
            )
        ).all()
        meta: list[tuple] = []
        for vol_id, vol_idx in vol_rows:
            ch_rows = (
                await db.execute(
                    select(
                        Chapter.id,
                        Chapter.chapter_idx,
                        Chapter.global_idx,
                        Chapter.updated_at,
                    )
                    .where(Chapter.volume_id == vol_id)
                    .order_by(Chapter.chapter_idx)
                )
            ).all()
            base = None  # legacy fallback for NULL global_idx (pre-a1001915)
            for cid, ch_idx, gidx, updated_at in ch_rows:
                if gidx is None:
                    if base is None:
                        base = await get_volume_first_global_idx(
                            db, project_id, vol_idx
                        )
                    gidx = base + max(0, int(ch_idx))
                meta.append((cid, int(gidx), updated_at))

        if not meta:
            logger.info(
                "style stats: no chapters (project=%s caller=%s)",
                project_id, caller,
            )
            return {"project_id": project_id, "chapter_count": 0, "skipped": True}

        # 2. Staleness: compare against cached per-chapter rows.
        existing = {
            cid: (src_updated, gidx)
            for cid, src_updated, gidx in (
                await db.execute(
                    select(
                        ChapterStyleStat.chapter_id,
                        ChapterStyleStat.source_updated_at,
                        ChapterStyleStat.global_idx,
                    ).where(ChapterStyleStat.project_id == project_id)
                )
            ).all()
        }
        stale = _stale_chapters(meta, existing, full=full)

        # Entity names (PG read-only projections): n-gram stop-list + roster
        # tokens. Alias folding (characters.profile_json.aliases) preserved.
        names: set[str] = set()
        for model in (Character, Location, Organization):
            rows = (
                await db.execute(
                    select(model.name).where(model.project_id == project_id)
                )
            ).all()
            names.update(n for (n,) in rows if n)
        alias_map = await load_alias_map(db, project_id)

        # 3. Recompute stale chapters only (content loaded just for these).
        if stale:
            stale_ids = [cid for cid, _g, _u in stale]
            content_by_id = dict(
                (
                    await db.execute(
                        select(Chapter.id, Chapter.content_text).where(
                            Chapter.id.in_(stale_ids)
                        )
                    )
                ).all()
            )
            for cid, gidx, updated_at in stale:
                text = content_by_id.get(cid)
                ch_stats = compute_chapter_style_stats(text or "")
                if ch_stats is None:
                    # Empty/erased chapter: drop its cached contribution.
                    await db.execute(
                        delete(ChapterStyleStat).where(
                            ChapterStyleStat.chapter_id == cid
                        )
                    )
                    continue
                appearances = count_appearances(text, names, alias_map)
                await db.execute(
                    insert(ChapterStyleStat)
                    .values(
                        project_id=project_id,
                        chapter_id=cid,
                        global_idx=gidx,
                        stats_json=ch_stats,
                        appearances_json=appearances,
                        source_updated_at=updated_at,
                    )
                    .on_conflict_do_update(
                        index_elements=["chapter_id"],
                        set_={
                            "global_idx": gidx,
                            "stats_json": ch_stats,
                            "appearances_json": appearances,
                            "source_updated_at": updated_at,
                        },
                    )
                )

        # 4. Aggregate from cached rows (no content_text except the recent
        # n-gram window, which is O(window), not O(book)).
        cached = (
            await db.execute(
                select(
                    ChapterStyleStat.chapter_id,
                    ChapterStyleStat.global_idx,
                    ChapterStyleStat.stats_json,
                    ChapterStyleStat.appearances_json,
                ).where(ChapterStyleStat.project_id == project_id)
            )
        ).all()
        if not cached:
            logger.info(
                "style stats: no written chapters (project=%s caller=%s)",
                project_id, caller,
            )
            await db.commit()
            return {"project_id": project_id, "chapter_count": 0, "skipped": True}

        cached_sorted = sorted(cached, key=lambda r: r[1])
        recent = cached_sorted[-_RECENT_WINDOW:]
        recent_content = dict(
            (
                await db.execute(
                    select(Chapter.id, Chapter.content_text).where(
                        Chapter.id.in_([cid for cid, _g, _s, _a in recent])
                    )
                )
            ).all()
        )
        recent_texts = [
            recent_content.get(cid) or "" for cid, _g, _s, _a in recent
        ]
        stats = aggregate_style_stats(
            [(gidx, sj) for _cid, gidx, sj, _aj in cached_sorted],
            recent_texts,
            names,
        )

        # C3/F4: rebuild the secondary-cast roster from the cached per-chapter
        # appearance rows (idempotent absolute values). Best-effort; never
        # blocks stats.
        try:
            await rebuild_roster(
                db,
                project_id,
                [(gidx, aj or {}) for _cid, gidx, _sj, aj in cached_sorted],
                valid_names=names,
            )
        except Exception as roster_err:
            logger.warning(
                "roster update failed (project=%s): %s", project_id, roster_err
            )

        await db.execute(
            insert(StyleStat)
            .values(
                project_id=project_id,
                stats_json=stats,
                chapter_count=stats.get("chapter_count", 0),
            )
            .on_conflict_do_update(
                index_elements=["project_id"],
                set_={
                    "stats_json": stats,
                    "chapter_count": stats.get("chapter_count", 0),
                },
            )
        )
        await db.commit()

    logger.info(
        "style stats recomputed (project=%s caller=%s chapters=%d stale=%d phrases=%d)",
        project_id, caller, stats.get("chapter_count", 0), len(stale),
        len(stats.get("top_phrases", [])),
    )
    return {
        "project_id": project_id,
        "chapter_count": stats.get("chapter_count", 0),
        "recomputed_chapters": len(stale),
    }
