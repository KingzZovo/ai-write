from __future__ import annotations

import pytest

from app.services.logic_critic import LogicIssue


def test_build_targeted_rewrite_content_lists_only_locatable() -> None:
    from app.services.chapter_pipeline import build_targeted_rewrite_content

    issues = [
        LogicIssue("spatial_direction", "high", "往下跑", "方向矛盾", "删去往下跑", True),
        LogicIssue("span_jump", "high", "臆造片段", "x", "y", False),  # 不该出现
    ]
    content = build_targeted_rewrite_content("原文正文……往下跑……", issues)
    assert "往下跑" in content
    assert "删去往下跑" in content
    assert "臆造片段" not in content   # unlocatable 不进改写指令
    assert "原文正文" in content        # 含被改全文


@pytest.mark.asyncio
async def test_apply_targeted_rewrite_returns_text(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from types import SimpleNamespace

    async def fake_text_prompt(task_type, user_content, db, **kwargs):
        assert task_type == "drafter"
        return SimpleNamespace(text="改写后的正文")

    monkeypatch.setattr(cp, "run_text_prompt", fake_text_prompt)

    issues = [LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)]
    out = await cp.apply_targeted_logic_rewrite(
        text="原文往下跑", issues=issues, db=object(), project_id="p", chapter_id="c"
    )
    assert out == "改写后的正文"


@pytest.mark.asyncio
async def test_apply_targeted_rewrite_rejects_collapsed_draft(monkeypatch) -> None:
    """定向改写若把整章塌缩成残片（drafter 只回片段、丢弃其余），
    必须拒绝并返回 None，保留上一稿；否则 persist-on-block 会把残片当成稿存库。"""
    import app.services.chapter_pipeline as cp
    from types import SimpleNamespace

    original = "原文正文。" * 400  # ~2400 chars 的完整章

    async def collapsing_drafter(task_type, user_content, db, **kwargs):
        # 模拟 drafter 只回了被点名片段附近的一小段（线上 ch2/ch3 真实故障形态）。
        return SimpleNamespace(text="只改了开头这一小段往下跑。")

    monkeypatch.setattr(cp, "run_text_prompt", collapsing_drafter)

    issues = [LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)]
    out = await cp.apply_targeted_logic_rewrite(
        text=original, issues=issues, db=object(), project_id="p", chapter_id="c"
    )
    assert out is None  # 残片被拒，调用方保留 original


@pytest.mark.asyncio
async def test_apply_targeted_rewrite_accepts_minor_trim(monkeypatch) -> None:
    """合法的定向改写允许小幅缩短（删掉矛盾短语），不应被长度门误杀。"""
    import app.services.chapter_pipeline as cp
    from types import SimpleNamespace

    original = "原文正文。" * 400

    async def minor_editor(task_type, user_content, db, **kwargs):
        return SimpleNamespace(text="原文正文。" * 380)  # 95% 保留

    monkeypatch.setattr(cp, "run_text_prompt", minor_editor)

    issues = [LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)]
    out = await cp.apply_targeted_logic_rewrite(
        text=original, issues=issues, db=object(), project_id="p", chapter_id="c"
    )
    assert out is not None
    assert out.startswith("原文正文。")


@pytest.mark.asyncio
async def test_apply_targeted_rewrite_degrades_to_none(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp

    async def boom(*a, **k):
        raise RuntimeError("relay down")

    monkeypatch.setattr(cp, "run_text_prompt", boom)

    issues = [LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)]
    out = await cp.apply_targeted_logic_rewrite(
        text="原文", issues=issues, db=object(), project_id="p", chapter_id="c"
    )
    assert out is None  # 失败返回 None（保留上一稿）


def test_pipeline_result_echo_report() -> None:
    from app.services.chapter_pipeline import ChapterPipelineResult
    from types import SimpleNamespace

    qg = SimpleNamespace(status="passed", final_text="终稿", to_safe_metadata=lambda: {"status": "passed"})
    res = ChapterPipelineResult(
        final_text="终稿",
        quality_gate_result=qg,
        logic_rounds=1,
        logic_issues_remaining=0,
        logic_available=True,
    )
    echo = res.to_echo_report()
    # echo 只含约定字段，不含中间稿/角色推理。
    assert echo == {
        "logic_rounds": 1,
        "logic_issues_remaining": 0,
        "logic_available": True,
        "prose_gate_status": "passed",
    }
    assert "intermediate_text" not in echo
    assert "issues" not in echo


@pytest.mark.asyncio
async def test_pipeline_disabled_delegates_to_quality_gate(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from types import SimpleNamespace

    monkeypatch.setattr(cp, "_pipeline_enabled", lambda: False)

    qg = SimpleNamespace(status="passed", final_text="QG终稿",
                         to_safe_metadata=lambda: {"status": "passed"})
    seen = {}

    async def fake_gate(**kwargs):
        seen.update(kwargs)
        return qg

    monkeypatch.setattr(cp, "apply_chapter_quality_gate", fake_gate)

    # logic_critic 不应被调用（开关关闭）。
    async def must_not_call(*a, **k):
        raise AssertionError("logic_critic must not run when pipeline disabled")

    monkeypatch.setattr(cp, "run_logic_critic", must_not_call)

    res = await cp.run_chapter_pipeline(
        text="初稿正文", db=object(), project_id="p", chapter_id="c",
        target_word_count=3000, chapter_outline=None, prev_chapter_tail="",
    )
    assert res.final_text == "QG终稿"
    assert res.logic_available is False
    assert res.logic_rounds == 0
    assert seen["text"] == "初稿正文"
    assert seen["target_word_count"] == 3000


def _qg(final_text="终稿", status="passed"):
    from types import SimpleNamespace
    return SimpleNamespace(status=status, final_text=final_text,
                           to_safe_metadata=lambda: {"status": status})


@pytest.mark.asyncio
async def test_clean_draft_skips_rewrite(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport

    monkeypatch.setattr(cp, "_pipeline_enabled", lambda: True)
    monkeypatch.setattr(cp, "_max_logic_rounds", lambda: 2)

    async def clean_critic(**k):
        return LogicCriticReport(available=True, clean=True, issues=[])

    rewrite_calls = 0

    async def count_rewrite(**k):
        nonlocal rewrite_calls
        rewrite_calls += 1
        return "不该被调用"

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", clean_critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", count_rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert rewrite_calls == 0
    assert res.logic_rounds == 0
    assert res.logic_issues_remaining == 0
    assert res.logic_available is True


@pytest.mark.asyncio
async def test_high_issue_rewrites_then_verifies_clean(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    monkeypatch.setattr(cp, "_pipeline_enabled", lambda: True)
    monkeypatch.setattr(cp, "_max_logic_rounds", lambda: 2)

    issue = LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)
    seq = [
        LogicCriticReport(available=True, clean=False, issues=[issue]),
        LogicCriticReport(available=True, clean=True, issues=[]),
    ]

    async def critic(**k):
        return seq.pop(0)

    async def rewrite(**k):
        return "改写后正文" + "y" * 500

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert res.logic_rounds == 1
    assert res.logic_issues_remaining == 0
    assert res.final_text.startswith("改写后正文")


@pytest.mark.asyncio
async def test_plateau_stops_loop(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    monkeypatch.setattr(cp, "_pipeline_enabled", lambda: True)
    monkeypatch.setattr(cp, "_max_logic_rounds", lambda: 3)

    issue = LogicIssue("span_jump", "high", "跨度", "突变", "补衔接", True)

    async def critic(**k):
        return LogicCriticReport(available=True, clean=False, issues=[issue])

    rewrite_calls = 0

    async def rewrite(**k):
        nonlocal rewrite_calls
        rewrite_calls += 1
        return "改" + "z" * 500

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert rewrite_calls == 1
    assert res.logic_issues_remaining == 1


@pytest.mark.asyncio
async def test_critic_unavailable_degrades(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport

    monkeypatch.setattr(cp, "_pipeline_enabled", lambda: True)

    async def down_critic(**k):
        return LogicCriticReport(available=False, clean=False, issues=[])

    async def rewrite(**k):
        raise AssertionError("must not rewrite when critic unavailable")

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", down_critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert res.logic_available is False
    assert res.logic_rounds == 0
    assert res.final_text == "x" * 500


@pytest.mark.asyncio
async def test_max_rounds_cap(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    monkeypatch.setattr(cp, "_pipeline_enabled", lambda: True)
    monkeypatch.setattr(cp, "_max_logic_rounds", lambda: 2)

    reports = [
        LogicCriticReport(available=True, clean=False, issues=[
            LogicIssue("span_jump", "high", f"q{n}", "p", "f", True) for n in range(k)
        ]) for k in (3, 2, 1)
    ]

    async def critic(**k):
        return reports.pop(0)

    rewrite_calls = 0

    async def rewrite(**k):
        nonlocal rewrite_calls
        rewrite_calls += 1
        return "改" + "w" * 500

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert rewrite_calls == 2
    assert res.logic_rounds == 2


def test_pipeline_env_defaults_pinned_for_tests() -> None:
    import os
    # conftest 必须在 import 前钉死，保证全套跑时确定（参照 CHAPTER_MAX_REWRITE_ROUNDS）。
    assert os.environ.get("LOGIC_CRITIC_MAX_ROUNDS") == "2"
    assert os.environ.get("CHAPTER_PIPELINE_ENABLED") == "1"
