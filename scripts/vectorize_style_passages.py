"""PR-VECTORIZE-PASSAGES: embed style_profile.sample_passages and upsert into
Qdrant style_samples_by_scene collection.

For each StyleProfile (default: 江南综合写法):
  - Read sample_passages list (each: {passage, scene_type, technique, ...})
  - Embed `passage` text via the configured embedding provider
  - Upsert into qdrant collection `style_samples_by_scene` with payload:
    { profile_id, profile_name, passage_idx, passage, scene_type, technique }
"""
import asyncio
import os
import sys
sys.path.insert(0, '/app')

from qdrant_client import AsyncQdrantClient
from app.config import settings
from app.db.session import async_session_factory
from app.models.project import StyleProfile
from app.services.qdrant_store import QdrantStore
from app.services.model_router import get_model_router_async

TARGET_PROFILE_IDS = os.environ.get(
    "TARGET_PROFILE_IDS",
    "d39058bb-a22c-4511-80f6-3649df8eca12,36fa0610-6df7-4e9a-aea9-6ea9ad1c9345"
).split(",")


async def main():
    print("=== PR-VECTORIZE-PASSAGES ===")
    qc = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    store = QdrantStore(qc)
    router = await get_model_router_async()

    async with async_session_factory() as db:
        for pid in TARGET_PROFILE_IDS:
            pid = pid.strip()
            if not pid:
                continue
            prof = await db.get(StyleProfile, pid)
            if prof is None:
                print(f"  profile {pid} not found")
                continue
            samples = prof.sample_passages or []
            print(f"  [{prof.name}] {len(samples)} sample_passages")
            if not samples:
                continue
            # Probe embedding dim using first non-empty passage
            seed = next((str((s.get("text") or s.get("passage") or "")) for s in samples if isinstance(s, dict) and ((s.get("text") or s.get("passage") or "")).strip()), "")
            if not seed:
                print("    no usable passage texts, skip")
                continue
            emb0 = await router.embed(seed)
            if not emb0:
                print("    embed failed for seed, skip")
                continue
            dim = len(emb0)
            print(f"    embedding dim: {dim}")
            await store.ensure_scene_samples_collection(dim)

            stored = 0
            for idx, s in enumerate(samples):
                if not isinstance(s, dict):
                    continue
                passage = ((s.get("text") or s.get("passage") or "")).strip()
                if not passage:
                    continue
                if idx == 0:
                    emb = emb0
                else:
                    try:
                        emb = await router.embed(passage)
                    except Exception as e:
                        print(f"    [{idx}] embed err: {e}")
                        continue
                payload = {
                    "profile_id": pid,
                    "profile_name": prof.name,
                    "passage_idx": idx,
                    "passage": passage,
                    "scene_type": (s.get("scene_type") or "").strip(),
                    "technique": (s.get("technique") or "").strip(),
                    "emotional_tone": (s.get("emotional_tone") or "").strip(),
                }
                try:
                    await store.store_scene_sample(
                        profile_id=pid,
                        passage_idx=idx,
                        embedding=emb,
                        payload=payload,
                    )
                    stored += 1
                except Exception as e:
                    print(f"    [{idx}] upsert err: {e}")
            print(f"    -> {stored} points upserted to style_samples_by_scene")

    cnt = await qc.count(collection_name="style_samples_by_scene")
    print(f"✅ final style_samples_by_scene size: {cnt.count}")
    await qc.close()

asyncio.run(main())
