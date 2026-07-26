"""Qdrant vector store management endpoints (v0.5)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException, Query

from app.config import settings
from app.services import qdrant_store as qs
from app.services.qdrant_store import QdrantStore

router = APIRouter(
    prefix="/api/vector-store",
    tags=["vector-store"],
)

# "chapter_summaries" / "chapter_summaries_compacted" are logical names:
# with a project_id they resolve to the per-project shard when it exists
# and is non-empty, else the legacy global collection + project filter.
COLLECTIONS = ["plots", "styles", "chapter_summaries", "chapter_summaries_compacted"]
_SHARDED = {"chapter_summaries", "chapter_summaries_compacted"}


async def _resolve_collection(
    store: QdrantStore, collection: str, project_id: str | None
) -> tuple[str, bool]:
    """Resolve a logical collection name to (actual_name, needs_project_filter)."""
    if collection in _SHARDED and project_id:
        return await qs.resolve_summary_read(
            store.client,
            project_id,
            compacted=(collection == "chapter_summaries_compacted"),
        )
    return collection, True


async def get_qdrant_store() -> QdrantStore:
    from qdrant_client import AsyncQdrantClient
    client = AsyncQdrantClient(
        host=getattr(settings, "QDRANT_HOST", "localhost"),
        port=getattr(settings, "QDRANT_PORT", 6333),
    )
    return QdrantStore(client)


@router.get("/collections")
async def list_collections():
    store = await get_qdrant_store()
    out = []
    for name in COLLECTIONS:
        try:
            out.append(await store.collection_stats(name))
        except Exception as e:
            out.append({"name": name, "error": str(e), "count": 0, "dim": 0, "distance": "", "sample_payloads": []})
    return {"collections": out}


@router.get("/{collection}/points")
async def list_points(
    collection: str,
    limit: int = Query(50, le=500),
    offset: str | None = None,
    project_id: str | None = None,
):
    if collection not in COLLECTIONS:
        raise HTTPException(404, "Unknown collection")
    store = await get_qdrant_store()
    actual, needs_filter = await _resolve_collection(store, collection, project_id)
    filter_dict = (
        {"project_id": project_id} if (project_id and needs_filter) else None
    )
    return await store.list_points(
        actual, limit=limit, offset=offset, filter_dict=filter_dict
    )


@router.delete("/{collection}/points")
async def delete_points(collection: str, body: dict = Body(...)):
    if collection not in COLLECTIONS:
        raise HTTPException(404, "Unknown collection")
    ids = body.get("point_ids", [])
    store = await get_qdrant_store()
    # Resolve the shard the browsing endpoints would have listed from, so
    # deletes target the same collection the ids came from.
    actual, _ = await _resolve_collection(store, collection, body.get("project_id"))
    await store.delete_points(actual, ids)
    return {"deleted": len(ids)}


@router.post("/{collection}/search")
async def search(collection: str, body: dict = Body(...)):
    if collection not in COLLECTIONS:
        raise HTTPException(404, "Unknown collection")
    query = (body.get("query_text") or "").strip()
    if not query:
        raise HTTPException(400, "query_text required")
    from app.services.feature_extractor import generate_embedding
    vec = await generate_embedding(query)
    store = await get_qdrant_store()
    project_id = body.get("project_id")
    actual, needs_filter = await _resolve_collection(store, collection, project_id)
    filter_dict = (
        {"project_id": project_id} if (project_id and needs_filter) else None
    )
    results = await store.search_by_vector(
        actual, vec, filter_dict=filter_dict, top_k=int(body.get("top_k", 5))
    )
    return {"results": results}


@router.post("/projects/{project_id}/migrate-shard")
async def migrate_shard(project_id: str):
    """Copy a project's chapter-summary points global → per-project shards.

    Idempotent (deterministic point ids; re-runs overwrite). Legacy points
    are not deleted — reads prefer the shard once it is non-empty.
    """
    return await qs.migrate_project_vectors(project_id)


@router.post("/projects/{project_id}/rebuild-rag")
async def rebuild_rag(project_id: str, body: dict = Body(default={})):
    from app.tasks import rebuild_rag_for_project
    task = rebuild_rag_for_project.delay(project_id, body.get("force", False))
    return {"task_id": task.id, "project_id": project_id}


@router.get("/rebuild-progress")
async def rebuild_progress(project_id: str):
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.REDIS_URL)
    try:
        raw = await client.get(f"rag_rebuild:{project_id}")
    finally:
        await client.close()
    if not raw:
        return {"status": "idle"}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)
