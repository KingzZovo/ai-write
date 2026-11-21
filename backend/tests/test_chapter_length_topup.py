"""章级篇幅补足(方案 D):整章不足时用最后一场上下文补 ≤N 次(2026-11-18)。

背景:超分配(送模目标 ×1.08)只把中位从 1870 拉到 2029,10 章里仍有 4 章 <2000
(最短 1582)。场级续写只看单场,章是场之和 —— 每场各欠一点就短了。所以补一层
**章级**核算:不足时才补,补到的增量必须 yield 给调用方(否则流式展示与落库不一致)。

这里只测补足函数本身:把 ``write_scene_stream`` 换成假流,不调模型。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.scene_orchestrator import (  # noqa: F401
    SceneOrchestrator,
    _chapter_min_words,
    _chapter_topup_threshold,
)


class _FakeOrchestrator(SceneOrchestrator):
    """把写手换成"吐出固定 chunks",并记录调用参数。"""

    def __init__(self, chunks: list[str]) -> None:
        super().__init__()
        self._chunks = chunks
        self.calls: list[dict] = []

    async def write_scene_stream(self, **kwargs):  # type: ignore[override]
        self.calls.append(kwargs)
        for chunk in self._chunks:
            yield chunk


def _scene():
    return SimpleNamespace(idx=1, target_words=1000)


async def _collect(orch: SceneOrchestrator, **kwargs) -> list[str]:
    out: list[str] = []
    async for chunk in orch._chapter_length_topup(**kwargs):
        out.append(chunk)
    return out


_BASE = dict(
    scene=_scene(), pack=None, chapter_target=2000,
    db=None, project_id="p", chapter_id="c", user_instruction="",
)


def test_no_topup_when_chapter_reaches_full_target():
    """达到**真实目标**(2000)才停 —— 补足线不是验收线(1900)。"""
    orch = _FakeOrchestrator(["不该被调用"])
    threshold = _chapter_topup_threshold(2000)
    assert threshold == 2000
    out = asyncio.run(_collect(orch, produced_chars=threshold, last_scene_text="x" * threshold, **_BASE))
    assert out == []
    assert orch.calls == []


def test_topup_still_runs_between_acceptance_floor_and_target():
    """1900(能过质量门)但 <2000:仍然要补 —— 用户要求的是"起码 2000"。"""
    orch = _FakeOrchestrator(["补" * 120])
    out = asyncio.run(_collect(orch, produced_chars=1900, last_scene_text="正" * 1900, **_BASE))
    assert out == ["补" * 120]
    assert len(orch.calls) == 1
    assert "100" in orch.calls[0]["extra_user_block"]


def test_topup_runs_once_when_chapter_is_short():
    orch = _FakeOrchestrator(["补" * 500])
    out = asyncio.run(_collect(orch, produced_chars=1500, last_scene_text="正" * 1500, **_BASE))
    assert out and out[0] == "补" * 500
    assert len(orch.calls) == 1
    extra = orch.calls[0]["extra_user_block"]
    # 缺口 = 2000 - 1500 = 500,提示里必须写出这个数字,写手才知道补多少。
    assert "500" in extra
    assert "整章补足" in extra


def test_topup_is_bounded_by_config(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "CHAPTER_LENGTH_CONTINUATION_MAX", 0, raising=False)
    orch = _FakeOrchestrator(["补" * 500])
    out = asyncio.run(_collect(orch, produced_chars=1000, last_scene_text="正" * 1000, **_BASE))
    assert out == []
    assert orch.calls == []


def test_topup_stops_when_continuation_is_only_repetition():
    """续写只是重复最后一段时丢弃,不把重复内容拼进正文。"""
    prior = "他把门推开,楼道里的灯忽明忽暗。" * 40
    orch = _FakeOrchestrator([prior[-200:]])  # 纯粹是尾巴的重复
    out = asyncio.run(_collect(orch, produced_chars=1500, last_scene_text=prior, **_BASE))
    assert out == []
    assert len(orch.calls) == 1


def test_topup_ignores_empty_continuation():
    orch = _FakeOrchestrator([""])
    out = asyncio.run(_collect(orch, produced_chars=1500, last_scene_text="正" * 1500, **_BASE))
    assert out == []


@pytest.mark.parametrize("produced", [0, 500, 1899])
def test_topup_triggers_below_floor_only(produced):
    orch = _FakeOrchestrator(["补" * 300])
    out = asyncio.run(_collect(orch, produced_chars=produced, last_scene_text="正" * max(produced, 1), **_BASE))
    assert bool(out) is True


def test_chapter_floor_matches_quality_gate():
    """生成侧与验收侧必须同口径,否则补足白补(补到 0.9 仍被判 blocked)。"""
    from app.services.chapter_quality_gate import _min_target_word_count

    assert _chapter_min_words(2000) == _min_target_word_count(2000)
    assert _chapter_min_words(1800) == _min_target_word_count(1800)