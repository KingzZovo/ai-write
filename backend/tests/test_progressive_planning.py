"""渐进式规划: ``ensure_minimal_plan_for_chapter`` 的离线用例。

全部离线:db 用内存 fake(不连库、不连模型),generator 用 fake 并记录调用。
盯的是"缺什么补什么"的判断与顺序,不是模型输出。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.project import Character, Chapter, Outline, Project, Volume
from app.services.progressive_planning import (
    PIECES,
    MinimalPlanStatus,
    ensure_minimal_plan_for_chapter,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """够用的假会话:execute/get/add/flush/commit,读库返回该模型全部行。"""

    def __init__(self, *, projects=(), outlines=(), volumes=(), characters=(), chapters=()):
        self._store = {
            Project: list(projects),
            Outline: list(outlines),
            Volume: list(volumes),
            Character: list(characters),
            Chapter: list(chapters),
        }
        self.commits = 0

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        return _FakeResult(self._store.get(entity, []))

    async def get(self, model, pk):
        for row in self._store.get(model, []):
            if str(getattr(row, "id", None)) == str(pk):
                return row
        return None

    def add(self, obj):
        self._store.setdefault(type(obj), []).append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    def rows(self, model):
        return list(self._store.get(model, []))


class FakeGenerator:
    """记录调用顺序;可在指定方法上抛错。"""

    def __init__(self, *, book=None, volume=None, chapter=None, raise_on=()):
        self.calls: list[str] = []
        self._book = book
        self._volume = volume
        self._chapter = chapter
        self._raise_on = set(raise_on)

    async def generate_book_outline(self, **kwargs):
        self.calls.append("generate_book_outline")
        if "book" in self._raise_on:
            raise RuntimeError("book boom")
        return dict(self._book) if isinstance(self._book, dict) else {}

    async def generate_volume_outline(self, **kwargs):
        self.calls.append("generate_volume_outline")
        if "volume" in self._raise_on:
            raise RuntimeError("volume boom")
        return dict(self._volume) if isinstance(self._volume, dict) else {}

    async def generate_chapter_outline(self, **kwargs):
        self.calls.append("generate_chapter_outline")
        if "chapter" in self._raise_on:
            raise RuntimeError("chapter boom")
        return dict(self._chapter) if isinstance(self._chapter, dict) else {}


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

PID = uuid4()


def project():
    return Project(id=PID, title="渡口旧账", premise="菜谱属于母亲。", genre="现实生活",
                   target_word_count=200000, settings_json={})


def book_content():
    return {
        "raw_text": "一、作品定位\n菜谱属于母亲。\n\n六、世界观设定集\n沿江渡口的旧账本规则。",
        "structured": {
            "main_plot": "追索旧账",
            "characters": "二、主要角色与小传\n林禾:学徒。",
            "world_setting": "沿江渡口的旧账本规则。",
        },
        "volume_plan": [{"idx": 1, "est_chapters": 30}],
        "volume_count": 1,
    }


def book_row(content=None):
    return Outline(id=uuid4(), project_id=PID, level="book", parent_id=None, version=1,
                   is_confirmed=1, content_json=content if content is not None else book_content())


def volume_row(volume_idx=1):
    return Volume(id=uuid4(), project_id=PID, volume_idx=volume_idx, title=f"第{volume_idx}卷")


def volume_outline_row(volume, chapters=(1, 2)):
    return Outline(
        id=uuid4(), project_id=PID, level="volume", parent_id=None, version=1, is_confirmed=1,
        content_json={
            "volume_idx": volume.volume_idx,
            "chapter_count": 30,
            "chapter_summaries": [
                {"chapter_idx": i, "title": f"第{i}章", "summary": f"第{i}章剧情"} for i in chapters
            ],
        },
    )


def chapter_outline_row(content=None, parent_id=None):
    return Outline(id=uuid4(), project_id=PID, level="chapter", parent_id=parent_id, version=1,
                   is_confirmed=0,
                   content_json=content if content is not None else {"chapter_idx": 1, "summary": "交接"})


def generated_volume(chapters=(1, 2, 3, 4, 5, 6)):
    return {
        "volume_idx": 1,
        "title": "第一卷",
        "chapter_count": len(chapters),
        "chapter_summaries": [
            {"chapter_idx": i, "title": f"第{i}章", "summary": f"第{i}章剧情"} for i in chapters
        ],
    }


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_missing_reports_all_five_pieces():
    gen = FakeGenerator(book={})  # 生成器拿不到可用书纲 → 什么都补不上
    db = FakeSession(projects=[project()])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen)
    assert isinstance(status, MinimalPlanStatus)
    assert status.ready is False
    assert status.missing == list(PIECES)
    assert set(status.missing) == {
        "story_base", "world_slice", "core_characters", "chapter_route", "chapter_contract"
    }
    assert gen.calls == ["generate_book_outline"]  # 依赖不满足,不继续往下生成
    assert status.errors  # 有失败说明


@pytest.mark.asyncio
async def test_book_present_volume_missing_only_volume_generator_runs():
    book = book_row()
    contract = chapter_outline_row()
    gen = FakeGenerator(volume=generated_volume())
    db = FakeSession(projects=[project()], outlines=[book, contract])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen)
    assert gen.calls == ["generate_volume_outline"], "书纲在、合同在,只该补卷纲"
    assert status.ready is True
    assert status.missing == []


@pytest.mark.asyncio
async def test_route_with_two_chapters_is_ready_for_chapter_one():
    book = book_row()
    volume = volume_row()
    vol_outline = volume_outline_row(volume, chapters=(1, 2))
    contract = chapter_outline_row(parent_id=vol_outline.id)
    gen = FakeGenerator(volume=generated_volume(), chapter={"chapter_idx": 1, "summary": "x"})
    db = FakeSession(projects=[project()], outlines=[book, vol_outline, contract], volumes=[volume])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen)
    assert status.ready is True
    assert status.missing == []
    assert gen.calls == [], "路线已覆盖第 1 章,合同也在,不该调任何生成器"


@pytest.mark.asyncio
async def test_chapter_30_beyond_route_reports_chapter_route_missing():
    book = book_row()
    volume = volume_row()
    vol_outline = volume_outline_row(volume, chapters=(1, 2, 3, 4, 5))
    gen = FakeGenerator(volume=generated_volume(), chapter={"chapter_idx": 30, "summary": "x"})
    db = FakeSession(projects=[project()], outlines=[book, vol_outline], volumes=[volume])
    status = await ensure_minimal_plan_for_chapter(db, PID, 30, generator=gen)
    assert status.ready is False
    assert "chapter_route" in status.missing, "第 30 章超出最小路线范围,应报告需要补路线"
    # 路线已到 max_route_chapters(默认 5),不再无意义地重生成整卷
    assert gen.calls == []
    assert any("chapter_route" in e for e in status.errors)


@pytest.mark.asyncio
async def test_missing_chapter_contract_calls_chapter_generator():
    book = book_row()
    volume = volume_row()
    vol_outline = volume_outline_row(volume, chapters=(1, 2))
    gen = FakeGenerator(chapter={"chapter_idx": 1, "summary": "交接", "key_events": ["收好菜谱"]})
    db = FakeSession(projects=[project()], outlines=[book, vol_outline], volumes=[volume])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen)
    assert gen.calls == ["generate_chapter_outline"]
    assert status.ready is True
    assert "chapter_contract" not in status.missing


@pytest.mark.asyncio
async def test_everything_ready_calls_no_generator():
    book = book_row()
    volume = volume_row()
    vol_outline = volume_outline_row(volume, chapters=(1, 2))
    chapter = Chapter(id=uuid4(), volume_id=volume.id, title="第一章", chapter_idx=1,
                      outline_json={"chapter_idx": 1, "summary": "交接"})
    gen = FakeGenerator(book=book_content(), volume=generated_volume(),
                        chapter={"chapter_idx": 1, "summary": "x"})
    db = FakeSession(projects=[project()], outlines=[book, vol_outline], volumes=[volume],
                     chapters=[chapter])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen)
    assert gen.calls == []
    assert status.ready is True
    assert status.missing == []
    assert set(status.have) == set(PIECES)


@pytest.mark.asyncio
async def test_generator_error_does_not_crash_and_keeps_missing():
    gen = FakeGenerator(book=book_content(), raise_on=("book",))
    db = FakeSession(projects=[project()])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen)
    assert status.ready is False
    assert set(status.missing) == set(PIECES)
    assert any("book boom" in e for e in status.errors), "错误原文要带在结果里"


@pytest.mark.asyncio
async def test_book_generation_fills_story_base_world_and_characters_in_one_call():
    gen = FakeGenerator(book=book_content(), volume=generated_volume(),
                        chapter={"chapter_idx": 1, "summary": "交接"})
    db = FakeSession(projects=[project()])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen)
    # 三段式书纲一次调用同时给出 story_base / world_slice / core_characters
    assert gen.calls == ["generate_book_outline", "generate_volume_outline", "generate_chapter_outline"]
    assert status.ready is True
    for piece in ("story_base", "world_slice", "core_characters"):
        assert piece in status.have
        assert piece not in status.missing


@pytest.mark.asyncio
async def test_route_keeps_only_max_route_chapters():
    book = book_row()
    gen = FakeGenerator(volume=generated_volume(chapters=tuple(range(1, 11))))
    db = FakeSession(projects=[project()], outlines=[book])
    status = await ensure_minimal_plan_for_chapter(db, PID, 1, generator=gen, max_route_chapters=3)
    assert status.have["chapter_route"] == [1, 2, 3]
    stored = [o for o in db.rows(Outline) if o.level == "volume"]
    assert len(stored) == 1
    summaries = stored[0].content_json["chapter_summaries"]
    assert [c["chapter_idx"] for c in summaries] == [1, 2, 3], "只保留前 N 章路线,其余丢弃"
    assert stored[0].content_json["_progressive_route"]["truncated"] is True
