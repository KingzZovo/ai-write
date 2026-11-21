"""回归:写后账目核对(步骤 1)必须"记账但不阻断"。

四条硬约束:
1. 写出的总额不在计划里 → 记进 ``numeric_conflicts``(并通过 ``to_safe_metadata`` 落库);
2. 一致 → 不记;
3. 抽取抛错 / 返回空 → 不记、不抛、不改 status;
4. **计划为空时一次模型调用都不发生**(零成本边界)。

第 4 条尤其重要:如果计划没定数还去调模型,每个不含金额的章节都会白烧一次调用。
"""
from __future__ import annotations

import pytest

from app.services.chapter_quality_gate import ChapterQualityGateResult
from app.services.chinese_prose_mechanics_checker import analyze_chinese_prose_mechanics
from app.services.numeric_check import (
    attach_numeric_conflicts,
    check_chapter_amounts,
    resolve_plan_text,
)
from app.services.numeric_ledger import parse_amounts

PLAN = "[CH-8] 周芸收下八百二。铺租八百付掉后只剩二十。"


class _Extractor:
    """假抽取器:记录调用次数,可按需抛错。"""

    def __init__(self, rows=None, error=None):
        self.rows = rows
        self.error = error
        self.calls = 0

    async def __call__(self, text):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


def _gate_result(text: str) -> ChapterQualityGateResult:
    report = analyze_chinese_prose_mechanics(text)
    return ChapterQualityGateResult(
        status="passed", original_text=text, final_text=text,
        initial_report=report, final_report=report,
    )


def test_conflicting_total_is_recorded():
    rows = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百四十","quote":"一共是八百四十块整"}]')
    result = _gate_result("一共是八百四十块整。")
    extractor = _Extractor(rows)

    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PLAN, extractor=extractor))

    assert [row["written"] for row in found] == [840]
    assert result.numeric_conflicts == found
    assert result.status == "passed", "核对绝不能改 status"
    assert result.to_safe_metadata()["numeric_conflicts"][0]["written"] == 840


def test_matching_total_is_not_recorded():
    rows = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百二十","quote":"总共是八百二十块钱"}]')
    result = _gate_result("总共是八百二十块钱。")

    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PLAN, extractor=_Extractor(rows)))

    assert found == []
    assert result.numeric_conflicts == []
    # 字段必须始终存在,元数据里是空列表而不是缺键。
    assert result.to_safe_metadata()["numeric_conflicts"] == []


@pytest.mark.parametrize("extractor", [
    _Extractor(error=RuntimeError("model exploded")),
    _Extractor(rows=None),
])
def test_extraction_failure_is_swallowed(extractor):
    result = _gate_result("总共是八百二十块钱。")

    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PLAN, extractor=extractor))

    assert found == [] and result.numeric_conflicts == []
    assert result.status == "passed"


def test_no_model_call_at_all_without_a_plan():
    """计划里没有任何金额 → 不调用模型(这是成本边界,不是优化)。"""
    result = _gate_result("这一章没有谈钱。")
    extractor = _Extractor(rows=[])

    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text="", extractor=extractor))

    assert found == [] and extractor.calls == 0


def test_check_chapter_amounts_returns_empty_for_empty_text():
    import asyncio
    extractor = _Extractor(rows=[])
    assert asyncio.run(check_chapter_amounts("", planned_text=PLAN, extractor=extractor)) == []
    assert extractor.calls == 0


def test_resolve_plan_text_is_empty_without_ids_or_usable_session():
    """缺 id 直接返回空;会话不可用也只返回空,不抛(调用方据此跳过核对)。"""
    import asyncio
    assert asyncio.run(resolve_plan_text(object(), project_id=None, chapter_id="c")) == ""
    assert asyncio.run(resolve_plan_text(object(), project_id="p", chapter_id=None)) == ""
    # 传进来的不是可用会话 → 查询失败被吞掉,仍是空串,绝不抛给质量门。
    assert asyncio.run(resolve_plan_text(object(), project_id="p", chapter_id="c")) == ""

from app.services.numeric_check import read_chapter_ledger, record_chapter_ledger


# ---------------------------------------------------------------------------
# 按名目的金额台账(2026-11-17):把"会误报"变成"可信告警"
# ---------------------------------------------------------------------------
# 实测:旧口径 3 条命中里 2 条是误报 —— ch08 的 820 就写在**本章大纲**里、ch18 的 3000
# 早在 ch10 **正文**里写过;根因是没有地方记"实际写出过的金额",于是"新引入的金额"和
# "与前文冲突的金额"分不开。现在核对按名目与**台账**比对,并**登记本章写出的总额**。
# 台账读写走注入式(见 ledger_reader / ledger_writer),这些用例不起真实库。

LEDGER = [{"item": "垫了总数", "amount": 3000, "chapter_idx": 10}]
# 计划钉子(本章大纲里的两笔钱):用它触发核对,并验证计划口径没有被台账口径吃掉。
PIN_LINES = "[CH-18] 铺租八百付掉后只剩二十。"


class _LedgerReader:
    """假台账读取器:记录调用次数,可按需抛错。"""

    def __init__(self, entries=None, error=None):
        self.entries = list(entries if entries is not None else [])
        self.error = error
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.entries)


class _LedgerWriter:
    """假台账写入器:记录每次调用的参数,可按需抛错。"""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def __call__(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)
        return None


def test_same_item_with_a_new_amount_is_reported():
    """同名目此前登记过 3000,这章写 3500 → 报(并附上跟哪个数对不上)。"""
    rows = parse_amounts(
        '[{"item":"垫了总数","value":"三千五","quote":"这一回垫了三千五"}]')
    result = _gate_result("这一回垫了三千五。")
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PIN_LINES, extractor=_Extractor(rows),
        ledger_reader=_LedgerReader(LEDGER), ledger_writer=_LedgerWriter()))

    assert [row["written"] for row in found] == [3500]
    assert found[0]["ledger_amounts"] == [3000.0]
    assert result.numeric_conflicts == found
    assert result.status == "passed", "台账核对同样不能改 status"


def test_same_item_with_the_same_amount_is_not_reported():
    """ch18 那类误报:3000 不在计划(大纲/摘要都没记)里,但台账登记过 → 不报。"""
    rows = parse_amounts('[{"item":"垫了总数","value":"三千","quote":"先垫了三千"}]')
    result = _gate_result("先垫了三千。")
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PIN_LINES, extractor=_Extractor(rows),
        ledger_reader=_LedgerReader(LEDGER), ledger_writer=_LedgerWriter()))

    assert found == [] and result.numeric_conflicts == []
    assert result.status == "passed"


def test_first_appearance_of_an_item_is_not_reported():
    """首次出现的名目无从比对 → 不报(旧口径在这里会报一条)。"""
    rows = parse_amounts('[{"item":"新铺面货款","value":"四百","quote":"四百块新铺面货款"}]')
    result = _gate_result("四百块新铺面货款。")
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PIN_LINES, extractor=_Extractor(rows),
        ledger_reader=_LedgerReader(LEDGER), ledger_writer=_LedgerWriter()))

    assert found == [] and result.numeric_conflicts == []


def test_plan_fallback_still_reports_a_pinned_drift():
    """台账里没有的名目仍走计划的旧口径:照大纲写 820 却写 840 → 仍报(真阳性没丢)。"""
    rows = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百四十","quote":"一共是八百四十块整"}]')
    result = _gate_result("一共是八百四十块整。")
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text="[CH-7] 周芸收下八百二。", extractor=_Extractor(rows),
        ledger_reader=_LedgerReader([]), ledger_writer=_LedgerWriter()))

    assert [row["written"] for row in found] == [840]


def test_ledger_read_failure_is_swallowed_and_never_blocks():
    """读台账失败 → 只记 warning;计划里写得上的数不会因为读失败被报成冲突,status 不动。"""
    rows = parse_amounts('[{"item":"铺租","value":"八百","quote":"八百块铺租"}]')
    result = _gate_result("八百块铺租。")
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PIN_LINES, extractor=_Extractor(rows),
        ledger_reader=_LedgerReader(error=RuntimeError("ledger unreadable")),
        ledger_writer=_LedgerWriter()))

    assert found == [] and result.numeric_conflicts == []
    assert result.status == "passed"


def test_no_pins_means_zero_model_calls_and_zero_ledger_io():
    """成本边界不变:本章没有金额钉子 → 模型、台账读、台账写全都不发生。"""
    result = _gate_result("这一章没有谈钱。")
    extractor = _Extractor(rows=[])
    reader, writer = _LedgerReader(LEDGER), _LedgerWriter()
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text="", extractor=extractor,
        ledger_reader=reader, ledger_writer=writer))

    assert found == [] and extractor.calls == 0
    assert reader.calls == 0 and writer.calls == []


def test_extraction_failure_does_not_write_the_ledger():
    """抽不出金额(抽错/空正文)时不能登记台账 —— 否则下一次核对比错了对象。"""
    writer = _LedgerWriter()
    import asyncio
    for extractor in (_Extractor(error=RuntimeError("model exploded")), _Extractor(rows=None)):
        found = asyncio.run(attach_numeric_conflicts(
            _gate_result("总共是八百二十块钱。"), planned_text=PIN_LINES,
            extractor=extractor, ledger_reader=_LedgerReader(LEDGER), ledger_writer=writer))
        assert found == [] and writer.calls == []


def test_the_chapters_totals_are_registered_in_the_ledger():
    """核对过就把本章的总额登记进台账(只登记总额,不带单价),供后续章节比对。"""
    rows = parse_amounts(
        '[{"item":"每套校服工钱","value":"二十八块","quote":"每套二十八块"},'
        ' {"item":"铺租总额","value":"八百","quote":"八百块铺租"}]')
    writer = _LedgerWriter()
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        _gate_result("八百块铺租,每套二十八块。"), planned_text=PIN_LINES,
        extractor=_Extractor(rows), ledger_reader=_LedgerReader(LEDGER),
        ledger_writer=writer, chapter_id="ch-18", project_id="p-1"))

    assert found == []
    assert len(writer.calls) == 1
    assert writer.calls[0]["entries"] == [{"item": "铺租总额", "amount": 800}]
    assert writer.calls[0]["chapter_id"] == "ch-18"
    assert writer.calls[0]["project_id"] == "p-1"


def test_ledger_write_failure_does_not_lose_the_report():
    """写台账失败不能影响已经判出来的冲突清单(严格非阻断)。"""
    rows = parse_amounts('[{"item":"垫了总数","value":"三千五","quote":"q"}]')
    result = _gate_result("这一回垫了三千五。")
    import asyncio
    found = asyncio.run(attach_numeric_conflicts(
        result, planned_text=PIN_LINES, extractor=_Extractor(rows),
        ledger_reader=_LedgerReader(LEDGER),
        ledger_writer=_LedgerWriter(error=RuntimeError("db down"))))

    assert [row["written"] for row in found] == [3500]
    assert result.status == "passed"


def test_check_chapter_amounts_accepts_an_injected_ledger():
    rows = parse_amounts('[{"item":"垫了总数","value":"三千五","quote":"q"}]')
    import asyncio
    assert [row["written"] for row in asyncio.run(
        check_chapter_amounts("这一回垫了三千五。", planned_text=PIN_LINES,
                              extractor=_Extractor(rows), ledger=LEDGER))] == [3500]
    # 台账为空时退回计划口径:与计划一致的数不报,计划里没有的照旧报。
    assert asyncio.run(check_chapter_amounts(
        "八百块铺租。", planned_text=PIN_LINES,
        extractor=_Extractor(parse_amounts('[{"item":"铺租","value":"八百","quote":"q"}]')),
        ledger=[])) == []


def test_default_ledger_reader_and_writer_never_raise_offline():
    """默认读写器缺 id / 起不了会话 / id 不是 UUID 时都只是返回空,不抛(不进库)。"""
    import asyncio
    assert asyncio.run(read_chapter_ledger(None)) == []
    # 会话不可用 → 查询失败被吞掉,仍是空清单(默认读台账的会话由调用方注入或自开,
    # 离线用例不触发真实连接)。
    assert asyncio.run(read_chapter_ledger(object(), project_id="p", chapter_id="c")) == []
    assert asyncio.run(record_chapter_ledger(chapter_id=None, entries=[{"item": "x", "amount": 1}])) is None
    assert asyncio.run(record_chapter_ledger(chapter_id="not-a-uuid",
                                            entries=[{"item": "x", "amount": 1}])) is None
    assert asyncio.run(record_chapter_ledger(chapter_id="not-a-uuid", entries=[])) is None


def test_ledger_lands_in_the_metadata_that_gets_persisted():
    """台账必须随**同一次** quality_gate 落库,否则首次生成无处登记。

    核对发生在章节新版本行创建之前,所以只往"已有版本行"补写会让整本书的首次遍历都不生效。
    """
    rows = parse_amounts('[{"item":"校服工钱总额","value":"八百二十","quote":"q"}]')
    result = _gate_result("总共是八百二十块钱。")

    import asyncio
    asyncio.run(attach_numeric_conflicts(
        result, planned_text=PLAN, extractor=_Extractor(rows),
        ledger_reader=_LedgerReader([]), ledger_writer=_LedgerWriter()))

    assert result.numeric_ledger == [{"item": "校服工钱总额", "amount": 820}]
    assert result.to_safe_metadata()["numeric_ledger"] == [
        {"item": "校服工钱总额", "amount": 820}]


def test_ledger_metadata_is_empty_when_nothing_extracted():
    result = _gate_result("这一章没有谈钱。")
    assert result.to_safe_metadata()["numeric_ledger"] == []
