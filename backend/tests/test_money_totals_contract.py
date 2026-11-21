"""回归:计划阶段定数(money_totals,步骤 4)。

根因(实测 2026-11-16):ch07 大纲只写"周芸说明八百的期限当天到",**没写这批活的工钱总额**,
于是写手当场自己发明一个自洽的数(outline10 写 880 = 四十套×二十二;baseline 写 840 = 三十套×二十八),
而 ch08 大纲才第一次写出"八百二" —— 相邻两章必然对不上。

修法是把"钱"提前到计划里定下来,并让这个数在写手提示里排在最前:
1. 场景合同新增**可选**字段 `money_totals`(不写不算错,坏值不阻断);
2. `CHAPTER_CONTRACT_FIELDS` 声明它,章节大纲块会渲染;
3. `numeric_pins` 把它作为**前置块**注入(条数上限会挤掉靠后的块,所以本章的数必须在最前)。
"""
from __future__ import annotations

import pytest

from app.services.context_pack import ContextPack
from app.services.outline_contract import CHAPTER_CONTRACT_FIELDS
from app.services.scene_plan_contract import ScenePlanValidationError, validate_scene_plan
from app.services.scene_plan_contract import validate_scene_plan
from tests.test_c1_scene_orchestrator import _scene_contract

MONEY = "校服工钱总额=八百二十(四十套 × 每套二十块零五毛)"


def _briefs(**overrides):
    plan = {"scenes": [_scene_contract(**overrides)]}
    return validate_scene_plan(plan, TARGET)


TARGET = 900


def test_money_totals_is_optional_and_absent_by_default():
    """不写这个字段不算错 —— 否则不含金额的场景会被规划校验打成失败。"""

def test_money_totals_is_optional_and_absent_by_default():
    """不写这个字段不算错 —— 否则不含金额的场景会被规划校验打成失败。"""
    briefs = _briefs()
    assert briefs[0].money_totals == ""


def test_money_totals_is_carried_and_rendered_to_the_writer():
    briefs = _briefs(money_totals=MONEY)
    assert briefs[0].money_totals == MONEY
    text = briefs[0].to_writer_user_content()
    assert "【本场已定金额】" in text
    assert "八百二十" in text

@pytest.mark.parametrize("bad", [["八百二十"], {"amount": 820}, []])
def test_structured_money_totals_is_normalized_not_fatal(bad):
    """列表/对象形态不会被丢掉,而是按既有台账口径归一成 JSON 字符串。"""
    briefs = _briefs(money_totals=bad)
    assert isinstance(briefs[0].money_totals, str)


@pytest.mark.parametrize("bad", [True, 820])
def test_scalar_money_totals_goes_to_the_repair_loop(bad):
    """标量坏值会被合同校验拒收(交给规划修复回环),不是静默放行。

    断言用内置 ``ValueError`` + 字段名:不依赖特定异常类对象,
    免得模块被重复导入时类身份不一致。
    """
    with pytest.raises(ValueError) as exc:
        _briefs(money_totals=bad)
    assert "money_totals" in str(exc.value)

def test_chapter_contract_fields_declares_money_totals():
    assert CHAPTER_CONTRACT_FIELDS["money_totals"] == "本场已定金额"


def test_chapter_outline_block_renders_money_totals():
    pack = ContextPack(current_outline={"summary": "结账", "money_totals": MONEY})
    prompt = pack.to_system_prompt()
    assert "本场已定金额" in prompt and "八百二十" in prompt


def test_chapter_money_totals_leads_the_numeric_pins():
    """本章计划定的数必须排在钉子清单最前:pin_block 有条数上限,靠后会被挤掉。"""
    pack = ContextPack(
        current_outline={"summary": "结账", "money_totals": MONEY},
        future_outlines=["[VOL-1/CH-8; id=x]《校服的钱》: 周芸收下八百四。"] * 12,
    )
    lines = pack.numeric_pin_lines()
    assert lines.splitlines()[0].startswith("[本章]")
    assert "八百二十" in lines


def test_numeric_pins_still_work_without_chapter_money_totals():
    """没写 money_totals 时行为不变:仍从卷纲/后章大纲取数。"""
    pack = ContextPack(future_outlines=["[VOL-1/CH-8; id=x]《校服的钱》: 周芸收下八百二。"])
    lines = pack.numeric_pin_lines()
    assert "八百二" in lines
    assert "[本章]" not in lines
