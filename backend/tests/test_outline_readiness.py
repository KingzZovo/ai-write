from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.generate import GenerateChapterRequest, generate_chapter
from app.services.outline_readiness import (
    build_outline_readiness_report,
    has_meaningful_outline_content,
)


PID = "7016637d-3c9b-40f7-83df-6a0de90ec5bc"


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, *, objects=None, execute_results=None):
        self.objects = objects or {}
        self.execute_results = list(execute_results or [])

    async def get(self, model, key):
        return self.objects.get((model.__name__, str(key)))

    async def execute(self, *_args, **_kwargs):
        if self.execute_results:
            return self.execute_results.pop(0)
        return _FakeResult([])


async def _collect_sse_text(response) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode("utf-8"))
        else:
            chunks.append(str(chunk))
    return "".join(chunks)


def test_has_meaningful_outline_content_rejects_empty_shells():
    assert not has_meaningful_outline_content(None)
    assert not has_meaningful_outline_content({})
    assert not has_meaningful_outline_content({"raw_text": ""})
    assert not has_meaningful_outline_content({"chapter_summaries": []})


def test_has_meaningful_outline_content_accepts_structured_outline():
    assert has_meaningful_outline_content({"raw_text": "全书大纲"})
    assert has_meaningful_outline_content({"volume_idx": 1, "title": "潮起"})
    assert has_meaningful_outline_content({"summary": "本章过程性大纲"})


@pytest.mark.asyncio
async def test_readiness_missing_all_layers_reports_book_volume_chapter():
    db = _FakeDB(execute_results=[_FakeResult([]), _FakeResult([])])

    report = await build_outline_readiness_report(
        db,
        project_id=PID,
        chapter_id="missing-chapter",
    )

    assert report.ready is False
    assert report.missing_layers == ["book", "volume", "chapter"]
    assert report.layers["book"].ready is False
    assert report.layers["volume"].ready is False
    assert report.layers["chapter"].ready is False


@pytest.mark.asyncio
async def test_readiness_requires_matching_volume_idx():
    from app.models.project import Chapter, Volume

    chapter = SimpleNamespace(
        id="chapter-1",
        volume_id="volume-1",
        chapter_idx=3,
        outline_json={"summary": "章节大纲"},
    )
    volume = SimpleNamespace(id="volume-1", volume_idx=2, title="第二卷")
    book_outline = SimpleNamespace(id="book-1", content_json={"raw_text": "全书大纲"})
    wrong_volume_outline = SimpleNamespace(
        id="volume-outline-1",
        content_json={"volume_idx": 1, "title": "第一卷", "chapter_summaries": [{"summary": "x"}]},
    )
    db = _FakeDB(
        objects={
            ("Chapter", "chapter-1"): chapter,
            ("Volume", "volume-1"): volume,
        },
        execute_results=[
            _FakeResult([book_outline]),
            _FakeResult([wrong_volume_outline]),
        ],
    )

    report = await build_outline_readiness_report(
        db,
        project_id=PID,
        chapter_id="chapter-1",
    )

    assert report.ready is False
    assert report.missing_layers == ["volume"]
    assert report.layers["chapter"].ready is True


@pytest.mark.asyncio
async def test_readiness_complete_chain_allows_prose_generation():
    chapter = SimpleNamespace(
        id="chapter-1",
        volume_id="volume-1",
        chapter_idx=3,
        outline_json={"summary": "章节大纲"},
    )
    volume = SimpleNamespace(id="volume-1", volume_idx=2, title="第二卷")
    book_outline = SimpleNamespace(id="book-1", content_json={"raw_text": "全书大纲"})
    volume_outline = SimpleNamespace(
        id="volume-outline-2",
        content_json={"volume_idx": 2, "title": "第二卷", "chapter_summaries": [{"summary": "x"}]},
    )
    db = _FakeDB(
        objects={
            ("Chapter", "chapter-1"): chapter,
            ("Volume", "volume-1"): volume,
        },
        execute_results=[
            _FakeResult([book_outline]),
            _FakeResult([volume_outline]),
        ],
    )

    report = await build_outline_readiness_report(
        db,
        project_id=PID,
        chapter_id="chapter-1",
    )

    assert report.ready is True
    assert report.missing_layers == []


@pytest.mark.asyncio
async def test_generate_chapter_blocks_before_generator_when_outline_chain_missing():
    from app.models.project import Chapter, Outline, Project, Volume

    project = SimpleNamespace(id=PID, settings_json={}, target_word_count=3000000)
    chapter = SimpleNamespace(
        id="chapter-1",
        volume_id="volume-1",
        chapter_idx=1,
        outline_json={},
        content_text="",
        target_word_count=1200,
        summary="",
        status="draft",
    )
    db = _FakeDB(
        objects={
            ("Project", PID): project,
            ("Chapter", "chapter-1"): chapter,
        },
        execute_results=[
            _FakeResult([]),  # world rules
            _FakeResult([]),  # existing book outline summary load
            _FakeResult([]),  # previous chapter
            _FakeResult([]),  # next chapter
        ],
    )

    blocked_report = SimpleNamespace(
        ready=False,
        missing_layers=["book", "volume", "chapter"],
        to_dict=lambda: {
            "ready": False,
            "missing_layers": ["book", "volume", "chapter"],
        },
        block_message=lambda: "缺少：全书大纲、当前卷大纲、本章大纲",
    )

    with patch(
        "app.api.generate.build_outline_readiness_report",
        new=AsyncMock(return_value=blocked_report),
    ), patch("app.api.generate.ChapterGenerator") as chapter_generator_cls:
        response = await generate_chapter(
            GenerateChapterRequest(project_id=PID, chapter_id="chapter-1"),
            db=db,
        )
        text = await _collect_sse_text(response)

    assert "outline_chain_incomplete" in text
    assert "book" in text and "volume" in text and "chapter" in text
    chapter_generator_cls.assert_not_called()


@pytest.mark.asyncio
async def test_scene_orchestrator_blocks_before_context_pack_when_outline_chain_missing():
    from app.services.scene_orchestrator import SceneOrchestrator

    blocked_report = SimpleNamespace(
        ready=False,
        missing_layers=["book", "volume", "chapter"],
        block_message=lambda: "缺少：全书大纲、当前卷大纲、本章大纲",
    )

    with patch(
        "app.services.scene_orchestrator.build_outline_readiness_report",
        new=AsyncMock(return_value=blocked_report),
    ), patch("app.services.scene_orchestrator.ContextPackBuilder") as pack_builder_cls:
        with pytest.raises(RuntimeError) as exc:
            async for _chunk in SceneOrchestrator().orchestrate_chapter_stream(
                project_id=PID,
                volume_id="volume-1",
                chapter_idx=1,
                db=object(),
                chapter_id="chapter-1",
            ):
                pass

    assert "outline_chain_incomplete" in str(exc.value)
    pack_builder_cls.assert_not_called()


@pytest.mark.asyncio
async def test_start_async_generation_blocks_chapter_task_before_enqueue():
    from app.api.generate import AsyncGenerateRequest, start_async_generation

    project = SimpleNamespace(id=PID, settings_json={})
    db = _FakeDB(objects={("Project", PID): project})
    blocked_report = SimpleNamespace(
        ready=False,
        missing_layers=["book", "volume", "chapter"],
        to_dict=lambda: {
            "ready": False,
            "missing_layers": ["book", "volume", "chapter"],
        },
        block_message=lambda: "缺少：全书大纲、当前卷大纲、本章大纲",
    )

    with patch(
        "app.api.generate.build_outline_readiness_report",
        new=AsyncMock(return_value=blocked_report),
    ):
        with pytest.raises(Exception) as exc:
            await start_async_generation(
                AsyncGenerateRequest(
                    project_id=PID,
                    task_type="chapter",
                    chapter_id="chapter-1",
                ),
                db=db,
            )

    assert getattr(exc.value, "status_code", None) == 422
    assert "outline_chain_incomplete" in str(getattr(exc.value, "detail", ""))


@pytest.mark.asyncio
async def test_start_async_generation_blocks_volume_outline_without_book():
    from app.api.generate import AsyncGenerateRequest, start_async_generation

    project = SimpleNamespace(id=PID, settings_json={})
    db = _FakeDB(
        objects={("Project", PID): project},
        execute_results=[_FakeResult([])],
    )

    with pytest.raises(Exception) as exc:
        await start_async_generation(
            AsyncGenerateRequest(
                project_id=PID,
                task_type="outline_volume",
                volume_idx=1,
            ),
            db=db,
        )

    assert getattr(exc.value, "status_code", None) == 422
    assert "outline_chain_incomplete" in str(getattr(exc.value, "detail", ""))


@pytest.mark.asyncio
async def test_generate_outline_volume_blocks_without_book_outline():
    from app.api.generate import GenerateOutlineRequest, generate_outline

    project = SimpleNamespace(id=PID, settings_json={}, target_word_count=3000000)
    db = _FakeDB(
        objects={("Project", PID): project},
        execute_results=[
            _FakeResult([]),  # book outline lookup
            _FakeResult([]),  # filter-word lookup
        ],
    )

    with patch("app.services.style_runtime.resolve_style_prompt", AsyncMock(return_value="")), patch(
        "app.services.foreshadow_lifecycle.load_active_foreshadows_for_context",
        AsyncMock(return_value=[]),
    ), patch("app.api.generate.OutlineGenerator.generate_volume_outline") as volume_outline_mock:
        response = await generate_outline(
            GenerateOutlineRequest(project_id=PID, level="volume", volume_idx=1),
            db=db,
        )
        text = await _collect_sse_text(response)

    assert "outline_chain_incomplete" in text
    assert "book" in text
    volume_outline_mock.assert_not_called()


@pytest.mark.asyncio
async def test_generate_outline_chapter_blocks_without_volume_outline():
    from app.api.generate import GenerateOutlineRequest, generate_outline

    project = SimpleNamespace(id=PID, settings_json={}, target_word_count=3000000)
    db = _FakeDB(
        objects={("Project", PID): project},
        execute_results=[
            _FakeResult([SimpleNamespace(content_json={"raw_text": "全书大纲"})]),  # book outline
            _FakeResult([]),  # filter-word lookup
        ],
    )

    with patch("app.services.style_runtime.resolve_style_prompt", AsyncMock(return_value="")), patch(
        "app.services.foreshadow_lifecycle.load_active_foreshadows_for_context",
        AsyncMock(return_value=[]),
    ), patch("app.api.generate.OutlineGenerator.generate_chapter_outline") as chapter_outline_mock:
        response = await generate_outline(
            GenerateOutlineRequest(project_id=PID, level="chapter", chapter_idx=1),
            db=db,
        )
        text = await _collect_sse_text(response)

    assert "outline_chain_incomplete" in text
    assert "volume" in text
    chapter_outline_mock.assert_not_called()


@pytest.mark.asyncio
async def test_expand_chapter_outline_blocks_without_book_or_volume():
    from app.api.chapters import expand_chapter_outline_endpoint
    from app.models.project import Chapter, Volume

    chapter = SimpleNamespace(
        id="chapter-1",
        volume_id="volume-1",
        chapter_idx=1,
        outline_json={},
        title="第一章",
        summary="",
    )
    volume = SimpleNamespace(id="volume-1", project_id=PID, volume_idx=1, title="第一卷")
    db = _FakeDB(
        objects={
            ("Chapter", "chapter-1"): chapter,
            ("Volume", "volume-1"): volume,
        },
        execute_results=[
            _FakeResult([]),
            _FakeResult([]),
        ],
    )

    with patch("app.services.chapter_outline_expander.expand_chapter_outline") as expand_mock:
        with pytest.raises(Exception) as exc:
            await expand_chapter_outline_endpoint(
                project_id=PID,
                chapter_id="chapter-1",
                db=db,
            )

    assert "outline_chain_incomplete" in str(exc.value)
    expand_mock.assert_not_called()
