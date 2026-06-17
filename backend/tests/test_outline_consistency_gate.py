from app.services.outline_consistency_gate import (
    check_outline_consistency,
    validate_outline_consistency,
    OutlineConsistencyError,
)


def test_outline_consistency_passes_clean_payload():
    payload = {
        "raw_text": """
一、书名与核心概念
主角林照在东港市旧环十二区遭遇回声塌陷。

二、主要角色与小传
主角：林照，男，18岁。导师：沈听澜，暗面学院外勤导师。父亲：林观澜。母亲：叶清。

七、分卷规划
第一卷写林照进入暗面学院，第二卷写清除署压力升级。
""",
        "volume_plan": [
            {"idx": 1, "title": "血脉苏醒", "theme": "觉醒", "core_conflict": "逃生", "est_chapters": 10},
            {"idx": 2, "title": "暗面学院", "theme": "训练", "core_conflict": "追捕", "est_chapters": 10},
        ],
    }

    report = validate_outline_consistency(payload, level="book")

    assert report.ok is True
    assert report.facts["role_names"]["protagonist"] == ["林照"]


def test_outline_consistency_blocks_role_name_drift():
    payload = {
        "raw_text": """
二、主要角色与小传
主角：林照，男，18岁，住在旧环十二区。
后续设定中主角：林烨，男，18岁，住在云港市。
导师：沈听澜。导师：沈知夏。
父亲：林观澜。父亲：林远山。
母亲：叶清。母亲：苏晚晴。
""",
        "volume_plan": [
            {"idx": 1, "title": "第一卷", "theme": "觉醒", "core_conflict": "逃生", "est_chapters": 10},
        ],
    }

    report = check_outline_consistency(payload, level="book")

    assert report.ok is False
    role_conflicts = [issue for issue in report.issues if issue.code == "role_name_conflict"]
    assert {issue.details["role"] for issue in role_conflicts} >= {"protagonist", "mentor", "father", "mother"}


def test_outline_consistency_blocks_alternative_plan_and_duplicate_volume_idx():
    payload = {
        "raw_text": """
七、分卷规划
方案一：第一卷觉醒，第二卷学院。
方案二：第一卷电梯事故，第二卷监察会。
""",
        "volume_plan": [
            {"idx": 1, "title": "觉醒", "theme": "觉醒", "core_conflict": "逃生", "est_chapters": 10},
            {"idx": 1, "title": "学院", "theme": "训练", "core_conflict": "追捕", "est_chapters": 0},
        ],
    }

    report = check_outline_consistency(payload, level="book")

    assert report.ok is False
    codes = {issue.code for issue in report.issues}
    assert "alternative_plan_residue" in codes
    assert "volume_idx_duplicate" in codes
    assert "volume_chapters_invalid" in codes


def test_validate_outline_consistency_raises_with_report():
    payload = {
        "raw_text": "<volume-plan>[]</volume-plan>",
        "volume_plan": [],
    }

    try:
        validate_outline_consistency(payload, level="book")
    except OutlineConsistencyError as exc:
        assert exc.report.ok is False
        assert any(issue.code == "volume_plan_tags_visible" for issue in exc.report.issues)
    else:
        raise AssertionError("expected OutlineConsistencyError")


def test_shenyi_payload_requires_structural_logic_anchors():
    payload = {
        "title": "神裔",
        "raw_text": """
《神裔》
主角林照在旧环十二区被沈听澜救出。纸质追查账保留父母线索。
暗面学院担保林照，旧环十二区因拆迁停滞成为异常高发区。
第二卷发现档案缺页，第三卷进入旧神残响区。
""",
        "volume_plan": [
            {"idx": 1, "title": "血脉苏醒", "theme": "觉醒", "core_conflict": "逃生", "est_chapters": 150},
        ],
    }

    report = check_outline_consistency(payload, level="book")

    assert report.ok is False
    codes = {issue.code for issue in report.issues}
    assert "semantic_anchor_encoding" in codes
    assert "academy_bargaining_motive" in codes
    assert "old_district_physical_basis" in codes
    assert "evidence_decay_catalyst" in codes
    assert "interface_tactical_actions" in codes
    assert "birth_record_fatality" in codes
    assert "daily_reality_routes" in codes


def test_shenyi_payload_passes_when_structural_logic_anchors_are_present():
    payload = {
        "title": "神裔",
        "raw_text": """
《神裔》
主角林照在旧环十二区被沈听澜救出。
快递单背面维修节点图不写直白文字推演，而被转译成老楼电路布线图、管网维修单、阻值与节点图。
林照的异常滤波接口让他成为可过滤旧神残骸频率的活体节点，裴玄真借此反向解析清除署协议并争取学院自治权。
战术动作必须是频率滤波、定位协议空拍、制造权限错位和三秒延迟。
出生记录一旦被清空，林照会身份索引归零，变成无主接口并被无名神核强行接管。
旧环十二区地下连接被污染的旧式城市管网，清除署不敢让重型机械破坏地基，以免触发链式空间折叠。
第二、三卷持续保留快递路线、维修工单、地下管网巡检与物理泄漏点。
第二卷末物理锚点触发协议底层自检，形成证据衰变与反向追踪，旧楼和锚点面临物理抹除。
""",
        "entity_registry": {"protagonist": "林照", "mentor": "沈听澜", "origin_location": "旧环十二区"},
        "volume_plan": [
            {"idx": 1, "title": "血脉苏醒", "theme": "觉醒", "core_conflict": "逃生", "est_chapters": 150},
        ],
    }

    report = check_outline_consistency(payload, level="book")

    assert report.ok is True
