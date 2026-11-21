"""每场篇幅兜底:写不够时补一次有界续写,且不能污染正文。

背景(2026-11-15 实测):600 字目标只写 330–500;章级质量门在"篇幅不足"时
直接 `blocked` 落 draft、`rewrite_rounds=0`,从补写。提示里早就写了目标字数,
所以这里要的是机械兜底:一次续写 + 接缝去重 + 无效就丢弃。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.scene_orchestrator import (
    SceneOrchestrator,
    _scene_continuation_max,
    _scene_min_words,
)


TAIL = "她把信封重新压回碗底,抬眼看了一下江面。"
PRIOR = "周芸坐在面摊油腻的长凳上,面前是一碗已经坨了的阳春面。" + TAIL
NEW_TEXT = ("周航从船舷上跳下来,踩着湿滑的石阶三两步上了岸,额头上还带着一层薄汗,"
            "快步走了过来,把工具包放在长凳另一头。")


def _scene(target_words: int = 600):
    return SimpleNamespace(idx=1, target_words=target_words)


def _install_writer(orch: SceneOrchestrator, outputs: list[str]) -> list[dict]:
    """把 write_scene_stream 换成按序返回固定文本的假流;记下每次调用。"""
    calls: list[dict] = []

    async def fake(*, scene, pack, prior_scenes_summary, db, project_id, chapter_id,
                   user_instruction="", extra_user_block=""):
        calls.append({"prior": prior_scenes_summary, "extra": extra_user_block})
        text = outputs[min(len(calls) - 1, len(outputs) - 1)]
        for start in range(0, len(text), 17):
            yield text[start:start + 17]

    orch.write_scene_stream = fake  # type: ignore[assignment]
    return calls


async def _collect(orch: SceneOrchestrator, prior_text: str) -> list[str]:
    out = []
    async for merged in orch._scene_continuations(
            scene=_scene(), pack=SimpleNamespace(), prior_text=prior_text, db=None,
            project_id="p", chapter_id="c"):
        out.append(merged)
    return out


def test_seam_strips_the_repeated_tail():
    """续写把上一段最后一句又写了一遍:必须裁掉,只留新增部分。"""
    out = SceneOrchestrator._strip_duplicate_seam(PRIOR, TAIL + NEW_TEXT)
    assert out.startswith("周航从船舷上跳下来")
    assert TAIL not in out


def test_seam_keeps_a_clean_continuation():
    out = SceneOrchestrator._strip_duplicate_seam(PRIOR, NEW_TEXT)
    assert out == NEW_TEXT


def test_seam_overlap_boundary_is_twelve_chars():
    """重叠 11 字不裁(可能是巧合),12 字裁(是重述)。"""
    base = "甲" * 60
    eleven = base[-11:] + NEW_TEXT
    twelve = base[-12:] + NEW_TEXT
    assert SceneOrchestrator._strip_duplicate_seam(base, eleven) == eleven
    assert not SceneOrchestrator._strip_duplicate_seam(base, twelve).startswith(base[-12:])


def test_seam_discards_pure_duplicate_and_too_short():
    """整段重述、或新增不足 40 字:丢弃,不拼接。"""
    assert SceneOrchestrator._strip_duplicate_seam(PRIOR, PRIOR) == ""
    assert SceneOrchestrator._strip_duplicate_seam(PRIOR, "周航上了岸。") == ""


@pytest.mark.asyncio
async def test_continuation_fires_once_when_scene_is_short(monkeypatch):
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_MAX", 1, raising=False)
    monkeypatch.setattr(settings, "SCENE_LENGTH_MIN_RATIO", 0.85, raising=False)
    orch = SceneOrchestrator()
    short = "周芸把信封压在碗下。" * 8          # 约 80 字,远低于 510
    calls = _install_writer(orch, [short, NEW_TEXT * 4])
    merged = await _collect(orch, "已写的开头。")
    assert len(calls) == 1, "首写在循环外,这里只应发生一次续写"
    assert "本场还没写够" in calls[0]["extra"]
    assert merged and len(merged[0]) >= 40


@pytest.mark.asyncio
async def test_continuation_stops_at_max_rounds(monkeypatch):
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_MAX", 2, raising=False)
    orch = SceneOrchestrator()
    always_short = "周芸把信封压在碗下,周航上了岸,两人站在面摊前谁都没有先开口说话,江风把芦苇吹得沙沙响。"
    calls = _install_writer(orch, [always_short] * 6)
    await _collect(orch, "已写的开头。")
    assert len(calls) == _scene_continuation_max() == 2


@pytest.mark.asyncio
async def test_continuation_disabled_changes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_ENABLED", False, raising=False)
    orch = SceneOrchestrator()
    calls = _install_writer(orch, ["短。"])
    merged = await _collect(orch, "已写的开头。")
    assert calls == [] and merged == []


@pytest.mark.asyncio
async def test_duplicate_continuation_is_discarded(monkeypatch):
    """续写只是把原样正文再吐一遍:不许追加,也不许无限重试。"""
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_MAX", 1, raising=False)
    orch = SceneOrchestrator()
    prior = "周芸把信封压在碗下。" * 10
    calls = _install_writer(orch, [prior])
    merged = await _collect(orch, prior)
    assert merged == []
    assert len(calls) == 1