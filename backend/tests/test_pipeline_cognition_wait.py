"""pipeline 在推进下一章前等该章认知就绪(2026-11-21)。

不加这道等待时,链路的实际表现是:ch1 成功、ch2 起全部
``character_cognition_not_ready``。原因是下一章的上下文要求"它之前所有章"的
认知记录已是 ``completed``(cognition_reader.validate_completed_receipt 对缺失
或非 completed 直接拒绝),而认知提取是章后**异步**派发的 —— 紧接着取下一章
必然读到缺失。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.production import runtime


def test_wait_seconds_reads_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_COGNITION_WAIT_SECONDS", raising=False)
    assert runtime._cognition_wait_seconds() == 600
    monkeypatch.setenv("PIPELINE_COGNITION_WAIT_SECONDS", "0")
    assert runtime._cognition_wait_seconds() == 0
    monkeypatch.setenv("PIPELINE_COGNITION_WAIT_SECONDS", "not-a-number")
    assert runtime._cognition_wait_seconds() == 600
    monkeypatch.setenv("PIPELINE_COGNITION_WAIT_SECONDS", "-5")
    assert runtime._cognition_wait_seconds() == 0


def _sessions_returning(statuses):
    """按调用次数依次返回这一串 record status(用尽后停在最后一个)。"""
    calls = {"n": 0}

    class _DB:
        async def scalar(self, *_args, **_kwargs):
            index = min(calls["n"], len(statuses) - 1)
            calls["n"] += 1
            status = statuses[index]
            return None if status is None else SimpleNamespace(status=status)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    return (lambda: _DB()), calls


def test_wait_returns_true_only_after_cognition_completed():
    sessions, calls = _sessions_returning(["pending", "completed"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0))
    assert ok is True
    assert calls["n"] == 2, "应当轮询到 completed 才返回,不能看到 pending 就放行"


def test_wait_returns_false_on_unusable_terminal_state():
    """failed / obsolete 是终态但不能用作上下文 —— 不能无限等。"""
    for status in ("failed", "obsolete"):
        sessions, calls = _sessions_returning([status])
        ok = asyncio.run(runtime._wait_chapter_cognition(
            sessions, "chapter", timeout_seconds=30, poll_seconds=0))
        assert ok is False
        assert calls["n"] == 1, "终态应立即返回,不该继续轮询"


def test_wait_times_out_when_record_never_appears():
    sessions, _ = _sessions_returning([None])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=0.01, poll_seconds=0))
    assert ok is False


def test_zero_timeout_preserves_legacy_behaviour(monkeypatch):
    """PIPELINE_COGNITION_WAIT_SECONDS=0 时不查库、直接放行。"""
    sessions, calls = _sessions_returning(["pending"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=0))
    assert ok is True
    assert calls["n"] == 0, "禁用等待时不应查询数据库"

    monkeypatch.setenv("PIPELINE_COGNITION_WAIT_SECONDS", "0")
    assert runtime._cognition_wait_seconds() == 0
