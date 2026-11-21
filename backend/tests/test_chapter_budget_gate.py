"""每章 token 预算护栏 + 伏笔缺铺垫事件的单元回归(2026-11-17)。

护栏的两条硬约定,必须由测试锁死:
1. 默认关闭(limit=0)时**不查库**、不改变任何既有行为;
2. 超限只产出"拒绝再生成"的事件,不动正文、不动状态。

伏笔事件同理:只在"有计划且有缺失"时才发,其余情况返回 None。
"""

from __future__ import annotations

import pytest

from app.services import chapter_budget as budget_mod
from app.services.chapter_budget import (
    ChapterBudgetVerdict,
    chapter_budget_verdict,
    resolve_limit_tokens,
)
from app.services.foreshadow_setup_check import (
    ForeshadowSetupItem,
    ForeshadowSetupReport,
    setup_event_payload,
)


class _FakeDb:
    """Records whether anything actually queried the database."""

    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, *_args, **_kwargs):  # pragma: no cover - must not run
        self.executed += 1
        raise AssertionError("budget guard must not touch the DB when disabled")


# --- resolve_limit_tokens -------------------------------------------------

def test_explicit_limit_wins_over_env(monkeypatch):
    monkeypatch.setenv(budget_mod.ENV_LIMIT, "5000")
    assert resolve_limit_tokens(1234) == 1234


def test_env_override_beats_settings_default(monkeypatch):
    monkeypatch.setenv(budget_mod.ENV_LIMIT, "7777")
    assert resolve_limit_tokens() == 7777


def test_bad_env_falls_through_to_settings(monkeypatch):
    monkeypatch.setenv(budget_mod.ENV_LIMIT, "not-a-number")
    value = resolve_limit_tokens()
    assert isinstance(value, int) and value >= 0


def test_negative_and_zero_mean_disabled(monkeypatch):
    monkeypatch.delenv(budget_mod.ENV_LIMIT, raising=False)
    assert resolve_limit_tokens(0) == 0
    assert resolve_limit_tokens(-5) == 0
    monkeypatch.setenv(budget_mod.ENV_LIMIT, "-1")
    assert resolve_limit_tokens() == 0


# --- chapter_budget_verdict ----------------------------------------------

@pytest.mark.asyncio
async def test_disabled_budget_does_not_query(monkeypatch):
    monkeypatch.delenv(budget_mod.ENV_LIMIT, raising=False)
    db = _FakeDb()
    verdict = await chapter_budget_verdict(db, "11111111-1111-4111-8111-111111111111",
                                           limit_tokens=0)
    assert verdict.exceeded is False
    assert verdict.used_tokens == 0
    assert db.executed == 0


@pytest.mark.asyncio
async def test_missing_chapter_is_not_exceeded(monkeypatch):
    monkeypatch.delenv(budget_mod.ENV_LIMIT, raising=False)
    db = _FakeDb()
    verdict = await chapter_budget_verdict(db, None, limit_tokens=100_000)
    assert verdict.chapter_id == ""
    assert verdict.exceeded is False
    assert db.executed == 0


@pytest.mark.asyncio
async def test_verdict_maps_budget_row(monkeypatch):
    async def fake_fetch(_db, *, chapter_id, limit_tokens):
        return {
            "chapter_id": str(chapter_id),
            "used_tokens": 120_000,
            "input_tokens": 100_000,
            "cached_input_tokens": 40_000,
            "output_tokens": 20_000,
            "calls": 7,
            "limit_tokens": limit_tokens,
            "exceeded": True,
            "by_step": [{"task_type": "scene_writer", "calls": 3, "total_tokens": 90_000}],
        }

    monkeypatch.setattr(budget_mod, "fetch_chapter_usage_budget", fake_fetch)
    verdict = await chapter_budget_verdict(_FakeDb(), "cid-1", limit_tokens=100_000)
    assert verdict.exceeded is True
    assert verdict.used_tokens == 120_000
    assert verdict.calls == 7
    assert verdict.remaining_tokens == 0
    assert verdict.by_step[0]["task_type"] == "scene_writer"


@pytest.mark.asyncio
async def test_verdict_below_limit_has_remaining(monkeypatch):
    async def fake_fetch(_db, *, chapter_id, limit_tokens):
        return {
            "chapter_id": str(chapter_id),
            "used_tokens": 40_000,
            "calls": 2,
            "limit_tokens": limit_tokens,
            "exceeded": False,
            "by_step": [],
        }

    monkeypatch.setattr(budget_mod, "fetch_chapter_usage_budget", fake_fetch)
    verdict = await chapter_budget_verdict(_FakeDb(), "cid-2", limit_tokens=100_000)
    assert verdict.exceeded is False
    assert verdict.remaining_tokens == 60_000


def test_event_payload_shape():
    verdict = ChapterBudgetVerdict(
        chapter_id="cid",
        used_tokens=130_000,
        limit_tokens=100_000,
        exceeded=True,
        calls=9,
        by_step=({"task_type": "rewrite", "calls": 2, "total_tokens": 30_000},),
    )
    event = verdict.as_event()
    assert event["event"] == "chapter_budget_exceeded"
    assert event["status"] == "blocked"
    assert event["used_tokens"] == 130_000
    assert event["remaining_tokens"] == 0
    assert event["by_step"] == [{"task_type": "rewrite", "calls": 2, "total_tokens": 30_000}]
    # The event must not claim anything about the saved prose.
    assert "content" not in event and "text" not in event


# --- foreshadow setup event ----------------------------------------------

def _report(items, chapter_id="cid"):
    missing = [item for item in items if not item.grounded]
    return ForeshadowSetupReport(
        chapter_id=chapter_id,
        planned=len(items),
        grounded=len(items) - len(missing),
        missing=missing,
        items=items,
    )


def test_foreshadow_event_none_when_all_grounded():
    grounded = ForeshadowSetupItem(
        plan_key="p1", title="存根", keywords=("存根",), grounded=True, evidence="…存根…"
    )
    assert setup_event_payload(_report([grounded])) is None


def test_foreshadow_event_none_without_plans():
    assert setup_event_payload(_report([])) is None
    assert setup_event_payload(None) is None


def test_foreshadow_event_reports_missing_with_cap():
    items = [
        ForeshadowSetupItem(plan_key=f"p{i}", title=f"线索{i}", keywords=(f"线索{i}",), grounded=False)
        for i in range(7)
    ]
    event = setup_event_payload(_report(items), limit=3)
    assert event is not None
    assert event["event"] == "foreshadow_setup_missing"
    assert event["status"] == "warning"
    assert event["planned"] == 7
    assert event["grounded"] == 0
    # Count is complete, the list is capped so the event stays small.
    assert event["missing_count"] == 7
    assert len(event["missing"]) == 3
    assert event["missing"][0] == {"plan_key": "p0", "title": "线索0", "keywords": ["线索0"]}


def test_foreshadow_event_partial_grounding():
    items = [
        ForeshadowSetupItem(plan_key="p1", title="钥匙", keywords=("钥匙",), grounded=True, evidence="…"),
        ForeshadowSetupItem(plan_key="p2", title="存根", keywords=("存根",), grounded=False),
    ]
    event = setup_event_payload(_report(items))
    assert event is not None
    assert (event["planned"], event["grounded"], event["missing_count"]) == (2, 1, 1)
    assert [m["plan_key"] for m in event["missing"]] == ["p2"]