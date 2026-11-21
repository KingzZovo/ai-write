"""pipeline 在推进下一章前等该章认知就绪(2026-11-21)。

不加这道等待时,链路的实际表现是:ch1 成功、ch2 起全部
``character_cognition_not_ready``。原因是下一章的上下文要求"它之前所有章"的
认知记录已是 ``completed``(cognition_reader.validate_completed_receipt 对缺失
或非 completed 直接拒绝),而认知提取是章后**异步**派发的 —— 紧接着取下一章
必然读到缺失。

补充实测:提取本身会**暂时**失败(与正文/实体写入竞争,人工重试能成功),
所以 ``failed`` 不能直接放行,而要放回项目自己的 pending 队列继续等
(Beat 的 cognition.replay_pending 每 60s 拾取),并限次以免永久失败时死循环。
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
    """按调用次数依次返回这一串 record status(用尽后停在最后一个)。

    同一个 db 实例在多次 `sessions()` 之间复用,便于断言 execute/commit。
    """
    calls = {"n": 0}

    class _DB:
        def __init__(self):
            self.executed = 0
            self.commits = 0

        async def scalar(self, *_args, **_kwargs):
            index = min(calls["n"], len(statuses) - 1)
            calls["n"] += 1
            status = statuses[index]
            if status is None:
                return None
            return SimpleNamespace(status=status, id="record-id")

        async def execute(self, _statement):
            self.executed += 1

        async def commit(self):
            self.commits += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    db = _DB()
    return (lambda: db), db, calls


def test_wait_returns_true_only_after_cognition_completed():
    sessions, _db, calls = _sessions_returning(["pending", "completed"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0))
    assert ok is True
    assert calls["n"] == 2, "应当轮询到 completed 才放行,看到 pending 不能返回"


def test_failed_cognition_is_requeued_and_waited_again():
    """failed 是暂时的:放回 pending 继续等,等到 completed 算就绪。"""
    sessions, db, _calls = _sessions_returning(["failed", "pending", "completed"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0))
    assert ok is True
    assert db.executed == 1, "失败记录必须被重新入队一次"
    assert db.commits == 1, "重排队必须提交,否则 Beat 看不到"


def test_requeue_is_bounded():
    """永久失败的章不能让我们无限重排。"""
    sessions, db, _calls = _sessions_returning(["failed"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0, requeue_limit=2))
    assert ok is False
    assert db.executed == 2, "重排队次数必须受 requeue_limit 限制"


def test_obsolete_is_never_requeued():
    """obsolete 是作者决策,不能自动重开。"""
    sessions, db, calls = _sessions_returning(["obsolete"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0))
    assert ok is False
    assert db.executed == 0, "obsolete 不得重新入队"
    assert calls["n"] == 1, "终态应立即返回,不该继续轮询"


def test_wait_times_out_when_record_never_appears():
    sessions, _db, _calls = _sessions_returning([None])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=0.01, poll_seconds=0))
    assert ok is False


def test_zero_timeout_preserves_legacy_behaviour(monkeypatch):
    """PIPELINE_COGNITION_WAIT_SECONDS=0 时不查库、直接放行。"""
    sessions, db, calls = _sessions_returning(["pending"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=0))
    assert ok is True
    assert calls["n"] == 0, "禁用等待时不应查询数据库"
    assert db.executed == 0

    monkeypatch.setenv("PIPELINE_COGNITION_WAIT_SECONDS", "0")
    assert runtime._cognition_wait_seconds() == 0
