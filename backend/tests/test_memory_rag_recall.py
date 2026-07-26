"""Long-form memory gap fixes — unit tests.

Covers three defects:
1. Chapter summaries now reach Qdrant on the generation path
   (chapter_summarizer.upsert_chapter_summary_embedding, called by
   summarize_and_save_chapter) — failure-tolerant by contract.
2. ContextPackBuilder._search_qdrant_snippets scopes the chapter_summaries
   search to the current project via a payload filter.
3. memory.backfill_prev_volume_summary writes the previous volume's
   VolumeSummary row exactly when it is missing (cross-volume L2 bridge).
"""
from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.project import Chapter, Volume, VolumeSummary


# ---------------------------------------------------------------------------
# Defect 1 — summary -> Qdrant upsert on the generation path
# ---------------------------------------------------------------------------


def _mock_qdrant_client_cls(monkeypatch):
    """Patch qdrant_client.AsyncQdrantClient with an AsyncMock instance."""
    client = AsyncMock()
    monkeypatch.setattr(
        "qdrant_client.AsyncQdrantClient", MagicMock(return_value=client)
    )
    return client


class TestUpsertChapterSummaryEmbedding:
    @pytest.mark.asyncio
    async def test_upserts_point_with_rag_rebuild_conventions(self, monkeypatch):
        from app.services.chapter_summarizer import upsert_chapter_summary_embedding

        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=[0.1] * 8),
        )
        client = _mock_qdrant_client_cls(monkeypatch)

        project_id = uuid.uuid4()
        volume_id = uuid.uuid4()
        ok = await upsert_chapter_summary_embedding(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=7,
            chapter_title="风起",
            summary="主角在雨夜发现了旧信。",
        )

        assert ok is True
        client.upsert.assert_awaited_once()
        kwargs = client.upsert.await_args.kwargs
        assert kwargs["collection_name"] == "chapter_summaries"
        point = kwargs["points"][0]
        # Deterministic id per (volume_id, chapter_idx), identical to
        # services/rag_rebuild.py so both writers overwrite the same point.
        expected_id = int(
            hashlib.md5(f"{volume_id}_7".encode()).hexdigest()[:16], 16
        )
        assert point.id == expected_id
        assert point.payload == {
            "project_id": str(project_id),
            "volume_id": str(volume_id),
            "chapter_idx": 7,
            "chapter_title": "风起",
            "summary": "主角在雨夜发现了旧信。",
        }

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_false_without_raising(self, monkeypatch):
        from app.services.chapter_summarizer import upsert_chapter_summary_embedding

        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(side_effect=RuntimeError("embed endpoint down")),
        )
        client = _mock_qdrant_client_cls(monkeypatch)

        ok = await upsert_chapter_summary_embedding(
            project_id=uuid.uuid4(),
            volume_id=uuid.uuid4(),
            chapter_idx=1,
            chapter_title="t",
            summary="摘要",
        )
        assert ok is False
        client.upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qdrant_failure_returns_false_without_raising(self, monkeypatch):
        from app.services.chapter_summarizer import upsert_chapter_summary_embedding

        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=[0.1] * 8),
        )
        client = _mock_qdrant_client_cls(monkeypatch)
        client.upsert.side_effect = RuntimeError("qdrant down")

        ok = await upsert_chapter_summary_embedding(
            project_id=uuid.uuid4(),
            volume_id=uuid.uuid4(),
            chapter_idx=1,
            chapter_title="t",
            summary="摘要",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_skips_when_project_id_missing(self, monkeypatch):
        from app.services.chapter_summarizer import upsert_chapter_summary_embedding

        embed = AsyncMock(return_value=[0.1] * 8)
        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding", embed
        )
        ok = await upsert_chapter_summary_embedding(
            project_id=None,
            volume_id=uuid.uuid4(),
            chapter_idx=1,
            chapter_title="t",
            summary="摘要",
        )
        assert ok is False
        embed.assert_not_awaited()


class TestSummarizeAndSaveDispatchesUpsert:
    def _make_chapter_and_volume(self):
        volume = Volume(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            title="第一卷",
            volume_idx=0,
        )
        chapter = Chapter(
            id=uuid.uuid4(),
            volume_id=volume.id,
            title="第一章",
            chapter_idx=3,
            content_text="正文" * 200,
            summary=None,
        )
        return chapter, volume

    def _make_db(self, chapter, volume):
        db = AsyncMock()

        async def _get(model, pk):
            if model is Chapter:
                return chapter
            if model is Volume:
                return volume
            return None

        db.get = AsyncMock(side_effect=_get)
        return db

    @pytest.mark.asyncio
    async def test_upsert_called_after_summary_commit(self, monkeypatch):
        import app.services.chapter_summarizer as cs

        chapter, volume = self._make_chapter_and_volume()
        db = self._make_db(chapter, volume)

        monkeypatch.setattr(
            cs, "summarize_chapter_text", AsyncMock(return_value="本章摘要。")
        )
        upsert = AsyncMock(return_value=True)
        monkeypatch.setattr(cs, "upsert_chapter_summary_embedding", upsert)

        ok, summary = await cs.summarize_and_save_chapter(
            chapter_id=chapter.id, db=db
        )

        assert ok is True
        assert summary == "本章摘要。"
        assert chapter.summary == "本章摘要。"
        db.commit.assert_awaited_once()
        upsert.assert_awaited_once_with(
            project_id=volume.project_id,
            volume_id=chapter.volume_id,
            chapter_idx=chapter.chapter_idx,
            chapter_title=chapter.title,
            summary="本章摘要。",
        )

    @pytest.mark.asyncio
    async def test_save_survives_upsert_failure(self, monkeypatch):
        import app.services.chapter_summarizer as cs

        chapter, volume = self._make_chapter_and_volume()
        db = self._make_db(chapter, volume)

        monkeypatch.setattr(
            cs, "summarize_chapter_text", AsyncMock(return_value="本章摘要。")
        )
        # The real helper never raises; a False return must not undo the save.
        monkeypatch.setattr(
            cs, "upsert_chapter_summary_embedding", AsyncMock(return_value=False)
        )

        ok, summary = await cs.summarize_and_save_chapter(
            chapter_id=chapter.id, db=db
        )
        assert ok is True
        assert summary == "本章摘要。"
        assert chapter.summary == "本章摘要。"


# ---------------------------------------------------------------------------
# Defect 2 — project filter on chapter_summaries search
# ---------------------------------------------------------------------------


class TestSearchQdrantSnippetsProjectFilter:
    @pytest.mark.asyncio
    async def test_search_is_filtered_by_project_id(self, monkeypatch):
        from qdrant_client.models import FieldCondition, MatchValue

        from app.services.context_pack import ContextPack, ContextPackBuilder

        monkeypatch.setattr(
            "app.services.feature_extractor.generate_embedding",
            AsyncMock(return_value=[0.1] * 8),
        )
        hit = MagicMock()
        hit.payload = {"summary": "相关历史章节摘要。"}
        client = _mock_qdrant_client_cls(monkeypatch)
        client.search.return_value = [hit]

        builder = ContextPackBuilder(db=MagicMock())
        pack = ContextPack()
        project_id = str(uuid.uuid4())
        await builder._search_qdrant_snippets(pack, ["主角", "旧信"], project_id)

        client.search.assert_awaited_once()
        kwargs = client.search.await_args.kwargs
        assert kwargs["collection_name"] == "chapter_summaries"
        qfilter = kwargs["query_filter"]
        assert qfilter is not None
        assert qfilter.must == [
            FieldCondition(key="project_id", match=MatchValue(value=project_id))
        ]
        assert pack.rag_snippets == ["相关历史章节摘要。"]


# ---------------------------------------------------------------------------
# Defect 3 — previous-volume summary backfill
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal async-session stub for backfill_prev_volume_summary."""

    def __init__(self, get_map=None, execute_results=None):
        self._get_map = get_map or {}
        self.execute_results = list(execute_results or [])
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, model, pk):
        return self._get_map.get(str(pk))

    async def execute(self, stmt):
        return _FakeResult(self.execute_results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class TestBackfillPrevVolumeSummary:
    def _volumes(self):
        project_id = uuid.uuid4()
        prev = Volume(
            id=uuid.uuid4(), project_id=project_id, title="第一卷", volume_idx=0
        )
        curr = Volume(
            id=uuid.uuid4(), project_id=project_id, title="第二卷", volume_idx=1
        )
        return prev, curr

    def _patch_session(self, monkeypatch, session):
        monkeypatch.setattr(
            "app.db.session.async_session_factory", lambda: session
        )

    def _patch_generate(self, monkeypatch, text):
        mock = AsyncMock(return_value=text)
        monkeypatch.setattr(
            "app.services.memory.HierarchicalMemory.generate_volume_summary", mock
        )
        return mock

    @pytest.mark.asyncio
    async def test_backfills_when_prev_volume_lacks_summary(self, monkeypatch):
        from app.services.memory import backfill_prev_volume_summary

        prev, curr = self._volumes()
        session = _FakeSession(
            get_map={str(curr.id): curr},
            # prev-volume lookup, existing-row check, pre-insert recheck
            execute_results=[prev, None, None],
        )
        self._patch_session(monkeypatch, session)
        gen = self._patch_generate(monkeypatch, "第一卷的卷级摘要。")

        assert await backfill_prev_volume_summary(str(curr.id)) is True
        gen.assert_awaited_once_with(str(prev.id))
        assert len(session.added) == 1
        row = session.added[0]
        assert isinstance(row, VolumeSummary)
        assert row.volume_id == prev.id
        assert row.summary_text == "第一卷的卷级摘要。"
        assert session.committed is True

    @pytest.mark.asyncio
    async def test_noop_when_no_previous_volume(self, monkeypatch):
        from app.services.memory import backfill_prev_volume_summary

        _, curr = self._volumes()
        session = _FakeSession(
            get_map={str(curr.id): curr}, execute_results=[None]
        )
        self._patch_session(monkeypatch, session)
        gen = self._patch_generate(monkeypatch, "不应被调用")

        assert await backfill_prev_volume_summary(str(curr.id)) is False
        gen.assert_not_awaited()
        assert session.added == []

    @pytest.mark.asyncio
    async def test_noop_when_prev_summary_already_exists(self, monkeypatch):
        from app.services.memory import backfill_prev_volume_summary

        prev, curr = self._volumes()
        session = _FakeSession(
            get_map={str(curr.id): curr},
            execute_results=[prev, uuid.uuid4()],  # existing VolumeSummary.id
        )
        self._patch_session(monkeypatch, session)
        gen = self._patch_generate(monkeypatch, "不应被调用")

        assert await backfill_prev_volume_summary(str(curr.id)) is False
        gen.assert_not_awaited()
        assert session.added == []

    @pytest.mark.asyncio
    async def test_noop_when_generation_returns_empty(self, monkeypatch):
        from app.services.memory import backfill_prev_volume_summary

        prev, curr = self._volumes()
        session = _FakeSession(
            get_map={str(curr.id): curr}, execute_results=[prev, None]
        )
        self._patch_session(monkeypatch, session)
        self._patch_generate(monkeypatch, "")

        assert await backfill_prev_volume_summary(str(curr.id)) is False
        assert session.added == []
        assert session.committed is False

    @pytest.mark.asyncio
    async def test_skips_insert_when_recheck_finds_row(self, monkeypatch):
        from app.services.memory import backfill_prev_volume_summary

        prev, curr = self._volumes()
        session = _FakeSession(
            get_map={str(curr.id): curr},
            # concurrent save backfilled while the LLM call was in flight
            execute_results=[prev, None, uuid.uuid4()],
        )
        self._patch_session(monkeypatch, session)
        self._patch_generate(monkeypatch, "第一卷的卷级摘要。")

        assert await backfill_prev_volume_summary(str(curr.id)) is False
        assert session.added == []

    @pytest.mark.asyncio
    async def test_never_raises(self, monkeypatch):
        from app.services.memory import backfill_prev_volume_summary

        class _BoomSession(_FakeSession):
            async def execute(self, stmt):
                raise RuntimeError("db down")

        _, curr = self._volumes()
        self._patch_session(
            monkeypatch, _BoomSession(get_map={str(curr.id): curr})
        )
        assert await backfill_prev_volume_summary(str(curr.id)) is False
