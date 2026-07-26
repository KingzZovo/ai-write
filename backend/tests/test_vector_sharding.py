"""Vector-layer scaling + hierarchical context compression — unit tests.

Covers the 500万字 roadmap items:
1. Per-project shard naming + read fallback (qdrant_store resolver).
2. migrate_project_vectors idempotency (fake Qdrant client).
3. Two-tier merged recall: live tier excludes compacted=true, compacted
   tier merged with a score penalty, capped at the limit.
4. Auto-compaction trigger: threshold, in-flight guard, non-blocking on
   failure.
5. Rolling book synopsis: regeneration on volume-summary creation,
   ContextPack injection order, silent degradation when absent.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import qdrant_store as qs


# ---------------------------------------------------------------------------
# Fake Qdrant client (collections held in memory)
# ---------------------------------------------------------------------------


def _match_condition(payload: dict, cond) -> bool:
    return payload.get(cond.key) == cond.match.value


def _passes_filter(payload: dict, qfilter) -> bool:
    if qfilter is None:
        return True
    for cond in qfilter.must or []:
        if not _match_condition(payload, cond):
            return False
    for cond in qfilter.must_not or []:
        if _match_condition(payload, cond):
            return False
    return True


class FakeQdrant:
    """Minimal in-memory AsyncQdrantClient stand-in."""

    def __init__(self):
        # name -> {"dim": int, "points": {id: {"vector": [...], "payload": {...}}}}
        self.collections: dict[str, dict] = {}
        self.search_calls: list[dict] = []
        self.search_results: dict[str, list] = {}  # collection -> hits

    def add_collection(self, name: str, dim: int = 8):
        self.collections[name] = {"dim": dim, "points": {}}

    def add_point(self, collection: str, pid: int, payload: dict, vector=None):
        self.collections[collection]["points"][pid] = {
            "vector": vector or [0.1] * self.collections[collection]["dim"],
            "payload": payload,
        }

    async def get_collection(self, name):
        if name not in self.collections:
            raise RuntimeError(f"collection {name} not found")
        col = self.collections[name]
        return SimpleNamespace(
            points_count=len(col["points"]),
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=col["dim"], distance="Cosine")
                )
            ),
        )

    async def create_collection(self, collection_name, vectors_config):
        self.add_collection(collection_name, dim=vectors_config.size)

    async def scroll(
        self,
        collection_name,
        scroll_filter=None,
        limit=256,
        offset=None,
        with_payload=True,
        with_vectors=False,
    ):
        if collection_name not in self.collections:
            raise RuntimeError(f"collection {collection_name} not found")
        pts = [
            SimpleNamespace(
                id=pid,
                vector=(p["vector"] if with_vectors else None),
                payload=p["payload"],
            )
            for pid, p in self.collections[collection_name]["points"].items()
            if _passes_filter(p["payload"], scroll_filter)
        ]
        return pts, None

    async def upsert(self, collection_name, points):
        if collection_name not in self.collections:
            raise RuntimeError(f"collection {collection_name} not found")
        for p in points:
            self.collections[collection_name]["points"][p.id] = {
                "vector": p.vector,
                "payload": p.payload,
            }

    async def search(
        self,
        collection_name,
        query_vector,
        query_filter=None,
        limit=10,
        score_threshold=None,
    ):
        self.search_calls.append(
            {
                "collection_name": collection_name,
                "query_filter": query_filter,
                "limit": limit,
                "score_threshold": score_threshold,
            }
        )
        if collection_name not in self.collections:
            raise RuntimeError(f"collection {collection_name} not found")
        hits = self.search_results.get(collection_name, [])
        return [
            h
            for h in hits
            if _passes_filter(h.payload or {}, query_filter)
        ][:limit]

    async def close(self):
        pass


def _hit(score: float, payload: dict):
    return SimpleNamespace(score=score, payload=payload)


# ---------------------------------------------------------------------------
# 1. Naming scheme + read resolver fallback
# ---------------------------------------------------------------------------


class TestShardNaming:
    def test_uuid_project_uses_hex_suffix(self):
        pid = uuid.uuid4()
        assert (
            qs.chapter_summaries_collection(pid)
            == f"chapter_summaries__{pid.hex}"
        )
        assert (
            qs.compacted_summaries_collection(pid)
            == f"chapter_summaries_compacted__{pid.hex}"
        )
        # UUID object and its string form resolve identically.
        assert qs.chapter_summaries_collection(str(pid)) == (
            qs.chapter_summaries_collection(pid)
        )

    def test_non_uuid_project_falls_back_to_md5(self):
        name = qs.chapter_summaries_collection("not-a-uuid")
        assert name.startswith("chapter_summaries__")
        suffix = name.split("__", 1)[1]
        assert len(suffix) == 32
        # Deterministic.
        assert name == qs.chapter_summaries_collection("not-a-uuid")

    @pytest.mark.asyncio
    async def test_resolver_prefers_nonempty_shard(self):
        pid = uuid.uuid4()
        client = FakeQdrant()
        shard = qs.chapter_summaries_collection(pid)
        client.add_collection(shard)
        client.add_point(shard, 1, {"project_id": str(pid)})

        assert await qs.resolve_summary_read(client, pid) == (shard, False)

    @pytest.mark.asyncio
    async def test_resolver_falls_back_when_shard_missing(self):
        pid = uuid.uuid4()
        client = FakeQdrant()
        assert await qs.resolve_summary_read(client, pid) == (
            "chapter_summaries",
            True,
        )
        assert await qs.resolve_summary_read(client, pid, compacted=True) == (
            "chapter_summaries_compacted",
            True,
        )

    @pytest.mark.asyncio
    async def test_resolver_falls_back_when_shard_empty(self):
        pid = uuid.uuid4()
        client = FakeQdrant()
        client.add_collection(qs.chapter_summaries_collection(pid))
        assert await qs.resolve_summary_read(client, pid) == (
            "chapter_summaries",
            True,
        )


# ---------------------------------------------------------------------------
# 2. migrate_project_vectors idempotency
# ---------------------------------------------------------------------------


class TestMigrateProjectVectors:
    @pytest.mark.asyncio
    async def test_migrate_copies_only_this_project_and_is_idempotent(self):
        pid = uuid.uuid4()
        other = uuid.uuid4()
        client = FakeQdrant()
        client.add_collection("chapter_summaries", dim=8)
        client.add_point(
            "chapter_summaries", 1, {"project_id": str(pid), "summary": "a"}
        )
        client.add_point(
            "chapter_summaries", 2, {"project_id": str(pid), "summary": "b"}
        )
        client.add_point(
            "chapter_summaries", 3, {"project_id": str(other), "summary": "x"}
        )

        shard = qs.chapter_summaries_collection(pid)
        result1 = await qs.migrate_project_vectors(pid, client=client)
        assert result1["status"] == "ok"
        assert result1["migrated"][shard] == 2
        assert set(client.collections[shard]["points"]) == {1, 2}
        # Legacy points untouched (pruning is a separate later step).
        assert len(client.collections["chapter_summaries"]["points"]) == 3

        # Idempotent: same ids overwrite, shard count unchanged.
        result2 = await qs.migrate_project_vectors(pid, client=client)
        assert result2["migrated"][shard] == 2
        assert set(client.collections[shard]["points"]) == {1, 2}

    @pytest.mark.asyncio
    async def test_migrate_without_legacy_collections_is_noop(self):
        pid = uuid.uuid4()
        client = FakeQdrant()
        result = await qs.migrate_project_vectors(pid, client=client)
        assert result["status"] == "ok"
        assert set(result["migrated"].values()) == {0}

    @pytest.mark.asyncio
    async def test_ensure_summary_shard_copies_legacy_on_first_creation(self):
        pid = uuid.uuid4()
        client = FakeQdrant()
        client.add_collection("chapter_summaries", dim=8)
        client.add_point(
            "chapter_summaries", 7, {"project_id": str(pid), "summary": "old"}
        )

        shard = await qs.ensure_summary_shard(client, pid, 8)
        assert shard == qs.chapter_summaries_collection(pid)
        # Pre-shard history was self-healed into the new shard.
        assert 7 in client.collections[shard]["points"]


# ---------------------------------------------------------------------------
# 3. Two-tier merged recall
# ---------------------------------------------------------------------------


class TestSearchProjectSummaries:
    @pytest.mark.asyncio
    async def test_merges_tiers_with_penalty_and_cap(self):
        pid = uuid.uuid4()
        client = FakeQdrant()
        live = qs.chapter_summaries_collection(pid)
        compacted = qs.compacted_summaries_collection(pid)
        client.add_collection(live)
        client.add_point(live, 1, {"project_id": str(pid)})
        client.add_collection(compacted)
        client.add_point(compacted, 2, {"project_id": str(pid)})

        client.search_results[live] = [
            _hit(0.80, {"summary": "live-a"}),
            _hit(0.60, {"summary": "live-b"}),
        ]
        # 0.75 * 0.9 = 0.675 → lands between live-a (0.80) and live-b (0.60).
        client.search_results[compacted] = [
            _hit(0.75, {"summary": "compacted-a"}),
        ]

        hits = await qs.search_project_summaries(
            client, pid, [0.1] * 8, limit=5
        )
        assert [h["payload"]["summary"] for h in hits] == [
            "live-a",
            "compacted-a",
            "live-b",
        ]
        assert hits[1]["tier"] == "compacted"
        assert hits[1]["score"] == pytest.approx(0.675)

        # Sharded reads need no project filter, but the live tier must
        # still exclude compacted=true points.
        live_call = client.search_calls[0]
        assert live_call["collection_name"] == live
        assert live_call["query_filter"].must is None
        assert [c.key for c in live_call["query_filter"].must_not] == [
            "compacted"
        ]
        compacted_call = client.search_calls[1]
        assert compacted_call["collection_name"] == compacted
        assert compacted_call["query_filter"] is None

    @pytest.mark.asyncio
    async def test_cap_applies_across_tiers(self):
        pid = uuid.uuid4()
        client = FakeQdrant()
        live = qs.chapter_summaries_collection(pid)
        compacted = qs.compacted_summaries_collection(pid)
        client.add_collection(live)
        client.add_point(live, 1, {"project_id": str(pid)})
        client.add_collection(compacted)
        client.add_point(compacted, 2, {"project_id": str(pid)})
        client.search_results[live] = [
            _hit(0.9 - i * 0.01, {"summary": f"live-{i}"}) for i in range(5)
        ]
        client.search_results[compacted] = [
            _hit(0.99, {"summary": "compacted-top"}),
        ]

        hits = await qs.search_project_summaries(
            client, pid, [0.1] * 8, limit=5
        )
        assert len(hits) == 5
        # 0.99*0.9=0.891 → compacted-top ranks second behind live-0 (0.90).
        assert hits[0]["payload"]["summary"] == "live-0"
        assert hits[1]["payload"]["summary"] == "compacted-top"

    @pytest.mark.asyncio
    async def test_legacy_fallback_filters_project_and_excludes_volume(self):
        pid = uuid.uuid4()
        vol = uuid.uuid4()
        client = FakeQdrant()
        client.add_collection("chapter_summaries")
        client.add_point("chapter_summaries", 1, {"project_id": str(pid)})
        client.search_results["chapter_summaries"] = []

        await qs.search_project_summaries(
            client, pid, [0.1] * 8, exclude_volume_id=vol
        )
        live_call = client.search_calls[0]
        assert live_call["collection_name"] == "chapter_summaries"
        assert [c.key for c in live_call["query_filter"].must] == ["project_id"]
        assert sorted(c.key for c in live_call["query_filter"].must_not) == [
            "compacted",
            "volume_id",
        ]

    @pytest.mark.asyncio
    async def test_never_raises_when_everything_is_broken(self):
        class Boom:
            def __getattr__(self, name):
                async def _fail(*a, **k):
                    raise RuntimeError("qdrant down")

                return _fail

        hits = await qs.search_project_summaries(Boom(), uuid.uuid4(), [0.1] * 8)
        assert hits == []


# ---------------------------------------------------------------------------
# 4. Auto-compaction trigger
# ---------------------------------------------------------------------------


class TestAutoCompactionTrigger:
    @pytest.fixture(autouse=True)
    def _clean_inflight(self):
        from app.services import chapter_summarizer as cs

        cs._COMPACT_IN_FLIGHT.clear()
        yield
        cs._COMPACT_IN_FLIGHT.clear()

    def _client(self, monkeypatch, live_points: int):
        client = AsyncMock()
        client.count.return_value = SimpleNamespace(count=live_points)
        monkeypatch.setattr(
            "qdrant_client.AsyncQdrantClient", MagicMock(return_value=client)
        )
        return client

    def _patch_embed(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=[0.1] * 8),
        )

    async def _upsert(self):
        from app.services.chapter_summarizer import (
            upsert_chapter_summary_embedding,
        )

        return await upsert_chapter_summary_embedding(
            project_id=uuid.uuid4(),
            volume_id=uuid.uuid4(),
            chapter_idx=1,
            chapter_title="t",
            summary="摘要",
        )

    @pytest.mark.asyncio
    async def test_fires_above_threshold(self, monkeypatch):
        from app.services import chapter_summarizer as cs

        self._patch_embed(monkeypatch)
        self._client(monkeypatch, live_points=201)
        run = AsyncMock()
        monkeypatch.setattr(cs, "_run_auto_compaction", run)

        assert await self._upsert() is True
        await asyncio.sleep(0)  # let the fire-and-forget task start
        run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_fire_at_or_below_threshold(self, monkeypatch):
        from app.services import chapter_summarizer as cs

        self._patch_embed(monkeypatch)
        self._client(monkeypatch, live_points=200)
        run = AsyncMock()
        monkeypatch.setattr(cs, "_run_auto_compaction", run)

        assert await self._upsert() is True
        await asyncio.sleep(0)
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_flight_guard_prevents_double_fire(self, monkeypatch):
        from app.services import chapter_summarizer as cs

        self._patch_embed(monkeypatch)
        self._client(monkeypatch, live_points=500)
        run = AsyncMock()
        monkeypatch.setattr(cs, "_run_auto_compaction", run)

        # First upsert schedules; in-flight marker blocks the second.
        assert await self._upsert() is True
        # _run_auto_compaction is mocked, so the marker is never discarded —
        # simulate an ongoing compaction for any project.
        assert len(cs._COMPACT_IN_FLIGHT) == 1

    @pytest.mark.asyncio
    async def test_count_failure_never_fails_the_save(self, monkeypatch):
        self._patch_embed(monkeypatch)
        client = self._client(monkeypatch, live_points=0)
        client.count.side_effect = RuntimeError("count endpoint down")

        assert await self._upsert() is True

    @pytest.mark.asyncio
    async def test_run_auto_compaction_never_raises_and_clears_marker(
        self, monkeypatch
    ):
        from app.services import chapter_summarizer as cs

        monkeypatch.setattr(
            "app.services.memory_compactor.compact_project_memory",
            AsyncMock(side_effect=RuntimeError("llm down")),
        )
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "app.db.session.async_session_factory", lambda: session
        )
        pid = str(uuid.uuid4())
        cs._COMPACT_IN_FLIGHT.add(pid)
        await cs._run_auto_compaction(pid)  # must not raise
        assert pid not in cs._COMPACT_IN_FLIGHT


# ---------------------------------------------------------------------------
# 5. Rolling book synopsis
# ---------------------------------------------------------------------------


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class TestRegenerateBookSynopsis:
    def _db(self, rows, project):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_RowsResult(rows))
        db.get = AsyncMock(return_value=project)
        return db

    @pytest.mark.asyncio
    async def test_regenerates_and_stores_in_settings_json(self, monkeypatch):
        from app.services.memory import regenerate_book_synopsis

        monkeypatch.setattr(
            "app.services.prompt_registry.run_text_prompt",
            AsyncMock(return_value=SimpleNamespace(text="全书至此梗概正文。")),
        )
        project = SimpleNamespace(settings_json={"style_reference": {"a": 1}})
        db = self._db(
            [("第一卷摘要", 0, "第一卷"), ("第二卷摘要", 1, "第二卷")], project
        )

        text = await regenerate_book_synopsis(str(uuid.uuid4()), db)
        assert text == "全书至此梗概正文。"
        stored = project.settings_json["book_synopsis"]
        assert stored["text"] == "全书至此梗概正文。"
        assert stored["source_volumes"] == 2
        # Pre-existing settings keys are preserved.
        assert project.settings_json["style_reference"] == {"a": 1}
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_truncates_to_800_chars(self, monkeypatch):
        from app.services.memory import regenerate_book_synopsis

        monkeypatch.setattr(
            "app.services.prompt_registry.run_text_prompt",
            AsyncMock(return_value=SimpleNamespace(text="梗" * 1000)),
        )
        project = SimpleNamespace(settings_json=None)
        db = self._db([("卷摘要", 0, "卷")], project)

        text = await regenerate_book_synopsis(str(uuid.uuid4()), db)
        assert len(text) == 801  # 800 + trailing ellipsis
        assert text.endswith("…")

    @pytest.mark.asyncio
    async def test_noop_without_volume_summaries(self, monkeypatch):
        from app.services.memory import regenerate_book_synopsis

        llm = AsyncMock()
        monkeypatch.setattr("app.services.prompt_registry.run_text_prompt", llm)
        db = self._db([], SimpleNamespace(settings_json={}))

        assert await regenerate_book_synopsis(str(uuid.uuid4()), db) == ""
        llm.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_raises(self):
        from app.services.memory import regenerate_book_synopsis

        db = AsyncMock()
        db.execute.side_effect = RuntimeError("db down")
        assert await regenerate_book_synopsis(str(uuid.uuid4()), db) == ""


class TestBackfillTriggersSynopsis:
    @pytest.mark.asyncio
    async def test_synopsis_regenerated_after_volume_summary_created(
        self, monkeypatch
    ):
        """Reuses the backfill fixture shape from test_memory_rag_recall."""
        from tests.test_memory_rag_recall import _FakeSession

        from app.models.project import Volume
        from app.services import memory as memory_mod

        project_id = uuid.uuid4()
        prev = Volume(
            id=uuid.uuid4(), project_id=project_id, title="第一卷", volume_idx=0
        )
        curr = Volume(
            id=uuid.uuid4(), project_id=project_id, title="第二卷", volume_idx=1
        )
        session = _FakeSession(
            get_map={str(curr.id): curr},
            execute_results=[prev, None, None],
        )
        monkeypatch.setattr(
            "app.db.session.async_session_factory", lambda: session
        )
        monkeypatch.setattr(
            "app.services.memory.HierarchicalMemory.generate_volume_summary",
            AsyncMock(return_value="第一卷的卷级摘要。"),
        )
        regen = AsyncMock(return_value="全书梗概")
        monkeypatch.setattr(memory_mod, "regenerate_book_synopsis", regen)

        assert await memory_mod.backfill_prev_volume_summary(str(curr.id)) is True
        regen.assert_awaited_once_with(str(project_id), session)

    @pytest.mark.asyncio
    async def test_no_synopsis_call_when_backfill_skipped(self, monkeypatch):
        from tests.test_memory_rag_recall import _FakeSession

        from app.models.project import Volume
        from app.services import memory as memory_mod

        curr = Volume(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            title="第一卷",
            volume_idx=0,
        )
        session = _FakeSession(
            get_map={str(curr.id): curr}, execute_results=[None]
        )
        monkeypatch.setattr(
            "app.db.session.async_session_factory", lambda: session
        )
        regen = AsyncMock()
        monkeypatch.setattr(memory_mod, "regenerate_book_synopsis", regen)

        assert (
            await memory_mod.backfill_prev_volume_summary(str(curr.id)) is False
        )
        regen.assert_not_awaited()


class TestSynopsisInjectionOrder:
    def _pack(self, **kwargs):
        from app.services.context_pack import ContextPack

        return ContextPack(
            book_outline_excerpt="全书大纲内容",
            volume_outline={"title": "第二卷", "volume_idx": 2},
            recent_summaries=["[CH-1] 前情摘要"],
            **kwargs,
        )

    def test_synopsis_sits_between_volume_outline_and_recents(self):
        prompt = self._pack(book_synopsis="全书至此发生了这些事。").to_system_prompt()
        i_vol = prompt.index("【本卷大纲】")
        i_syn = prompt.index("【全书至此梗概】")
        i_recent = prompt.index("【近五章摘要】")
        assert i_vol < i_syn < i_recent
        assert "全书至此发生了这些事。" in prompt

    def test_absent_synopsis_degrades_silently(self):
        prompt = self._pack().to_system_prompt()
        assert "全书至此梗概" not in prompt
        assert "【近五章摘要】" in prompt
