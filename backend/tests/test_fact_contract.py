from copy import deepcopy
import hashlib

import pytest

from app.services.fact_contract import (
    FactContract,
    assert_text_matches_fact_contract,
    contract_from_outline_payload,
    validate_text_against_fact_contract,
)
from app.services.outline_authority import record_text_edit


def test_fact_contract_prompt_describes_identity_priority_and_state_sources():
    contract = FactContract(
        role_names={"protagonist": "林照", "mentor": "沈听澜"},
        registered_character_names=["林照", "沈听澜"],
    )

    prompt = contract.to_prompt()

    assert "优先级高于" in prompt
    assert "向量检索" in prompt
    assert "角色卡" in prompt
    assert "主角: 林照" in prompt
    assert "导师/救援者: 沈听澜" in prompt


def test_contract_from_outline_payload_reads_entity_registry():
    contract = contract_from_outline_payload(
        {
            "entity_registry": {
                "protagonist": "林照",
                "mentor": "沈听澜",
                "father": "林远山",
                "mother": "苏晚晴",
            },
            "raw_text": "主角林照在东港市旧环十二区生活。",
        }
    )

    assert contract.role_names == {
        "protagonist": "林照",
        "mentor": "沈听澜",
        "father": "林远山",
        "mother": "苏晚晴",
    }


def test_matching_text_passes_fact_contract_gate():
    contract = FactContract(role_names={"protagonist": "林照", "mentor": "沈听澜"})
    text = "主角林照在回声塌陷中觉醒，随后被导师沈听澜带离旧环十二区。"

    report = validate_text_against_fact_contract(text, contract)

    assert report.ok is True
    assert report.issues == []


def test_protagonist_name_difference_remains_a_contextual_observation():
    contract = FactContract(role_names={"protagonist": "林照"})
    text = "主角林烨，男，18岁，现居云港市东城老小区。"

    report = validate_text_against_fact_contract(text, contract)

    assert report.ok is True
    assert report.issues[0].code == "role_name_drift"
    assert report.issues[0].severity == "warning"
    assert report.issues[0].details["canonical"] == "林照"
    assert "林烨" in report.issues[0].details["observed"]
    assert report.issues[0].details["identity_established"] is False
    assert report.facts["text_hash"] == hashlib.sha256(text.encode()).hexdigest()
    assert report.facts["editorial_status"] == "needs_author_review"


def test_another_academy_mentor_is_not_proven_to_replace_the_registered_mentor():
    contract = FactContract(role_names={"mentor": "沈听澜"})
    text = "沈知夏是暗面学院外勤导师成员，负责回收失控觉醒者。"

    report = validate_text_against_fact_contract(text, contract)

    assert report.ok is True
    assert report.issues[0].code == "role_name_drift"
    assert report.issues[0].severity == "warning"
    assert report.issues[0].details["canonical"] == "沈听澜"
    assert "沈知夏" in report.issues[0].details["observed"]


def test_multiple_people_with_a_shared_role_are_not_an_unfounded_hard_error():
    # There is no canonical single mentor in this contract; two mentors can
    # coexist in a novel. Regex role detection cannot prove identity drift.
    text = '导师：沈听澜。另一位导师：陈川。'
    report = validate_text_against_fact_contract(text, FactContract())
    assert len(report.facts['observed_role_names']['mentor']) == 2
    assert report.ok
    assert report.issues and all(issue.severity == 'warning' for issue in report.issues)


def test_action_sentence_does_not_trigger_false_canonical_name_drift():
    contract = FactContract(role_names={'mentor': '陈川'}, registered_character_names=['陈川'])
    report = validate_text_against_fact_contract('导师陈川留在门外。', contract)
    assert report.ok and report.issues == []
    assert report.facts['scope'] == 'explicit_role_declarations_only'
    # Explicit contradictory declarations remain detectable.
    report = validate_text_against_fact_contract('导师：陈川留。', contract)
    assert report.ok
    assert report.issues[0].details['observed'] == ['陈川留']
    assert report.issues[0].severity == 'warning'


def test_identity_prompt_preserves_author_control_and_declares_its_scope():
    prompt = FactContract(role_names={'mentor': '陈川'}).to_prompt()
    assert '不能代替前文正文' in prompt
    assert '按作者要求处理' in prompt
    assert '同类身份可以属于不同人物' in prompt
    assert '唯一事实契约' not in prompt


def test_registered_names_are_not_silently_truncated_at_eighty():
    names = [f'登记角色{i}' for i in range(85)]
    prompt = FactContract(registered_character_names=names).to_prompt()
    assert '、'.join(names) in prompt


def test_current_author_text_replaces_old_registry_before_name_extraction():
    old = {'entity_registry': {'protagonist': '林照', 'mentor': '沈听澜'},
           'raw_text': '主角：林照。导师：沈听澜。'}
    edited = record_text_edit(old, {**old, 'raw_text': '主角：林禾。她在渡口修船。'})
    original = deepcopy(edited)
    contract = contract_from_outline_payload(edited)
    assert contract.role_names == {'protagonist': '林禾'}
    assert contract.outline_source['authority'] == 'author_text'
    assert contract.role_name_sources == {'protagonist': 'author_text'}
    assert contract.outline_role_names == {'protagonist': ['林禾']}
    assert '林照' not in contract.to_prompt() and '沈听澜' not in contract.to_prompt()
    assert edited == original


def test_current_author_text_can_supersede_unusable_old_registry_structure():
    contract = contract_from_outline_payload({'entity_registry': ['旧提取结构'],
        'summary': {'obsolete': True}, 'raw_text': '主角：林禾。', '_author_text_revision': 'revision'})
    assert contract.role_names == {'protagonist': '林禾'}
    assert contract.role_name_sources == {'protagonist': 'author_text'}


@pytest.mark.parametrize('raw', ['', ' \n ', None, ['主角：林禾。']])
def test_bad_current_author_text_cannot_reinstate_old_registry(raw):
    payload = {'entity_registry': {'protagonist': '林照'},
               'raw_text': raw, '_author_text_revision': 'saved-author-revision'}
    with pytest.raises(ValueError, match='当前确认书纲'):
        contract_from_outline_payload(payload)


def test_multiple_outline_role_names_are_preserved_without_choosing_a_single_identity():
    payload = {'entity_registry': {'mentor': {'name': '沈听澜'}, '导师': '陈川'},
               'raw_text': '导师：沈听澜。另一位导师：陈川。'}
    contract = contract_from_outline_payload(payload)
    assert 'mentor' not in contract.role_names
    assert contract.outline_role_names['mentor'] == ['沈听澜', '陈川']
    assert '沈听澜' in contract.to_prompt() and '陈川' in contract.to_prompt()


@pytest.mark.parametrize(('text', 'role', 'name'), [
    ('导师：沈听澜。另一位导师：陈川。', 'mentor', '沈听澜'),
    ('林禾介绍自己的父亲：林远山。陈川接着说，他的父亲：陈海。', 'father', '林远山'),
    ('她已经在新稿中更名，主角：林禾。', 'protagonist', '林照'),
])
def test_same_role_label_or_an_author_rename_needs_context_not_a_regex_veto(text, role, name):
    contract = FactContract(role_names={role: name})
    report = assert_text_matches_fact_contract(text, contract)
    assert report.ok and report.issues
    assert all(issue.severity == 'warning' for issue in report.issues)
    assert report.issues[0].details['identity_established'] is False
    assert report.facts['observed_role_names'][role]
    assert report.facts['editorial_status'] == 'needs_author_review'


def test_no_detected_name_difference_does_not_certify_identity_or_prose_quality():
    report = validate_text_against_fact_contract('陈川把信交给了她。', FactContract())
    assert report.ok and report.issues == []
    assert report.facts['editorial_status'] == 'needs_author_review'
    assert report.facts['scope'] == 'explicit_role_declarations_only'
