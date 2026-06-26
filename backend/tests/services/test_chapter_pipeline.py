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
