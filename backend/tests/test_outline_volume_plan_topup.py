"""卷数不足的确定性兜底 + 分卷规划一致性判定(2026-11-21)。

真实链路暴露两个缺陷:

1. 模型偶尔无视 scale 指令只规划 1 卷 —— 而 ``volume_creation._target`` 要求
   卷号 1..N 连续,缺卷会让第 2 卷起的卷纲永远生成不了(用户看到的现象是
   "第 2 卷报错,书纲保存失败")。
2. 一致性判定用「全文『第X卷』提及次数 >= len(plan) * 16」判"多套方案",
   卷数少时阈值低得离谱(实测 plan=1 时阈值仅 32),正文正常描述多卷就被
   判成残留多套方案 → 书纲永远保存不了。
"""
from __future__ import annotations

import json

from app.services.outline_consistency_gate import check_outline_consistency
from app.services.outline_generator import OutlineGenerator, resolve_chapters_per_volume_pref


def _generator() -> OutlineGenerator:
    return OutlineGenerator.__new__(OutlineGenerator)


def _block(volumes: list[dict]) -> str:
    return "<volume-plan>" + json.dumps(volumes, ensure_ascii=False) + "</volume-plan>"


SKELETON = "一、书名与核心概念\n书名:《白蜡树下》"


def test_top_up_fills_missing_volumes_to_scale():
    text = SKELETON + "\n\n" + _block([{"idx": 1, "title": "第一卷", "est_chapters": 100}])
    out = _generator()._top_up_volume_plan(text, {"n_volumes": 5, "chapters_per_volume": 100})
    plan = OutlineGenerator._extract_volume_plan(out)
    assert [item["idx"] for item in plan] == [1, 2, 3, 4, 5]
    assert all(item["est_chapters"] > 0 for item in plan)
    # 模型已给出的卷必须原样保留,只补缺的。
    assert plan[0]["title"] == "第一卷"


def test_top_up_is_noop_when_volumes_sufficient():
    text = SKELETON + "\n\n" + _block([
        {"idx": 1, "title": "一", "est_chapters": 10},
        {"idx": 2, "title": "二", "est_chapters": 10},
    ])
    assert _generator()._top_up_volume_plan(text, {"n_volumes": 2, "chapters_per_volume": 10}) == text


def test_top_up_noop_without_scale():
    text = SKELETON
    assert _generator()._top_up_volume_plan(text, None) == text
    assert _generator()._top_up_volume_plan(text, {}) == text


def test_top_up_noop_without_any_volume_plan():
    """模型一个卷都没给时不由这里补 —— 那种情况 stage A 校验直接判不合格。"""
    assert _generator()._top_up_volume_plan(SKELETON, {"n_volumes": 3}) == SKELETON


def test_topped_up_plan_shapes_up_for_volume_creation():
    """补齐后卷号 1..N 连续、章数为正整数 —— volume_creation._target 的前提。"""
    text = SKELETON + "\n\n" + _block([{"idx": 1, "title": "第一卷", "est_chapters": 100}])
    out = _generator()._top_up_volume_plan(text, {"n_volumes": 5, "chapters_per_volume": 100})
    plan = OutlineGenerator._extract_volume_plan(out)
    indices = [item["idx"] for item in plan]
    assert sorted(indices) == list(range(1, len(plan) + 1))
    assert all(type(item["est_chapters"]) is int and item["est_chapters"] > 0 for item in plan)


def test_top_up_label_is_stripped_from_visible_text():
    """补齐用的控制标签不得留在用户可见正文里。"""
    text = SKELETON + "\n\n" + _block([{"idx": 1, "title": "第一卷", "est_chapters": 100}])
    out = _generator()._top_up_volume_plan(text, {"n_volumes": 3, "chapters_per_volume": 100})
    visible = OutlineGenerator._strip_volume_plan_tags(out)
    assert "<volume-plan>" not in visible
    assert "一、书名与核心概念" in visible


def test_gate_allows_repeated_mention_of_same_volume():
    """同一卷号在标题/概述里被正常重复提及,不是多套方案。"""
    payload = {
        "volume_plan": [{"idx": 1, "title": "第一卷", "est_chapters": 100}],
        "raw_text": "七、分卷规划\n第一卷 垃圾站里的招生简章\n第一卷 概述:林默在垃圾站捡到招生简章。\n",
    }
    report = check_outline_consistency(payload, level="book")
    assert "possible_multiple_volume_plans" not in [i.code for i in report.issues]


def test_gate_flags_wrapped_volume_numbers():
    """卷号回绕(1,2,3 之后又出现 1,2,3)才是残留多套方案。"""
    payload = {
        "volume_plan": [
            {"idx": 1, "title": "A", "est_chapters": 10},
            {"idx": 2, "title": "B", "est_chapters": 10},
            {"idx": 3, "title": "C", "est_chapters": 10},
        ],
        "raw_text": (
            "七、分卷规划\n第一卷 a\n第二卷 b\n第三卷 c\n"
            "第一卷 a\n第二卷 b\n第三卷 c\n"
        ),
    }
    report = check_outline_consistency(payload, level="book")
    assert "possible_multiple_volume_plans" in [i.code for i in report.issues]


def test_gate_ignores_volume_mentions_outside_planning_section():
    """分卷规划节之外的「第X卷」提及(如正文概述)不参与判定。"""
    payload = {
        "volume_plan": [{"idx": 1, "title": "第一卷", "est_chapters": 100}],
        "raw_text": (
            "七、分卷规划\n第一卷 招生简章\n"
            "八、结局\n他把母亲的名字从死者名单上划掉;那一幕同时收回第一卷埋的钩子。\n"
        ),
    }
    report = check_outline_consistency(payload, level="book")
    assert "possible_multiple_volume_plans" not in [i.code for i in report.issues]

def test_gate_allows_back_reference_to_earlier_volume():
    """"第二卷承接第一卷的钩子" 这类正常引用(1,2,1)不是多套方案。

    上一版判定用"卷号下降即回绕",会把这种正文判成残留多套 → 书纲存不下。
    """
    payload = {
        "volume_plan": [
            {"idx": 1, "title": "A", "est_chapters": 10},
            {"idx": 2, "title": "B", "est_chapters": 10},
        ],
        "raw_text": (
            "七、分卷规划\n第一卷 a\n"
            "第二卷 b,承接第一卷的钩子(第一卷到第二卷是一条主线)\n"
        ),
    }
    report = check_outline_consistency(payload, level="book")
    assert "possible_multiple_volume_plans" not in [i.code for i in report.issues]


def test_multi_block_without_idx_does_not_collapse():
    """模型每卷一整块、块内不带 idx 时,合并后应是 2 条而不是 1 条。"""
    text = (
        SKELETON
        + "\n\n" + _block([{"title": "第一卷", "est_chapters": 10}])
        + "\n\n" + _block([{"title": "第二卷", "est_chapters": 20}])
    )
    plan = OutlineGenerator._extract_volume_plan(text)
    assert [item["idx"] for item in plan] == [1, 2]
    assert [item["est_chapters"] for item in plan] == [10, 20]


def test_multi_block_fills_missing_fields_across_blocks():
    """后一块补上前一块缺的 est_chapters,不再被 setdefault 整条丢掉。"""
    text = (
        SKELETON
        + "\n\n" + _block([{"idx": 1, "title": "第一卷"}])
        + "\n\n" + _block([{"idx": 1, "est_chapters": 30}])
    )
    plan = OutlineGenerator._extract_volume_plan(text)
    assert len(plan) == 1
    assert plan[0]["title"] == "第一卷"
    assert plan[0]["est_chapters"] == 30


def test_non_numeric_est_chapters_does_not_raise():
    """模型写 "约100章" 时按缺省处理,不能让 int() 冒出去盖掉可读错误。"""
    text = SKELETON + "\n\n" + _block([{"idx": 1, "title": "第一卷", "est_chapters": "约100章"}])
    plan = OutlineGenerator._extract_volume_plan(text)
    assert plan[0]["est_chapters"] == 10


def test_chapters_per_volume_alias_is_not_clamped():
    """简写键是"每卷多少章"的真值,不能被旧三键的 50 上限夹掉(否则 500 章缩成 250)。"""
    assert resolve_chapters_per_volume_pref({"chapters_per_volume": 100}) == (100, 100)


def test_explicit_three_keys_keep_their_own_clamp():
    """旧三键的 [1,50] 语义不变。"""
    assert resolve_chapters_per_volume_pref({"chapters_per_volume_min": 100}) == (50, 50)
