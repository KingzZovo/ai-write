"""回归:计划层金额收敛(2026-11-17)。

实测两章链:加了 `money_totals` 字段后**写手**不再自造,但**规划者**会 ——
ch07 计划写「工钱=八百四十元(三十套 × 每套二十八元)」,而 ch08 大纲写"八百二"。
两个阶段各自自洽,读者看到的仍是相邻两章对不上。

所以约束下在计划层:与大纲冲突的金额条目丢掉、保留其余,并声明以大纲为准。
**不抛错、不改 status** —— 金额对不上不该让整章写不出来。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import prompt_registry
from app.services.money_plan import AUTHORITY_NOTE, reconcile_money_totals
from app.services.scene_orchestrator import SceneOrchestrator
from tests.test_c1_scene_orchestrator import _FakePack, _scene_contract

PINNED = "[CH-8] 周芸收下八百二。铺租八百付掉后只剩二十。"


def test_matching_entries_are_kept_untouched():
    text = "铺租=八百元(今天到期)"
    assert reconcile_money_totals(text, PINNED) == (text, [])


def test_conflicting_entry_is_dropped_and_authority_noted():
    text = "校服工钱=八百四十元(三十套 × 每套二十八元); 铺租=八百元(今天到期)"
    kept, dropped = reconcile_money_totals(text, PINNED)
    assert dropped == ["校服工钱=八百四十元(三十套 × 每套二十八元)"]
    assert "八百四十" not in kept
    assert "铺租=八百元" in kept
    assert kept.endswith(AUTHORITY_NOTE)


def test_no_pins_means_no_authority_to_change_the_plan():
    text = "校服工钱=八百四十元"
    assert reconcile_money_totals(text, "") == (text, [])
    assert reconcile_money_totals(text, "本章没有谈钱。") == (text, [])


def test_empty_entry_text_is_a_no_op():
    assert reconcile_money_totals("", PINNED) == ("", [])


def test_entry_without_numbers_is_kept():
    text = "工钱另计;铺租=八百元"
    kept, dropped = reconcile_money_totals(text, PINNED)
    assert dropped == []
    assert "工钱另计" in kept


def test_everything_conflicting_leaves_only_the_authority_note():
    kept, dropped = reconcile_money_totals("校服工钱=八百四十元", PINNED)
    assert dropped == ["校服工钱=八百四十元"]
    assert kept == AUTHORITY_NOTE


class _PackWithPins(_FakePack):
    def numeric_pin_lines(self) -> str:
        return PINNED


def _reply(payload):
    return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), finish_reason="stop")


@pytest.mark.asyncio
async def test_planner_hook_drops_conflicting_totals_before_writing(monkeypatch):
    """规划返回的自造总额必须在交给写手之前被收敛掉。"""
    scene = _scene_contract(money_totals="校服工钱=八百四十元(三十套 × 每套二十八元); 铺租=八百元")
    monkeypatch.setattr(prompt_registry, "run_text_prompt",
                       AsyncMock(side_effect=[_reply({"scenes": [scene]})]))
    briefs = await SceneOrchestrator().plan_scenes(
        pack=_PackWithPins(), db=None, project_id="p", chapter_id="c",
        target_words=900, user_instruction="",
    )
    assert len(briefs) == 1
    assert "八百四十" not in briefs[0].money_totals
    assert "铺租=八百元" in briefs[0].money_totals
    assert briefs[0].money_totals.endswith(AUTHORITY_NOTE)


@pytest.mark.asyncio
async def test_planner_hook_is_a_no_op_without_pins(monkeypatch):
    """包里没有金额钉子时,计划原样保留(没有权威值就无权改)。"""
    scene = _scene_contract(money_totals="校服工钱=八百四十元")
    monkeypatch.setattr(prompt_registry, "run_text_prompt",
                       AsyncMock(side_effect=[_reply({"scenes": [scene]})]))
    briefs = await SceneOrchestrator().plan_scenes(
        pack=_FakePack(), db=None, project_id="p", chapter_id="c",
        target_words=900, user_instruction="",
    )
    assert briefs[0].money_totals == "校服工钱=八百四十元"

def test_comma_separated_entries_are_judged_one_by_one():
    """实测:ch08 的计划用「, 」分隔三条;不能因为一条不合就整串丢掉。"""
    text = ("校服工钱=820元(王婶验收三十套校服支付), "
            "铺面租金=800元(交付给房东老钱), "
            "结余现金=20元(扣除租金后缝纫铺实际剩余)")
    kept, dropped = reconcile_money_totals(text, PINNED)
    assert dropped == []
    assert "820" in kept and "800" in kept and "20" in kept


def test_one_bad_entry_among_commas_does_not_discard_the_rest():
    text = "校服工钱=840元(三十套×二十八元), 铺面租金=800元(交付房东)"
    kept, dropped = reconcile_money_totals(text, PINNED)
    assert dropped == ["校服工钱=840元(三十套×二十八元)"]
    assert "铺面租金=800元" in kept
