from __future__ import annotations

import pytest


def test_logic_issue_and_report_shape() -> None:
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    high = LogicIssue(
        dimension="spatial_direction",
        severity="high",
        quote="通道向地下深处倾斜延伸",
        problem="既写通向地面又写向地下延伸，方向矛盾",
        fix_hint="统一为逃向地面，删去往下跑",
        locatable=True,
    )
    low = LogicIssue(
        dimension="prop_state",
        severity="low",
        quote="手机还在手里",
        problem="",
        fix_hint="",
        locatable=False,
    )
    report = LogicCriticReport(available=True, clean=False, issues=[high, low])

    assert report.high_issues == [high]
    assert report.locatable_issues == [high]
    assert report.issue_count == 2


def test_build_user_content_is_isolated() -> None:
    from app.services.logic_critic import build_logic_critic_user_content

    content = build_logic_critic_user_content(
        chapter_text="林照推开门，看见骨架。",
        chapter_outline={"title": "逃生", "summary": "主角逃离五楼"},
        prev_chapter_tail="上一章结尾：他走进楼道。",
    )
    assert "林照推开门" in content
    assert "逃生" in content or "主角逃离五楼" in content
    assert "他走进楼道" in content
    assert "空间方向" in content
    assert "画面重述" in content or "重述" in content
    assert "clean" in content
    assert "issues" in content


def test_build_user_content_tolerates_missing_optionals() -> None:
    from app.services.logic_critic import build_logic_critic_user_content

    content = build_logic_critic_user_content(
        chapter_text="正文。",
        chapter_outline=None,
        prev_chapter_tail="",
    )
    assert "正文。" in content
    assert "clean" in content


def test_parse_clean_output() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    report = parse_logic_critic_output({"issues": [], "clean": True}, chapter_text="任意正文")
    assert report.available is True
    assert report.clean is True
    assert report.issues == []


def test_parse_marks_unlocatable_quote() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    chapter = "台阶向上倾斜，通向地面层出口。他迈开步子往下跑。"
    parsed = {
        "clean": False,
        "issues": [
            {
                "dimension": "spatial_direction",
                "severity": "high",
                "quote": "他迈开步子往下跑",
                "problem": "方向矛盾",
                "fix_hint": "删去往下跑",
            },
            {
                "dimension": "span_jump",
                "severity": "high",
                "quote": "他乘电梯直达顶楼",
                "problem": "臆造",
                "fix_hint": "x",
            },
        ],
    }
    report = parse_logic_critic_output(parsed, chapter_text=chapter)
    assert report.available is True
    assert report.clean is False
    assert len(report.issues) == 2
    locatable = report.locatable_issues
    assert len(locatable) == 1
    assert locatable[0].quote == "他迈开步子往下跑"


def test_parse_clean_false_but_no_issues_is_clean() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    report = parse_logic_critic_output({"issues": [], "clean": False}, chapter_text="正文")
    assert report.clean is True


def test_parse_garbage_returns_unavailable() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    assert parse_logic_critic_output({}, chapter_text="正文").available is False
    assert parse_logic_critic_output(None, chapter_text="正文").available is False


@pytest.mark.asyncio
async def test_run_logic_critic_happy_path(monkeypatch) -> None:
    import app.services.logic_critic as lc

    async def fake_structured(task_type, user_content, db, **kwargs):
        assert task_type == "logic_critic"
        assert "本章正文" in user_content
        return {
            "clean": False,
            "issues": [
                {
                    "dimension": "spatial_direction",
                    "severity": "high",
                    "quote": "往下跑",
                    "problem": "方向矛盾",
                    "fix_hint": "删去",
                }
            ],
        }

    monkeypatch.setattr(lc, "run_structured_prompt", fake_structured)

    report = await lc.run_logic_critic(
        chapter_text="他往下跑。" + "x" * 300,
        chapter_outline=None,
        prev_chapter_tail="",
        db=object(),
        project_id="p",
        chapter_id="c",
    )
    assert report.available is True
    assert report.high_issues[0].dimension == "spatial_direction"


@pytest.mark.asyncio
async def test_run_logic_critic_degrades_on_exception(monkeypatch) -> None:
    import app.services.logic_critic as lc

    async def boom(*a, **k):
        raise RuntimeError("relay 503")

    monkeypatch.setattr(lc, "run_structured_prompt", boom)

    report = await lc.run_logic_critic(
        chapter_text="正文" + "x" * 300,
        chapter_outline=None,
        prev_chapter_tail="",
        db=object(),
        project_id="p",
        chapter_id="c",
    )
    # 异常 → 不可用 → 降级，绝不抛出。
    assert report.available is False


@pytest.mark.asyncio
async def test_run_logic_critic_skips_short_text(monkeypatch) -> None:
    import app.services.logic_critic as lc

    called = False

    async def tracker(*a, **k):
        nonlocal called
        called = True
        return {"issues": [], "clean": True}

    monkeypatch.setattr(lc, "run_structured_prompt", tracker)

    report = await lc.run_logic_critic(
        chapter_text="太短",
        chapter_outline=None,
        prev_chapter_tail="",
        db=object(),
        project_id="p",
        chapter_id="c",
    )
    # 超短稿跳过核查（无意义），不调 LLM，返回 clean+available。
    assert called is False
    assert report.available is True
    assert report.clean is True
