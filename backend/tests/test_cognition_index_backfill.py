"""回归:认知索引回填(换 embedding 模型后的重建入口)。

背景:集合名带 space 指纹,换模型等于换集合;索引是懒建的(`rank_cognition` 解析上下文时
顺手 upsert),而已写完的书不会再生成一次 —— 所以召回会长期为空且不报错。
本模块补上回填入口,行为要满足:按写作路径逐章补、单章失败不中断、幂等、返回统计。

踩过的坑:`load_candidate_context` 返回的是**渲染后的文本**(它内部已经把检索跑掉了),
不能当 prompt 用 —— 必须走 `load_prompt_context(..., defer_retrieval=True)`。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import character_cognition, cognition_index_backfill as backfill
from app.services import cognition_retrieval


class _Row:
    def __init__(self, idx: int, cid: str):
        self.id = cid
        self.chapter_idx = idx
        self.global_idx = idx
        self.outline_json = {"summary": f"第{idx}章"}
        self.content_text = f"第{idx}章正文"


class _Space:
    identity = "space-current"

    async def aclose(self) -> None:  # pragma: no cover
        pass


class _Prompt:
    def __init__(self, before):
        self.before_chapter_id = before


class _Session:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _sessions():
    return lambda: _Session()


@pytest.mark.asyncio
async def test_indexes_every_chapter_after_the_first(monkeypatch):
    rows = [_Row(i, f"c{i}") for i in range(1, 5)]
    monkeypatch.setattr(backfill, "_project_chapters", AsyncMock(return_value=rows))
    calls = []

    async def fake_load(db, pid, *, before_chapter_id, chapter_outline, chapter_text,
                        previous_text="", priority_names=(), max_chars=1200,
                        defer_retrieval=False):
        assert defer_retrieval is True, "回填必须用 defer_retrieval,不能在 DB 会话里嵌入"
        return _Prompt(before_chapter_id)

    async def fake_rank(prompt, *, client, space):
        calls.append(prompt.before_chapter_id)
        return []

    monkeypatch.setattr(character_cognition, "load_prompt_context", fake_load)
    monkeypatch.setattr(cognition_retrieval, "rank_cognition", fake_rank)

    result = await backfill.backfill_project_cognition(
        "p1", sessions=_sessions(), space_loader=AsyncMock(return_value=_Space()), client=object())

    assert result["status"] == "ok"
    assert result["chapters_indexed"] == 3
    assert calls == ["c2", "c3", "c4"], "第一章没有'之前的认知',必须跳过"
    assert result["space_id"] == "space-current"


@pytest.mark.asyncio
async def test_single_chapter_failure_does_not_stop_the_loop(monkeypatch):
    rows = [_Row(i, f"c{i}") for i in range(1, 4)]
    monkeypatch.setattr(backfill, "_project_chapters", AsyncMock(return_value=rows))

    async def fake_load(db, pid, *, before_chapter_id, chapter_outline, chapter_text,
                        previous_text="", priority_names=(), max_chars=1200,
                        defer_retrieval=False):
        if before_chapter_id == "c2":
            raise RuntimeError("embedding down")
        return _Prompt(before_chapter_id)

    monkeypatch.setattr(character_cognition, "load_prompt_context", fake_load)
    monkeypatch.setattr(cognition_retrieval, "rank_cognition", AsyncMock(return_value=[]))

    result = await backfill.backfill_project_cognition(
        "p1", sessions=_sessions(), space_loader=AsyncMock(return_value=_Space()), client=object())

    assert result["status"] == "partial"
    assert (result["chapters_indexed"], result["chapters_failed"]) == (1, 1)
    assert result["failures"][0]["chapter_idx"] == 2
    assert "embedding down" in result["failures"][0]["error"]


@pytest.mark.asyncio
async def test_prompt_without_before_chapter_is_counted_as_skipped(monkeypatch):
    rows = [_Row(1, "c1"), _Row(2, "c2")]
    monkeypatch.setattr(backfill, "_project_chapters", AsyncMock(return_value=rows))
    monkeypatch.setattr(character_cognition, "load_prompt_context",
                        AsyncMock(return_value=_Prompt(None)))
    monkeypatch.setattr(cognition_retrieval, "rank_cognition", AsyncMock(return_value=[]))

    result = await backfill.backfill_project_cognition(
        "p1", sessions=_sessions(), space_loader=AsyncMock(return_value=_Space()), client=object())

    assert result["chapters_skipped"] == 1 and result["chapters_indexed"] == 0


@pytest.mark.asyncio
async def test_empty_project_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(backfill, "_project_chapters", AsyncMock(return_value=[]))
    result = await backfill.backfill_project_cognition(
        "p1", sessions=_sessions(), space_loader=AsyncMock(return_value=_Space()), client=object())
    assert result["status"] == "empty"
    assert result["chapters_indexed"] == 0