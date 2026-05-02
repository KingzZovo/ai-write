"""
Qdrant Vector Storage Manager

Manages collections and vector storage for:
- plots: Plot feature embeddings (from PlotExtractor) [deprecated v0.6, kept for back-compat]
- styles: Style feature embeddings (from StyleExtractor) [deprecated v0.6]
- chapter_summaries: Chapter summary embeddings (used by memory.py)
- style_profiles (v0.6): Structured style-profile JSON embeddings (no raw text)
- beat_sheets (v0.6): Entity-redacted plot beat embeddings
- style_samples_redacted (v0.6): Redacted raw excerpts for style-reference fallback
"""

from __future__ import annotations

import hashlib
import logging
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

from app.services.feature_extractor import generate_embedding

logger = logging.getLogger(__name__)


class QdrantStore:
    """Centralized Qdrant vector storage for plots, styles, and chapter summaries."""

    # Vector size auto-detected on first embed. Default 4096 for nvidia/nv-embed-v1.
    # Falls back to 1536 for text-embedding-3-small.
    COLLECTIONS: dict[str, dict[str, Any]] = {
        "plots": {"size": 4096, "distance": "Cosine"},
        "styles": {"size": 4096, "distance": "Cosine"},
        "chapter_summaries": {"size": 4096, "distance": "Cosine"},
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
