"""pipeline 在推进下一章前等该章认知就绪(2026-11-21)。

不加等待时:ch1 成功、ch2 起全部 ``character_cognition_not_ready`` —— 下一章的
上下文要求"它之前所有章"的认知记录为 completed,而提取是章后**异步**派发的。

三条来自实测与独立审查的约束锁在这里:

1. ``completed`` 才放行;``obsolete``(源已变/作者决策)立即放弃。
2. ``failed`` **不能立刻重投**:Celery 的 autoretry 是秒级三次,同一 prompt 下
   会重复同一错误(实测某章 4 次全败)。必须等冷却满了再置回 pending ——
   而这也是**唯一**有效的重试路径:``run_cognition_extraction`` 对
   ``failed`` + 非原 delivery_id 的投递直接 skip(model_retry_requires_original_task,
   见 test_cognition_workflow_offline),置回 pending 才能绕开它。
3. 等待在 ``project_worker_lock`` 内,必须每轮检查 ``should_stop``。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import settings
from app.services.production import runtime

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def test_wait_seconds_reads_settings(monkeypatch):
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", 0, raising=False)
    assert runtime._cognition_wait_seconds() == 0
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", 45, raising=False)
    assert runtime._cognition_wait_seconds() == 45
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", -3, raising=False)
    assert runtime._cognition_wait_seconds() == 0, "负值按关闭处理"
    monkeypatch.setattr(settings, "PIPELINE_COGNITION_WAIT_SECONDS", "bad", raising=False)
    assert runtime._cognition_wait_seconds() == 180


def _sessions(records):
    """按调用次数依次返回 (status, updated_at) 对(用尽后停在最后一个)。"""
    calls = {"n": 0}

    class _DB:
        def __init__(self):
            self.executed = 0
            self.commits = 0

        async def scalar(self, *_args, **_kwargs):
            index = min(calls["n"], len(records) - 1)
            calls["n"] += 1
            item = records[index]
            if item is None:
                return None
            status, age_seconds = item
            return SimpleNamespace(
                status=status, id="record-id",
                updated_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
            )

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


def _run(sessions, **kwargs):
    kwargs.setdefault("timeout_seconds", 30)
    kwargs.setdefault("poll_seconds", 0)
    return asyncio.run(runtime._wait_chapter_cognition(sessions, "chapter", **kwargs))


def test_returns_true_only_after_completed():
    sessions, _db, calls = _sessions([("pending", 0), ("completed", 0)])
    assert _run(sessions) is True
    assert calls["n"] == 2, "看到 pending 不能放行"


def test_fresh_failure_is_not_requeued_yet():
    """刚失败(< 冷却)时不重投 —— 那是 autoretry 的地盘,抢了会重复同一错误。"""
    sessions, db, _calls = _sessions([("failed", 5)])
    ok = _run(sessions, timeout_seconds=0.02)
    assert ok is False
    assert db.executed == 0, "冷却期内不得 requeue"


def test_cooled_failure_is_requeued_then_can_complete():
    sessions, db, _calls = _sessions([
        ("failed", 400),      # 冷却已过 → 置回 pending
        ("pending", 0),       # beat 重投
        ("completed", 0),
    ])
    assert _run(sessions) is True
    assert db.executed == 1, "冷却满后必须重投一次"
    assert db.commits == 1, "重投要提交,否则 beat 看不到"


def test_requeue_is_bounded():
    """永久失败的章不能被无限重投。"""
    sessions, db, _calls = _sessions([("failed", 400)])
    ok = _run(sessions, timeout_seconds=0.02, requeue_limit=2)
    assert ok is False
    assert db.executed == 2, "重投次数必须受 requeue_limit 限制"


def test_obsolete_is_terminal_immediately():
    sessions, db, calls = _sessions([("obsolete", 0)])
    assert _run(sessions) is False
    assert calls["n"] == 1
    assert db.executed == 0, "obsolete 不得重投"


def test_should_stop_breaks_the_wait():
    sessions, _db, _calls = _sessions([("pending", 0)])
    stops = {"n": 0}

    async def should_stop():
        stops["n"] += 1
        return True

    ok = _run(sessions, timeout_seconds=600, should_stop=should_stop)
    assert ok is False
    assert stops["n"] == 1, "应当先问 should_stop,命中即返回"


def test_timeout_when_record_never_appears():
    sessions, _db, _calls = _sessions([None])
    assert _run(sessions, timeout_seconds=0.02) is False


def test_zero_timeout_preserves_legacy_behaviour():
    sessions, db, calls = _sessions([("pending", 0)])
    ok = _run(sessions, timeout_seconds=0)
    assert ok is True
    assert calls["n"] == 0, "禁用等待时不应查库"
    assert db.executed == 0
