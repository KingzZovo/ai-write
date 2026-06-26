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
