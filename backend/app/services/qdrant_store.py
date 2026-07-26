"""
Qdrant Vector Storage Manager

Manages collections and vector storage for:
- plots: Plot feature embeddings (from PlotExtractor) [deprecated v0.6, kept for back-compat]
- styles: Style feature embeddings (from StyleExtractor) [deprecated v0.6]
- chapter_summaries: Chapter summary embeddings (used by memory.py)
- style_profiles (v0.6): Structured style-profile JSON embeddings (no raw text)
- beat_sheets (v0.6): Entity-redacted plot beat embeddings
- style_samples_redacted (v0.6): Redacted raw excerpts for style-reference fallback

Per-project sharding (500万字 scaling)
--------------------------------------
Chapter-summary memory is sharded one collection per project:

  chapter_summaries__<project-uuid-hex>            (live tier)
  chapter_summaries_compacted__<project-uuid-hex>  (compacted tier)

The module-level helpers below are the SINGLE SOURCE OF TRUTH for the
naming scheme and the read-fallback semantics; every writer
(chapter_summarizer, rag_rebuild, memory.py) and reader (context_pack,
memory.py, api/vector_store) must resolve collection names through them.
Reads fall back to the legacy global ``chapter_summaries`` /
``chapter_summaries_compacted`` collections (with a project_id payload
filter) while a project's shard is missing or empty, so pre-migration
projects keep working. ``migrate_project_vectors`` copies a project's
points global → shard and is idempotent.

Reference-book collections (plots / styles / style_profiles / beat_sheets /
style_samples_redacted / style_samples_by_scene) deliberately stay GLOBAL:
they are shared corpora keyed by ``book_id``, not per-project data.
"""

from __future__ import annotations

import hashlib
import logging
import uuid as _uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-project sharding for chapter-summary memory (single source of truth)
# ---------------------------------------------------------------------------

LEGACY_CHAPTER_SUMMARIES = "chapter_summaries"
LEGACY_COMPACTED_SUMMARIES = "chapter_summaries_compacted"
# Tier-4 (自由检索层): per-project chapter full-text chunk shards. No legacy
# global collection exists for chunks — the tier is born sharded.
CHAPTER_CHUNKS_PREFIX = "chapter_chunks"


def project_shard_suffix(project_id: Any) -> str:
    """Stable collection-name suffix for a project.

    UUID project ids use their 32-char hex form (no dashes — Qdrant names
    stay simple); anything else falls back to md5 of the string form so the
    scheme never raises.
    """
    raw = str(project_id)
    try:
        return _uuid.UUID(raw).hex
    except (ValueError, AttributeError, TypeError):
        return hashlib.md5(raw.encode()).hexdigest()


def chapter_summaries_collection(project_id: Any) -> str:
    """Per-project live chapter-summary collection name."""
    return f"{LEGACY_CHAPTER_SUMMARIES}__{project_shard_suffix(project_id)}"


def compacted_summaries_collection(project_id: Any) -> str:
    """Per-project compacted chapter-summary collection name."""
    return f"{LEGACY_COMPACTED_SUMMARIES}__{project_shard_suffix(project_id)}"


def chapter_chunks_collection(project_id: Any) -> str:
    """Per-project chapter full-text chunk collection name (Tier 4)."""
    return f"{CHAPTER_CHUNKS_PREFIX}__{project_shard_suffix(project_id)}"


async def collection_point_count(client: Any, name: str) -> int | None:
    """Point count of a collection, or None when it does not exist."""
    try:
        info = await client.get_collection(name)
        return int(info.points_count or 0)
    except Exception:  # noqa: BLE001 — missing collection / transport error
        return None


async def resolve_summary_read(
    client: Any,
    project_id: Any,
    *,
    compacted: bool = False,
) -> tuple[str, bool]:
    """Resolve which collection to READ chapter-summary memory from.

    Returns ``(collection_name, needs_project_filter)``. The per-project
    shard wins when it exists and is non-empty; otherwise the legacy global
    collection is returned with ``needs_project_filter=True`` so callers add
    the project_id payload filter (pre-migration backward compatibility).
    """
    sharded = (
        compacted_summaries_collection(project_id)
        if compacted
        else chapter_summaries_collection(project_id)
    )
    count = await collection_point_count(client, sharded)
    if count:
        return sharded, False
    legacy = LEGACY_COMPACTED_SUMMARIES if compacted else LEGACY_CHAPTER_SUMMARIES
    return legacy, True


async def _create_collection(client: Any, name: str, dim: int) -> None:
    await client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    logger.info("Created Qdrant collection: %s (dim=%d)", name, dim)


async def ensure_summary_shard(
    client: Any,
    project_id: Any,
    dim: int,
    *,
    compacted: bool = False,
) -> str:
    """Ensure the project's summary shard exists; return its name.

    On FIRST creation the project's points are copied over from the legacy
    global collection (self-healing migration), so a shard that starts
    receiving new writes never hides the project's pre-shard history from
    recall. Idempotent and failure-tolerant: a failed legacy copy leaves the
    shard usable and a later ``migrate_project_vectors`` run repairs it.
    """
    name = (
        compacted_summaries_collection(project_id)
        if compacted
        else chapter_summaries_collection(project_id)
    )
    try:
        await client.get_collection(name)
        return name
    except Exception:  # noqa: BLE001 — shard missing, create it
        pass
    await _create_collection(client, name, dim)
    legacy = LEGACY_COMPACTED_SUMMARIES if compacted else LEGACY_CHAPTER_SUMMARIES
    try:
        copied = await _copy_project_points(client, project_id, legacy, name)
        if copied:
            logger.info(
                "Copied %d legacy points into %s on shard creation", copied, name
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Legacy copy into %s failed (run migrate later): %s", name, exc)
    return name


async def ensure_chunk_shard(client: Any, project_id: Any, dim: int) -> str:
    """Ensure the project's chapter-chunk shard exists; return its name.

    Unlike summary shards there is no legacy global collection to copy from
    (the chunk tier is born sharded), so this is a plain create-if-missing.
    """
    name = chapter_chunks_collection(project_id)
    try:
        await client.get_collection(name)
        return name
    except Exception:  # noqa: BLE001 — shard missing, create it
        pass
    await _create_collection(client, name, dim)
    return name


async def search_project_chunks(
    client: Any,
    project_id: Any,
    embedding: list[float],
    *,
    limit: int = 3,
    score_threshold: float = 0.5,
    exclude_global_idx: int | None = None,
) -> list[dict]:
    """Chapter full-text chunk recall for one project (Tier 4, 自由检索层).

    Searches the project's ``chapter_chunks__<hex>`` shard, excluding the
    chapter currently being generated (``exclude_global_idx`` payload
    filter) so the model never "recalls" the chapter it is writing.

    Returns ``[{"score", "payload"}]``; never raises (missing shard /
    transport errors degrade to an empty list).
    """
    try:
        must_not: list[Any] = []
        if exclude_global_idx is not None:
            must_not.append(
                FieldCondition(
                    key="global_idx",
                    match=MatchValue(value=int(exclude_global_idx)),
                )
            )
        qfilter = Filter(must_not=must_not) if must_not else None
        results = await client.search(
            collection_name=chapter_chunks_collection(project_id),
            query_vector=embedding,
            query_filter=qfilter,
            limit=limit,
            score_threshold=score_threshold,
        )
        return [
            {"score": float(h.score), "payload": h.payload or {}}
            for h in results
        ]
    except Exception as exc:  # noqa: BLE001 — shard missing / transport error
        logger.debug("chapter chunk search skipped: %s", exc)
        return []


async def _copy_project_points(
    client: Any,
    project_id: Any,
    source: str,
    target: str,
) -> int:
    """Copy one project's points source → target (same ids ⇒ idempotent)."""
    flt = Filter(
        must=[
            FieldCondition(
                key="project_id", match=MatchValue(value=str(project_id))
            )
        ]
    )
    copied = 0
    offset = None
    while True:
        points, offset = await client.scroll(
            collection_name=source,
            scroll_filter=flt,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break
        structs = [
            PointStruct(id=p.id, vector=p.vector, payload=p.payload or {})
            for p in points
            if p.vector is not None
        ]
        if structs:
            await client.upsert(collection_name=target, points=structs)
            copied += len(structs)
        if offset is None:
            break
    return copied


async def migrate_project_vectors(
    project_id: Any,
    client: Any = None,
) -> dict[str, Any]:
    """Copy a project's chapter-summary points global → per-project shards.

    Idempotent: point ids are deterministic, so re-runs simply overwrite.
    Legacy points are NOT deleted (reads prefer the shard once it is
    non-empty); pruning the global collections is a later, separate step.

    Invocation (orchestrator runbook):
      - API:   POST /api/vector-store/projects/{project_id}/migrate-shard
      - code:  ``await migrate_project_vectors(project_id)``
    Safe to run while the backend is live.
    """
    owns = client is None
    if client is None:
        from qdrant_client import AsyncQdrantClient

        from app.config import settings

        client = AsyncQdrantClient(
            host=getattr(settings, "QDRANT_HOST", "localhost"),
            port=getattr(settings, "QDRANT_PORT", 6333),
        )
    try:
        migrated: dict[str, int] = {}
        for legacy, sharded in (
            (LEGACY_CHAPTER_SUMMARIES, chapter_summaries_collection(project_id)),
            (LEGACY_COMPACTED_SUMMARIES, compacted_summaries_collection(project_id)),
        ):
            try:
                legacy_info = await client.get_collection(legacy)
            except Exception:  # noqa: BLE001 — no legacy tier, nothing to copy
                migrated[sharded] = 0
                continue
            try:
                await client.get_collection(sharded)
            except Exception:  # noqa: BLE001 — create shard with legacy's dim
                vectors = legacy_info.config.params.vectors
                size = getattr(vectors, "size", None)
                if size is None and isinstance(vectors, dict):
                    first = next(iter(vectors.values()), None)
                    size = getattr(first, "size", None)
                await _create_collection(client, sharded, int(size or 2048))
            migrated[sharded] = await _copy_project_points(
                client, project_id, legacy, sharded
            )
        return {"status": "ok", "project_id": str(project_id), "migrated": migrated}
    finally:
        if owns:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


async def search_project_summaries(
    client: Any,
    project_id: Any,
    embedding: list[float],
    *,
    limit: int = 5,
    score_threshold: float = 0.4,
    exclude_volume_id: Any = None,
    compacted_penalty: float = 0.9,
) -> list[dict]:
    """Two-tier chapter-summary recall for one project (W10 fix).

    Searches BOTH the live tier (excluding points already marked
    ``compacted=true``) and the compacted tier, each resolved through
    ``resolve_summary_read`` (shard preferred, legacy+filter fallback).
    Compacted hits carry a slight score penalty (they are coarser rollups);
    results merge by penalized score, capped at ``limit``.

    Returns ``[{"score", "payload", "tier"}]``; never raises.
    """
    hits: list[dict] = []

    async def _tier_search(compacted: bool) -> None:
        try:
            name, needs_filter = await resolve_summary_read(
                client, project_id, compacted=compacted
            )
            must: list[Any] = []
            must_not: list[Any] = []
            if needs_filter:
                must.append(
                    FieldCondition(
                        key="project_id", match=MatchValue(value=str(project_id))
                    )
                )
            if not compacted:
                must_not.append(
                    FieldCondition(key="compacted", match=MatchValue(value=True))
                )
            if exclude_volume_id is not None:
                must_not.append(
                    FieldCondition(
                        key="volume_id",
                        match=MatchValue(value=str(exclude_volume_id)),
                    )
                )
            qfilter = (
                Filter(must=must or None, must_not=must_not or None)
                if (must or must_not)
                else None
            )
            results = await client.search(
                collection_name=name,
                query_vector=embedding,
                query_filter=qfilter,
                limit=limit,
                score_threshold=score_threshold,
            )
            penalty = compacted_penalty if compacted else 1.0
            for h in results:
                hits.append(
                    {
                        "score": float(h.score) * penalty,
                        "payload": h.payload or {},
                        "tier": "compacted" if compacted else "live",
                    }
                )
        except Exception as exc:  # noqa: BLE001 — tier missing / transport error
            logger.debug(
                "summary tier search skipped (compacted=%s): %s", compacted, exc
            )

    await _tier_search(False)
    await _tier_search(True)
    hits.sort(key=lambda x: -x["score"])
    return hits[:limit]


class QdrantStore:
    """Centralized Qdrant vector storage for plots, styles, and chapter summaries."""

    # Vector size auto-detected on first embed. Default 4096 for nvidia/nv-embed-v1.
    # Falls back to 1536 for text-embedding-3-small.
    COLLECTIONS: dict[str, dict[str, Any]] = {
        "plots": {"size": 4096, "distance": "Cosine"},
        "styles": {"size": 4096, "distance": "Cosine"},
        # 2048 = nvidia/llama-nemotron-embed-vl-1b-v2, the configured embedding
        # model (matches the live collection). The previous 4096 declaration
        # would make a fresh install create a collection that rejects every
        # write from the generation path and rag_rebuild.
        "chapter_summaries": {"size": 2048, "distance": "Cosine"},
        # v0.6 decompile collections
        "style_profiles": {"size": 4096, "distance": "Cosine"},
        "beat_sheets": {"size": 4096, "distance": "Cosine"},
        "style_samples_redacted": {"size": 4096, "distance": "Cosine"},
    }

    _DISTANCE_MAP = {
        "Cosine": Distance.COSINE,
        "Euclid": Distance.EUCLID,
        "Dot": Distance.DOT,
    }

    def __init__(self, client: AsyncQdrantClient) -> None:
        self.client = client

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    async def ensure_collections(self) -> None:
        """Create all managed collections if they do not already exist."""
        for name, cfg in self.COLLECTIONS.items():
            try:
                await self.client.get_collection(name)
            except (UnexpectedResponse, Exception):
                try:
                    distance = self._DISTANCE_MAP.get(cfg["distance"], Distance.COSINE)
                    await self.client.create_collection(
                        collection_name=name,
                        vectors_config=VectorParams(
                            size=cfg["size"],
                            distance=distance,
                        ),
                    )
                    logger.info("Created Qdrant collection: %s", name)
                except Exception as exc:
                    logger.warning("Failed to create collection %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Plot features
    # ------------------------------------------------------------------

    async def store_plot_features(
        self,
        book_id: str,
        chunk_id: str,
        sequence_id: int,
        summary_text: str,
        embedding: list[float],
    ) -> None:
        """Store a plot feature embedding in the 'plots' collection."""
        point_id = self._deterministic_id(f"plot_{book_id}_{chunk_id}")
        try:
            await self.client.upsert(
                collection_name="plots",
                points=[
                    PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            "book_id": book_id,
                            "chunk_id": chunk_id,
                            "sequence_id": sequence_id,
                            "summary": summary_text,
                        },
                    )
                ],
            )
        except Exception as exc:
            logger.warning("Failed to store plot features for chunk %s: %s", chunk_id, exc)

    # ------------------------------------------------------------------
    # Style features
    # ------------------------------------------------------------------

    async def store_style_features(
        self,
        book_id: str,
        chunk_id: str,
        sequence_id: int,
        features_dict: dict,
        embedding: list[float],
    ) -> None:
        """Store a style feature embedding in the 'styles' collection."""
        point_id = self._deterministic_id(f"style_{book_id}_{chunk_id}")
        try:
            await self.client.upsert(
                collection_name="styles",
                points=[
                    PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            "book_id": book_id,
                            "chunk_id": chunk_id,
                            "sequence_id": sequence_id,
                            "features": features_dict,
                        },
                    )
                ],
            )
        except Exception as exc:
            logger.warning("Failed to store style features for chunk %s: %s", chunk_id, exc)

    # ------------------------------------------------------------------
    # Similarity search
    # ------------------------------------------------------------------

    async def search_similar_plots(
        self,
        query_embedding: list[float],
        book_id: str | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Search for similar plot feature vectors, optionally filtered by book."""
        query_filter = None
        if book_id is not None:
            query_filter = Filter(
                must=[
                    FieldCondition(key="book_id", match=MatchValue(value=book_id)),
                ],
            )

        try:
            results = await self.client.search(
                collection_name="plots",
                query_vector=query_embedding,
                query_filter=query_filter,
                limit=top_k,
                score_threshold=0.3,
            )
            return [
                {
                    "score": hit.score,
                    "payload": hit.payload,
                }
                for hit in results
            ]
        except Exception as exc:
            logger.warning("Plot similarity search failed: %s", exc)
            return []

    async def search_similar_styles(
        self,
        query_embedding: list[float],
        book_id: str | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Search for similar style feature vectors, optionally filtered by book."""
        query_filter = None
        if book_id is not None:
            query_filter = Filter(
                must=[
                    FieldCondition(key="book_id", match=MatchValue(value=book_id)),
                ],
            )

        try:
            results = await self.client.search(
                collection_name="styles",
                query_vector=query_embedding,
                query_filter=query_filter,
                limit=top_k,
                score_threshold=0.3,
            )
            return [
                {
                    "score": hit.score,
                    "payload": hit.payload,
                }
                for hit in results
            ]
        except Exception as exc:
            logger.warning("Style similarity search failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Sample text retrieval (for StyleAgent few-shot)
    # ------------------------------------------------------------------

    async def get_sample_texts_for_style(
        self,
        sample_block_ids: list[str],
    ) -> list[str]:
        """
        Retrieve original text content for a list of block (chunk) IDs.

        Falls back to fetching from PostgreSQL via TextChunk since Qdrant
        style payloads store features, not full text.
        """
        if not sample_block_ids:
            return []

        texts: list[str] = []
        try:
            from app.db.session import async_session_factory
            from app.models.project import TextChunk
            from sqlalchemy import select

            async with async_session_factory() as db:
                result = await db.execute(
                    select(TextChunk.content).where(
                        TextChunk.id.in_(sample_block_ids)
                    )
                )
                rows = result.scalars().all()
                texts = [row for row in rows if row]
        except Exception as exc:
            logger.warning("Failed to fetch sample texts: %s", exc)

        return texts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _deterministic_id(key: str) -> int:
        """Generate a deterministic integer point ID from a string key."""
        h = hashlib.md5(key.encode()).hexdigest()
        return int(h[:16], 16)

    # ------------------------------------------------------------------
    # v0.5 — CRUD for the /vector management panel
    # ------------------------------------------------------------------

    async def list_points(
        self,
        collection: str,
        limit: int = 50,
        offset: Any = None,
        filter_dict: dict | None = None,
    ) -> dict:
        """Scroll through points — returns {points: [{id, payload}], next_offset}."""
        qfilter = None
        if filter_dict:
            qfilter = Filter(
                must=[
                    FieldCondition(key=k, match=MatchValue(value=v))
                    for k, v in filter_dict.items()
                ]
            )
        points, next_offset = await self.client.scroll(
            collection_name=collection,
            limit=limit,
            offset=offset,
            scroll_filter=qfilter,
            with_payload=True,
            with_vectors=False,
        )
        return {
            "points": [
                {"id": p.id, "payload": p.payload or {}} for p in points
            ],
            "next_offset": next_offset,
        }

    async def delete_points(self, collection: str, point_ids: list) -> None:
        """Delete points by ID list."""
        from qdrant_client.models import PointIdsList
        await self.client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=point_ids),
        )

    async def collection_stats(self, collection: str) -> dict:
        """Return {name, count, dim, distance, sample_payloads}."""
        info = await self.client.get_collection(collection)
        sample_result, _ = await self.client.scroll(
            collection_name=collection,
            limit=3,
            with_payload=True,
            with_vectors=False,
        )
        vectors = info.config.params.vectors
        # Some Qdrant versions expose .size / .distance directly on vectors
        size = getattr(vectors, "size", None)
        distance = getattr(vectors, "distance", None)
        if size is None and isinstance(vectors, dict):
            # Named-vectors mode: fall back to first entry
            first = next(iter(vectors.values()))
            size = getattr(first, "size", 0)
            distance = getattr(first, "distance", "Cosine")
        return {
            "name": collection,
            "count": info.points_count,
            "dim": size,
            "distance": str(distance) if distance is not None else "Cosine",
            "sample_payloads": [p.payload or {} for p in sample_result],
        }

    async def search_by_vector(
        self,
        collection: str,
        query_vector: list[float],
        filter_dict: dict | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """Generic vector search (any collection, any filter)."""
        qfilter = None
        if filter_dict:
            qfilter = Filter(
                must=[
                    FieldCondition(key=k, match=MatchValue(value=v))
                    for k, v in filter_dict.items()
                ]
            )
        hits = await self.client.search(
            collection_name=collection,
            query_vector=query_vector,
            query_filter=qfilter,
            limit=top_k,
        )
        return [
            {"score": h.score, "id": h.id, "payload": h.payload or {}} for h in hits
        ]

    # ------------------------------------------------------------------
    # v0.6 — Decompile collections (style_profiles / beat_sheets /
    #         style_samples_redacted). Typed helpers wrap upsert/search.
    # ------------------------------------------------------------------

    async def store_style_profile(
        self,
        book_id: str,
        slice_id: str,
        profile_json: dict,
        embedding: list[float],
    ) -> int:
        """Store a structured style-profile card. Returns point id."""
        point_id = self._deterministic_id(f"style_profile_{book_id}_{slice_id}")
        try:
            await self.client.upsert(
                collection_name="style_profiles",
                points=[
                    PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            "book_id": book_id,
                            "slice_id": slice_id,
                            "profile": profile_json,
                        },
                    )
                ],
            )
        except Exception as exc:
            logger.warning("Failed to store style_profile for slice %s: %s", slice_id, exc)
            raise
        return point_id

    async def store_beat_sheet(
        self,
        book_id: str,
        slice_id: str,
        beat_json: dict,
        embedding: list[float],
    ) -> int:
        """Store an entity-redacted beat sheet card. Returns point id."""
        point_id = self._deterministic_id(f"beat_{book_id}_{slice_id}")
        try:
            await self.client.upsert(
                collection_name="beat_sheets",
                points=[
                    PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            "book_id": book_id,
                            "slice_id": slice_id,
                            "beat": beat_json,
                        },
                    )
                ],
            )
        except Exception as exc:
            logger.warning("Failed to store beat_sheet for slice %s: %s", slice_id, exc)
            raise
        return point_id

    async def store_style_sample_redacted(
        self,
        book_id: str,
        slice_id: str,
        redacted_text: str,
        embedding: list[float],
        entities_map: dict | None = None,
    ) -> int:
        """Store an entity-redacted raw excerpt for style reference. Returns point id."""
        point_id = self._deterministic_id(f"style_sample_{book_id}_{slice_id}")
        try:
            await self.client.upsert(
                collection_name="style_samples_redacted",
                points=[
                    PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            "book_id": book_id,
                            "slice_id": slice_id,
                            "redacted_text": redacted_text,
                            "entities_map": entities_map or {},
                        },
                    )
                ],
            )
        except Exception as exc:
            logger.warning("Failed to store style_sample_redacted for slice %s: %s", slice_id, exc)
            raise
        return point_id

    async def search_style_profiles(
        self,
        query_embedding: list[float],
        book_id: str | None = None,
        top_k: int = 3,
    ) -> list[dict]:
        return await self._filtered_search("style_profiles", query_embedding, book_id, top_k)

    async def search_beat_sheets(
        self,
        query_embedding: list[float],
        book_id: str | None = None,
        top_k: int = 2,
    ) -> list[dict]:
        return await self._filtered_search("beat_sheets", query_embedding, book_id, top_k)

    async def search_style_samples_redacted(
        self,
        query_embedding: list[float],
        book_id: str | None = None,
        top_k: int = 1,
    ) -> list[dict]:
        return await self._filtered_search("style_samples_redacted", query_embedding, book_id, top_k)

    async def _filtered_search(
        self,
        collection: str,
        query_embedding: list[float],
        book_id: str | None,
        top_k: int,
    ) -> list[dict]:
        qfilter = None
        if book_id is not None:
            qfilter = Filter(
                must=[FieldCondition(key="book_id", match=MatchValue(value=book_id))],
            )
        try:
            hits = await self.client.search(
                collection_name=collection,
                query_vector=query_embedding,
                query_filter=qfilter,
                limit=top_k,
            )
            return [
                {"score": h.score, "id": h.id, "payload": h.payload or {}} for h in hits
            ]
        except Exception as exc:
            logger.warning("Search on %s failed: %s", collection, exc)
            return []

    # PR-VECTORIZE-PASSAGES: per-passage style samples keyed by scene_type.
    SCENE_SAMPLES_COLLECTION = "style_samples_by_scene"

    async def ensure_scene_samples_collection(self, dim: int) -> None:
        """Create the style_samples_by_scene collection if absent."""
        try:
            await self.client.get_collection(self.SCENE_SAMPLES_COLLECTION)
        except Exception:
            try:
                from qdrant_client.models import Distance, VectorParams
                await self.client.create_collection(
                    collection_name=self.SCENE_SAMPLES_COLLECTION,
                    vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
                )
                logger.info("Created Qdrant collection: %s (dim=%d)", self.SCENE_SAMPLES_COLLECTION, dim)
            except Exception as exc:
                logger.warning("Failed to create %s: %s", self.SCENE_SAMPLES_COLLECTION, exc)

    async def store_scene_sample(
        self,
        profile_id: str,
        passage_idx: int,
        embedding: list[float],
        payload: dict,
    ) -> None:
        """Upsert one style sample point keyed by (profile_id, passage_idx)."""
        from qdrant_client.models import PointStruct
        pid = self._deterministic_id(f"{profile_id}:{passage_idx}")
        await self.client.upsert(
            collection_name=self.SCENE_SAMPLES_COLLECTION,
            points=[PointStruct(id=pid, vector=embedding, payload=payload)],
        )

    async def search_scene_samples(
        self,
        embedding: list[float],
        *,
        scene_type: str | None = None,
        profile_id: str | None = None,
        top_k: int = 2,
    ) -> list[dict]:
        """Search style_samples_by_scene optionally filtered by scene_type/profile_id."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        must = []
        if scene_type:
            must.append(FieldCondition(key="scene_type", match=MatchValue(value=scene_type)))
        if profile_id:
            must.append(FieldCondition(key="profile_id", match=MatchValue(value=profile_id)))
        flt = Filter(must=must) if must else None
        try:
            # qdrant-client >=1.10 deprecated .search in favor of .query_points
            res = await self.client.query_points(
                collection_name=self.SCENE_SAMPLES_COLLECTION,
                query=embedding,
                limit=top_k,
                query_filter=flt,
                with_payload=True,
            )
            points = getattr(res, "points", res) or []
            return [
                {"id": r.id, "score": r.score, "payload": r.payload}
                for r in points
            ]
        except Exception as exc:
            logger.warning("search_scene_samples failed: %s", exc)
            return []

