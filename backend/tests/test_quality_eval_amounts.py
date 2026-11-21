"""回归:金额抽取的纯函数部分(不联网)。

模型输出的字段名在不同调用间真的会变,实测同一提示下出现过四套:
``{金额,单位,原文}``、``{金额,原文,用途}``、``{amount,context}``、``{item,value,quote}``。
解析器必须都认,否则"抽到了但读不出"。

另一条实测教训:口语金额的"块/角"必须按位解析。
``四块五`` = 4.5(不是 5)、``二十块零五毛`` = 20.5(不是 25)——最初的实现两处都读错。
"""
from __future__ import annotations

import pytest

from app.services.numeric_ledger import (
    conflicts,
    drift_against,
    looks_like_total,
    parse_amount,
    parse_amounts,
    planned_amounts_from_text,
    totals_for,
)
from scripts.quality_eval import amounts as amounts_script
from scripts.quality_eval.amounts import extract_amounts


@pytest.mark.parametrize("text,expected", [
    ("八百二十", 820),
    ("八百四十块整", 840),
    ("二十八块", 28),
    ("八百块钱", 800),
    ("八百块", 800),
    ("九百", 900),
    ("一千二", 1200),
    ("每套二十八块", 28),
    ("800", 800),
    ("820 元", 820),
    # 口语小数:按位解析,不是把数字拼起来。
    ("四块五", 4.5),
    ("二十块零五毛", 20.5),
    ("一元二角", 1.2),
    # 名目被抽成碎片时不能给出数字(正则版就是在这里出错的)。
    ("呢", None),
    ("的铺租", None),
    ("", None),
    (None, None),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("raw,expected_count", [
    ('[{"金额":800,"单位":"元","原文":"那八百块钱的铺面租金"}]', 1),
    ('[{"金额":"八百块钱","原文":"这笔工钱","用途":"铺面租金"}]', 1),
    ('[{"amount": 800, "context": "那八百块钱"}]', 1),
    ('[{"item":"每套校服工钱","value":"二十八块","quote":"每套校服的工钱是二十八块"}]', 1),
    ('```json\n[{"amount":840,"context":"一共是八百四十块整"}]\n```', 1),
    ("没有金额", None),
    ("[]", 0),
])
def test_parse_amounts_accepts_every_observed_schema(raw, expected_count):
    rows = parse_amounts(raw)
    if expected_count is None:
        assert rows is None
    else:
        assert rows is not None and len(rows) == expected_count


def test_parse_amounts_normalizes_amount():
    rows = parse_amounts('[{"item":"校服工钱总额","value":"八百二十","quote":"总共是八百二十块钱"}]')
    assert rows[0]["amount"] == 820
    assert rows[0]["item"] == "校服工钱总额"


@pytest.mark.parametrize("item,total", [
    ("校服工钱总额", True),
    ("三十套校服总工钱", True),
    ("一共八百四十块", True),
    # 单价不是总额:这条区分正是正则做不到的。
    ("每套校服工钱", False),
    ("每套单价", False),
    ("校服工钱", False),
])
def test_looks_like_total(item, total):
    assert looks_like_total(item) is total


def test_totals_for_prefers_total_over_unit_price():
    rows = parse_amounts(
        '[{"item":"每套校服工钱","value":"二十八块","quote":"每套校服的工钱是二十八块"},'
        ' {"item":"校服工钱总额","value":"八百二十","quote":"总共是八百二十块钱"}]'
    )
    assert [r["amount"] for r in totals_for(rows, "工钱")] == [820]


def test_totals_for_falls_back_to_any_hit_when_no_total():
    rows = parse_amounts('[{"item":"铺租","value":"八百块","quote":"今天到期的八百块铺租"}]')
    assert [r["amount"] for r in totals_for(rows, "铺租")] == [800]


class _FakeClient:
    """不联网的假客户端:按顺序返回预置内容,记录调用参数。"""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents[min(len(self.calls) - 1, len(self.contents) - 1)]
        message = type("M", (), {"content": content})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice]})()


def test_extract_amounts_uses_given_model_and_returns_rows():
    client = _FakeClient(['[{"item":"校服工钱总额","value":"八百四十","quote":"一共是八百四十块整"}]'])
    rows, raw = extract_amounts(client, model="deepseek-chat", text="正文", max_tokens=1500)
    assert client.calls[0]["model"] == "deepseek-chat"
    assert client.calls[0]["max_tokens"] == 1500
    assert rows[0]["amount"] == 840
    assert raw.startswith("[")


def test_empty_output_is_retried_and_never_read_as_no_amounts():
    """实测:推理模型会把整段预算花在思考上、返回空正文。空 != 没有金额。"""
    client = _FakeClient(["", '  ', '[{"item":"校服工钱总额","value":"八百二十","quote":"总共是八百二十块钱"}]'])
    rows, _ = extract_amounts(client, model="m", text="正文", retries=2)
    assert len(client.calls) == 3
    assert [r["amount"] for r in rows] == [820]


def test_persistent_empty_output_returns_none():
    client = _FakeClient(["", ""])
    rows, raw = extract_amounts(client, model="m", text="正文", retries=1)
    assert rows is None and raw == ""
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# 单一实现:纯函数在产品层,脚本只做搬运(不允许第二套解析)
# ---------------------------------------------------------------------------


def test_script_reuses_the_product_layer_implementation():
    """脚本导出的必须是产品层的同一函数对象,不是复制出来的第二份。"""
    import app.services.numeric_ledger as ledger

    assert amounts_script.parse_amount is ledger.parse_amount
    assert amounts_script.parse_amounts is ledger.parse_amounts
    assert amounts_script.looks_like_total is ledger.looks_like_total
    assert amounts_script.totals_for is ledger.totals_for


# ---------------------------------------------------------------------------
# 与"计划已定的数"比对(写后核对 warning 的核心判定)
# ---------------------------------------------------------------------------


def test_planned_amounts_from_text_reads_outline_fragments():
    text = "[CH-8] 周芸收下八百二。铺租八百付掉后只剩二十。"
    assert planned_amounts_from_text(text) == {820.0, 800, 20}


def test_conflicts_flags_written_total_absent_from_plan():
    rows = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百四十","quote":"一共是八百四十块整"}]'
    )
    found = conflicts(rows, {820.0, 800, 20})
    assert [row["written"] for row in found] == [840]
    assert found[0]["item"] == "校服工钱总额"
    assert found[0]["quote"] == "一共是八百四十块整"


def test_conflicts_ignores_matching_total_and_unit_prices():
    rows = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百二十","quote":"总共是八百二十块钱"},'
        ' {"item":"每套单价","value":"二十八块","quote":"每套二十八块"}]'
    )
    # 总额 820 在计划里 → 不报;单价 28 不参与总额比对 → 也不报。
    assert conflicts(rows, {820.0, 800, 20}) == []


def test_conflicts_never_fires_without_a_plan():
    """计划为空一律不判:否则每章都会凭空报警。"""
    rows = parse_amounts('[{"item":"货款总额","value":"七百元","quote":"七百元货款"}]')
    assert conflicts(rows, set()) == []


def test_conflicts_are_capped():
    entries = ",".join(
        '{"item":"货款总额%d","value":"%d元","quote":"q%d"}' % (i, 100 + i, i)
        for i in range(10)
    )
    rows = parse_amounts(f"[{entries}]")
    assert len(conflicts(rows, {1.0})) == 5


# ---------------------------------------------------------------------------
# 跨章漂移指标(compare_arms --amounts 的聚合口径)
# ---------------------------------------------------------------------------


def test_drift_against_flags_totals_absent_from_the_later_chapter():
    """实测形态:ch07 自造 840,ch08 写 820 → 漂移 1 条。"""
    ch07 = parse_amounts('[{"item":"校服工钱总额","value":"八百四十","quote":"q"}]')
    ch08 = parse_amounts('[{"item":"校服工钱总额","value":"八百二十","quote":"q"}]')
    assert [row["written"] for row in drift_against(ch07, ch08)] == [840]


def test_drift_against_is_empty_when_totals_agree():
    ch07 = parse_amounts('[{"item":"校服工钱总额","value":"八百二十","quote":"q"}]')
    ch08 = parse_amounts('[{"item":"校服工钱总额","value":"八百二十块","quote":"q"}]')
    assert drift_against(ch07, ch08) == []


def test_report_drift_counts_and_prints(monkeypatch, capsys):
    """聚合与输出:一条冲突 → 漂移条数=1。用假抽取器,不联网。"""
    import argparse

    from scripts.quality_eval import amounts as amounts_mod
    from scripts.quality_eval import compare_arms as compare_mod

    def fake_extract(client, *, model, text, max_tokens, retries):
        value = "八百四十" if text == "前一章" else "八百二十"
        return parse_amounts('[{"item":"校服工钱总额","value":"%s","quote":"q"}]' % value), ""

    monkeypatch.setattr(amounts_mod, "extract_amounts", fake_extract)
    args = argparse.Namespace(model="m", max_tokens=1, retries=0,
                             api_key="k", endpoint="http://127.0.0.1:1/v1")
    compare_mod._report_drift([{"label": "A", "texts": {7: "前一章", 8: "后一章"}}], [7, 8], args)
    out = capsys.readouterr().out
    assert "冲突=1" in out
    assert "漂移条数=1" in out

from app.services.numeric_ledger import ledger_conflicts, normalize_item, same_item


# ---------------------------------------------------------------------------
# 按名目的金额台账(写后核对的新口径,纯函数在 app/services/numeric_ledger.py)
# ---------------------------------------------------------------------------
# 实测(2026-11-17)旧口径 3 条命中里 2 条是误报:ch08 的 820 就写在**本章大纲**里、
# ch18 的 3000 早在 ch10 **正文**里写过。两者都不是"与已定数冲突",而是
# "计划/摘要没记这个数"。台账把"实际写出过的金额"按名目登记下来,才分得开这两类。


def test_ledger_rule_kills_the_observed_false_positives():
    ledger = [{"item": "垫了总数", "amount": 3000, "chapter_idx": 10},
              {"item": "校服工钱总额", "amount": 820, "chapter_idx": 8}]
    # ch18:写的 3000 与 ch10 登记过的 3000 一致 → 不报(旧口径会报,它在计划里找不到)。
    ch18 = parse_amounts('[{"item":"垫了总数","value":"三千","quote":"先垫了三千"}]')
    assert [row["written"] for row in conflicts(ch18, {800.0, 20})] == [3000]
    assert ledger_conflicts(ch18, ledger, {800.0, 20}) == []
    # ch08:写的 820 与 ch08 自己那一条登记一致 → 不报。
    ch08 = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百二十","quote":"周芸收下八百二"}]')
    assert ledger_conflicts(ch08, ledger, {800.0, 20}) == []


def test_ledger_rule_reports_the_same_item_with_a_different_amount():
    """真阳性形态:同一名目此前登记 820,这一章写 840 —— 这是"对不上",不是"新引入"。"""
    ledger = [{"item": "校服工钱总额", "amount": 820, "chapter_idx": 8}]
    rows = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百四十","quote":"一共是八百四十块整"}]')
    found = ledger_conflicts(rows, ledger, {820.0, 800.0, 20})
    assert [row["written"] for row in found] == [840]
    assert found[0]["ledger_amounts"] == [820.0]
    assert found[0]["ledger_chapters"] == [8]


def test_ledger_item_matching_is_conservative():
    """名目匹配要保守:只有共享 ≥2 个连续汉字才算同名目,分类词不参与匹配。"""
    assert same_item("校服工钱总额", "三十套校服总工钱") is True
    assert same_item("铺租总额", "工钱总额") is False
    assert same_item("铺租", "工钱") is False
    assert normalize_item("校服工钱总额") == "校服工钱"
