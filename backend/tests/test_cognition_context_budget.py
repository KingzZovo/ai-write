"""认知上下文预算:长篇不该因为"人物多"就整章生成失败(2026-11-18)。

实测事故:gemini@2000 的一次 100 章验证,跑到 ch36 起 **连续 10 章**
`RequiredContextUnavailable: character_cognition_context_budget_exceeded`,
撞到连续错误上限后整批中断(只落了 35 章)。

根因:上游 `context_pack` 把**全书人物卡的名字+别名**整体作为 `priority_names` 传进来,
而 `render_observations` 要求"每个必选人物的**最新一条记录(含依据原文)**都必须装进
预算",预算默认只有 1200 字符 —— 人物与依据原文一多必然撑爆。

为什么不能改成"装不下就裁掉几个人":那会退化成"只给旧记录",模型会以为人物还不知道,
写出"忽然知道了/忽然忘了"的矛盾情节。这条 fail-closed 是有意的,见
`tests/test_cognition_context_selection.py::test_recalled_latest_evidence_overflow_is_explicit_instead_of_older_unknown`。

所以修法是**提高预算**(配置项 `COGNITION_HISTORY_MAX_CHARS`,默认 1200 → 3000),
并保留"装不下就显式失败",同时把"还差多少"写进日志便于继续调。
"""

from __future__ import annotations

import pytest

from app.services.cognition_context import HEADER, Observation, render_observations
from app.services.context_errors import RequiredContextUnavailable


def test_config_default_budget_is_raised():
    """预算默认值必须已经抬高;退回 1200 会让长篇再次整批中断。"""
def _rec(i: int, quote_len: int) -> Observation:
    """一条带依据原文的认知记录(引文长度可控,用来卡预算边界)。"""
    return Observation(
        i + 1, i, f"人物{i}",
        f"人物{i}·知道:第{i}章的线索。\n  依据原文:" + "引" * quote_len,
        f"k{i}",
    )


def test_default_budget_fits_every_character_latest_record():
    """20 个必选人物、每人一条带短引文的记录,在 3000 预算下全部装下且不失败。"""
    events = [_rec(i, 70) for i in range(20)]
    out = render_observations(
        events, 3000, priority_names=[f"人物{i}" for i in range(20)], context_text="",
    )
    assert out
    assert len(out) <= 3000
    # 每个必选人物的最新记录都在 —— 这正是 fail-closed 要保护的东西。
    for i in range(20):
        assert f"人物{i}·知道" in out


def test_old_budget_would_have_failed_on_the_same_input():
    """同一批内容在旧的 1200 预算下确实会失败 —— 证明是容量问题,不是逻辑问题。"""
    events = [_rec(i, 70) for i in range(20)]
    with pytest.raises(RequiredContextUnavailable, match="budget"):
        render_observations(
            events, 1200, priority_names=[f"人物{i}" for i in range(20)], context_text="",
        )

def test_single_oversized_record_still_fails_closed():
    """单条记录本身就超出整个预算 = 数据异常,这时必须停(fail-closed 不放松)。"""
    events = [Observation(1, 0, "甲", "甲·知道:" + "长" * 4000, "k")]
    with pytest.raises(RequiredContextUnavailable) as err:
        render_observations(events, 3000, priority_names=["甲"], context_text="甲推门进来。")
    assert str(err.value) == "character_cognition_context_budget_exceeded"


def test_budget_failure_is_observable_in_logs(caplog):
    """失败前必须留下"还差多少"的日志,否则无从判断该把预算调到多大。"""
    events = [Observation(1, 0, "甲", "甲·知道:" + "长" * 4000, "k")]
    with caplog.at_level("WARNING"):
        with pytest.raises(RequiredContextUnavailable):
            render_observations(events, 3000, priority_names=["甲"], context_text="甲推门进来。")
    assert any("cognition_context_budget_exceeded" in rec.message for rec in caplog.records)


def test_empty_events_returns_empty_string():
    assert render_observations([], 3000, priority_names=["甲"], context_text="") == ""


def test_header_alone_never_blows_the_budget():
    """HEADER 自身必须远小于默认预算,否则任何输入都无解。"""
    from app.config import settings

    assert len(HEADER) < settings.COGNITION_HISTORY_MAX_CHARS / 4
