from app.services.fact_contract import (
    FactContract,
    contract_from_outline_payload,
    validate_text_against_fact_contract,
)


def test_fact_contract_prompt_declares_authority_over_retrieval_and_cards():
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


def test_protagonist_name_drift_fails_fact_contract_gate():
    contract = FactContract(role_names={"protagonist": "林照"})
    text = "主角林烨，男，18岁，现居云港市东城老小区。"

    report = validate_text_against_fact_contract(text, contract)

    assert report.ok is False
    assert report.issues[0].code == "role_name_drift"
    assert report.issues[0].details["canonical"] == "林照"
    assert "林烨" in report.issues[0].details["observed"]


def test_mentor_name_drift_fails_fact_contract_gate():
    contract = FactContract(role_names={"mentor": "沈听澜"})
    text = "沈知夏是暗面学院外勤导师成员，负责回收失控觉醒者。"

    report = validate_text_against_fact_contract(text, contract)

    assert report.ok is False
    assert report.issues[0].code == "role_name_drift"
    assert report.issues[0].details["canonical"] == "沈听澜"
    assert "沈知夏" in report.issues[0].details["observed"]
