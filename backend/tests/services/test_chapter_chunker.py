"""Tier-4 chapter chunking (自由检索层) — chunker, upsert, recall, gates.

- chunk boundaries: short chapter → 1 chunk; long chapter → multiple chunks,
  each <= max, sentence/paragraph safe
- deterministic point ids (md5 of volume_id_chapteridx_seq)
- upsert payload shape + stale-tail deletion filter (re-save fewer chunks)
- CHAPTER_CHUNKING_ENABLED / CHAPTER_CHUNK_RECALL_ENABLED gates
- qdrant_store.search_project_chunks: current-chapter exclusion, never raises
- ContextPack chunk recall wiring (render source + 300-char snippet cap)
"""
from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chapter_chunker import (
    CHUNK_MAX_CHARS,
    chunk_chapter_text,
    chunk_point_id,
    upsert_chapter_chunks,
)

_SENTENCE_ENDERS = "。！？…!?"

_PARA = "炉膛里的柴火噼啪炸开，火星子溅到青砖地上又灭了。" * 16  # 384 chars


class TestChunkChapterText:
    def test_short_chapter_single_chunk(self):
        text = "夜里下了雨。\n林岚推门进来。"
        chunks = chunk_chapter_text(text)
        assert chunks == ["夜里下了雨。\n林岚推门进来。"]

    def test_long_chapter_multiple_bounded_chunks(self):
        text = "\n".join([_PARA] * 5)  # 5 paragraphs, ~1920 chars
        chunks = chunk_chapter_text(text)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= CHUNK_MAX_CHARS
            # Sentence-boundary safe: every chunk ends on a sentence ender.
            assert c[-1] in _SENTENCE_ENDERS
        # Lossless: rejoining chunks reproduces the paragraph stream.
        assert "".join("".join(c.split("\n")) for c in chunks) == "".join(
            text.split("\n")
        )

    def test_overlong_paragraph_split_at_sentence_boundaries(self):
        text = _PARA * 3  # single 1152-char paragraph, no newlines
        chunks = chunk_chapter_text(text)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= CHUNK_MAX_CHARS
            assert c[-1] in _SENTENCE_ENDERS

    def test_empty_text(self):
        assert chunk_chapter_text("") == []
        assert chunk_chapter_text("  \n \n ") == []

    def test_deterministic(self):
        text = "\n".join([_PARA] * 3)
        assert chunk_chapter_text(text) == chunk_chapter_text(text)


class TestPointIdDeterminism:
    def test_matches_md5_convention(self):
        vid = uuid.uuid4()
        expected = int(
            hashlib.md5(f"{vid}_7_2".encode()).hexdigest()[:16], 16
        )
        assert chunk_point_id(vid, 7, 2) == expected
        assert chunk_point_id(vid, 7, 2) == chunk_point_id(vid, 7, 2)
        assert chunk_point_id(vid, 7, 3) != chunk_point_id(vid, 7, 2)


def _mock_qdrant_client_cls(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(
        "qdrant_client.AsyncQdrantClient", MagicMock(return_value=client)
    )
    return client


class TestUpsertChapterChunks:
    @pytest.mark.asyncio
    async def test_upserts_points_and_deletes_stale_tail(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=[0.1] * 8),
        )
        client = _mock_qdrant_client_cls(monkeypatch)

        project_id = uuid.uuid4()
        volume_id = uuid.uuid4()
        text = "\n".join([_PARA] * 4)
        expected_chunks = chunk_chapter_text(text)
        assert len(expected_chunks) > 1

        n = await upsert_chapter_chunks(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=7,
            global_idx=27,
            content_text=text,
        )

        assert n == len(expected_chunks)
        kwargs = client.upsert.await_args.kwargs
        from app.services.qdrant_store import chapter_chunks_collection

        assert kwargs["collection_name"] == chapter_chunks_collection(project_id)
        points = kwargs["points"]
        for seq, point in enumerate(points):
            assert point.id == chunk_point_id(volume_id, 7, seq)
            assert point.payload == {
                "project_id": str(project_id),
                "volume_id": str(volume_id),
                "chapter_idx": 7,
                "global_idx": 27,
                "chunk_seq": seq,
                "text": expected_chunks[seq],
            }

        # Stale-tail cleanup: re-saving a shorter chapter must delete points
        # with chunk_seq >= new count for THIS chapter (global_idx filter).
        client.delete.assert_awaited_once()
        selector = client.delete.await_args.kwargs["points_selector"]
        conditions = {c.key: c for c in selector.filter.must}
        assert conditions["chunk_seq"].range.gte == len(expected_chunks)
        assert conditions["global_idx"].match.value == 27

    @pytest.mark.asyncio
    async def test_chunking_gate_off_is_noop(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "CHAPTER_CHUNKING_ENABLED", False)
        client_cls = MagicMock()
        monkeypatch.setattr("qdrant_client.AsyncQdrantClient", client_cls)

        n = await upsert_chapter_chunks(
            project_id=uuid.uuid4(),
            volume_id=uuid.uuid4(),
            chapter_idx=1,
            global_idx=1,
            content_text=_PARA,
        )
        assert n == 0
        client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_ids_or_text_skip(self):
        assert (
            await upsert_chapter_chunks(
                project_id=None,
                volume_id=uuid.uuid4(),
                chapter_idx=1,
                global_idx=1,
                content_text=_PARA,
            )
            == 0
        )
        assert (
            await upsert_chapter_chunks(
                project_id=uuid.uuid4(),
                volume_id=uuid.uuid4(),
                chapter_idx=1,
                global_idx=1,
                content_text="",
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_embedding_failure_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=None),
        )
        client_cls = MagicMock()
        monkeypatch.setattr("qdrant_client.AsyncQdrantClient", client_cls)
        n = await upsert_chapter_chunks(
            project_id=uuid.uuid4(),
            volume_id=uuid.uuid4(),
            chapter_idx=1,
            global_idx=1,
            content_text=_PARA,
        )
        assert n == 0
        client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_qdrant_failure_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=[0.1] * 8),
        )
        client = _mock_qdrant_client_cls(monkeypatch)
        client.upsert.side_effect = RuntimeError("qdrant down")
        n = await upsert_chapter_chunks(
            project_id=uuid.uuid4(),
            volume_id=uuid.uuid4(),
            chapter_idx=1,
            global_idx=1,
            content_text=_PARA,
        )
        assert n == 0


class TestSearchProjectChunks:
    @pytest.mark.asyncio
    async def test_excludes_current_chapter_via_filter(self):
        from app.services.qdrant_store import (
            chapter_chunks_collection,
            search_project_chunks,
        )

        client = AsyncMock()
        hit = MagicMock(score=0.8, payload={"global_idx": 4, "text": "原文"})
        client.search.return_value = [hit]
        project_id = uuid.uuid4()

        out = await search_project_chunks(
            client, project_id, [0.1] * 8, limit=3, exclude_global_idx=12
        )

        assert out == [{"score": 0.8, "payload": {"global_idx": 4, "text": "原文"}}]
        kwargs = client.search.await_args.kwargs
        assert kwargs["collection_name"] == chapter_chunks_collection(project_id)
        assert kwargs["limit"] == 3
        must_not = kwargs["query_filter"].must_not
        assert len(must_not) == 1
        assert must_not[0].key == "global_idx"
        assert must_not[0].match.value == 12

    @pytest.mark.asyncio
    async def test_missing_shard_returns_empty_never_raises(self):
        from app.services.qdrant_store import search_project_chunks

        client = AsyncMock()
        client.search.side_effect = RuntimeError("collection not found")
        assert await search_project_chunks(client, uuid.uuid4(), [0.1] * 8) == []


class TestContextPackChunkRecall:
    def _builder_env(self, monkeypatch, chunk_hits):
        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=[0.1] * 8),
        )
        _mock_qdrant_client_cls(monkeypatch)
        monkeypatch.setattr(
            "app.services.qdrant_store.search_project_summaries",
            AsyncMock(return_value=[]),
        )
        chunk_search = AsyncMock(return_value=chunk_hits)
        monkeypatch.setattr(
            "app.services.qdrant_store.search_project_chunks", chunk_search
        )
        return chunk_search

    @pytest.mark.asyncio
    async def test_recall_populates_snippets_and_excludes_current(
        self, monkeypatch
    ):
        from app.services.context_pack import ContextPack, ContextPackBuilder

        chunk_search = self._builder_env(
            monkeypatch,
            [
                {"score": 0.8, "payload": {"global_idx": 4, "text": "雨" * 500}},
                {"score": 0.6, "payload": {"global_idx": 9, "text": "巷子里"}},
            ],
        )
        builder = ContextPackBuilder(db=AsyncMock())
        pack = ContextPack()
        pack.global_chapter_idx = 12

        await builder._search_qdrant_snippets(pack, ["林岚"], "pid-1")

        chunk_search.assert_awaited_once()
        kwargs = chunk_search.await_args.kwargs
        assert kwargs["exclude_global_idx"] == 12
        assert kwargs["limit"] == 3
        assert kwargs["score_threshold"] == 0.5
        # Snippets capped at 300 chars, labeled with source chapter.
        assert pack.chunk_recall == [
            "[CH-4] " + "雨" * 300,
            "[CH-9] 巷子里",
        ]

    @pytest.mark.asyncio
    async def test_recall_gate_off_skips_search(self, monkeypatch):
        from app.config import settings
        from app.services.context_pack import ContextPack, ContextPackBuilder

        chunk_search = self._builder_env(monkeypatch, [])
        monkeypatch.setattr(settings, "CHAPTER_CHUNK_RECALL_ENABLED", False)
        builder = ContextPackBuilder(db=AsyncMock())
        pack = ContextPack()
        pack.global_chapter_idx = 12

        await builder._search_qdrant_snippets(pack, ["林岚"], "pid-1")

        chunk_search.assert_not_awaited()
        assert pack.chunk_recall == []
