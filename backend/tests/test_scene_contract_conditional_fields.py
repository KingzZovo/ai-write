"""Regression: scene-contract gate must match the prompt's conditional framing.

SCENE_CONTRACT_FIELDS_PROMPT marks action_budget / inference_ledger as
CONDITIONAL ("高压/追捕/近身冲突场景必须...") and the field block as
"必须尽量补齐". But _has_valid_scene_contract required all 12 fields on EVERY
scene unconditionally, so a legitimately calm scene (no high-pressure action)
that omitted action_budget/inference_ledger was rejected and the whole chapter
fell back to template briefs. Observed on 神裔 ch2/ch3 full-flow run.

The gate must require the always-applicable continuity fields, but accept a
scene that only omits the conditional (high-pressure-only) fields.
"""
from __future__ import annotations

from app.services.scene_orchestrator import (
    SceneBrief,
    _has_valid_scene_contract,
    _missing_contract_fields,
)

_ALWAYS_FIELDS = dict(
    start_state="承接上一场",
    time_delta="紧接上一场",
    location_path="同地承接",
    entity_transfers="人物到场",
    power_resource_map="资源差清楚",
    information_state="已知信息",
    mechanism_limits="机制有边界",
    result_strength="局部推进",
    transition_bridge="交给下一场",
    continuity_ledger="台账：场初->场末",
)


def _calm_scene():
    # A quiet scene: every always-on field present, but the conditional
    # high-pressure fields (action_budget / inference_ledger) legitimately blank.
    return SceneBrief(idx=1, title="对坐", brief="安静的对话场", **_ALWAYS_FIELDS)


def test_calm_scene_without_conditional_fields_is_valid():
    s = _calm_scene()
    assert s.action_budget == "" and s.inference_ledger == ""
    assert _missing_contract_fields(s) == [], (
        "conditional fields should not be flagged missing on a calm scene"
    )
    assert _has_valid_scene_contract([s])


def test_scene_missing_an_always_field_still_rejected():
    s = _calm_scene()
    s.continuity_ledger = ""  # drop an always-required field
    assert "continuity_ledger" in _missing_contract_fields(s)
    assert not _has_valid_scene_contract([s])
