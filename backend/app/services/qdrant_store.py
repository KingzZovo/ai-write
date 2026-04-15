"""
Qdrant Vector Storage Manager

Manages collections and vector storage for:
- plots: Plot feature embeddings (from PlotExtractor)
- styles: Style feature embeddings (from StyleExtractor)
- chapter_summaries: Chapter summary embeddings (used by memory.py)
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

    COLLECTIONS: dict[str, dict[str, Any]] = {
        "plots": {"size": 1536, "distance": "Cosine"},
        "styles": {"size": 1536, "distance": "Cosine"},
        "chapter_summaries": {"size": 1536, "distance": "Cosine"},
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
