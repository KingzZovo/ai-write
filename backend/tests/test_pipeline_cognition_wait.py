"""pipeline 在推进下一章前等该章认知就绪(2026-11-21)。

不加这道等待时:ch1 成功、ch2 起全部 ``character_cognition_not_ready`` ——
下一章的上下文要求"它之前所有章"的认知记录为 completed
(cognition_reader.validate_completed_receipt 对缺失/非 completed 直接拒绝),
而提取是章后**异步**派发的。

三条来自独立审查的约束也在这里锁住:

1. ``failed`` **不能**当立即终态。提取任务带
   ``autoretry_for=(Exception,), max_retries=3``,重投时同一 delivery_id 会把
   记录置回 running 并可能完成 —— 自己 requeue 反而与它打架。所以继续轮询。
2. ``obsolete``(源已变/作者决策)才是真终态,立即放弃。
3. 等待在 ``project_worker_lock`` 内,必须每轮检查 ``should_stop``,否则用户
   pause 会被拖满整个超时、rollback 也拿不到锁。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.config import settings
from app.services.production import runtime


def test_wait_seconds_reads_settings(monkeypatch):
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", 0, raising=False)
    assert runtime._cognition_wait_seconds() == 0
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", 45, raising=False)
    assert runtime._cognition_wait_seconds() == 45
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", -3, raising=False)
    assert runtime._cognition_wait_seconds() == 0, "负值按关闭处理,不能变成无限等"
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", "bad", raising=False)
    assert runtime._cognition_wait_seconds() == 180


def _sessions(statuses):
    """按调用次数依次返回状态串(用尽后停在最后一个);同一 db 实例复用。"""
    calls = {"n": 0}

    class _DB:
        def __init__(self):
            self.executed = 0
            self.commits = 0

        async def scalar(self, *_args, **_kwargs):
            index = min(calls["n"], len(statuses) - 1)
            calls["n"] += 1
            status = statuses[index]
            return None if status is None else SimpleNamespace(status=status, id="record-id")

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


def test_returns_true_only_after_completed():
    sessions, _db, calls = _sessions(["pending", "completed"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0))
    assert ok is True
    assert calls["n"] == 2, "看到 pending 不能放行"


def test_failed_is_not_treated_as_final():
    """failed 后仍继续等 —— autoretry 可能把它救回来。"""
    sessions, db, _calls = _sessions(["failed", "completed"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0))
    assert ok is True
    assert db.executed == 0, "不应自行 requeue:那会与 Celery autoretry 抢同一个记录"


def test_persistently_failed_times_out_without_requeue():
    sessions, db, _calls = _sessions(["failed"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=0.01, poll_seconds=0))
    assert ok is False
    assert db.executed == 0


def test_obsolete_is_terminal_immediately():
    sessions, _db, calls = _sessions(["obsolete"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=30, poll_seconds=0))
    assert ok is False
    assert calls["n"] == 1, "obsolete 应立即返回"


def test_should_stop_breaks_the_wait():
    """pause / 所有权变化要能打断等待,不能把锁占到超时。"""
    sessions, _db, _calls = _sessions(["pending"])
    stops = {"n": 0}

    async def should_stop():
        stops["n"] += 1
        return True

    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=600, poll_seconds=0, should_stop=should_stop))
    assert ok is False
    assert stops["n"] == 1, "应当先问 should_stop 再查库,命中即返回"


def test_timeout_when_record_never_appears():
    sessions, _db, _calls = _sessions([None])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=0.01, poll_seconds=0))
    assert ok is False


def test_zero_timeout_preserves_legacy_behaviour():
    sessions, db, calls = _sessions(["pending"])
    ok = asyncio.run(runtime._wait_chapter_cognition(
        sessions, "chapter", timeout_seconds=0))
    assert ok is True
    assert calls["n"] == 0, "禁用等待时不应查库"
    assert db.executed == 0
