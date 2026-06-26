from __future__ import annotations


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
