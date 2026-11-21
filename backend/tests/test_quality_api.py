from app.services.chinese_prose_mechanics_checker import analyze_chinese_prose_mechanics


def test_chinese_prose_mechanics_report_contains_manual_audit_metrics() -> None:
    text = "少年喊了声师傅。他把来意说得很低。门缝里透不出灯。"

    report = analyze_chinese_prose_mechanics(text).to_safe_dict()

    assert report["pseudo_literary_register_count"] >= 2
    assert report["semantic_collocation_count"] >= 1
    assert report["plain_contemporary_violation_count"] >= 3
    assert "duplicate_explanation_span_count" in report
