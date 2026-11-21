"""回归:实体提取的逐字依据同样要可定位,且失败时要点名。

实测(2026-11-14 真实链):`location_changes[0].evidence无法在原文逐字定位` 在第 8 章
触发过一次,靠重试才过。原因与认知提取相同——模型把 evidence 改写成自己的话。
原来的修复提示只说"字段下标",模型重试仍然继续改写;这里补上"哪一句找不到"。

同一组约束也写进了 `chapter_entity_extraction.SYSTEM_PROMPT`:
直接从正文复制连续片段,不改写、不加省略号、不拼接。
"""
from __future__ import annotations

import json

import pytest

from app.services.chapter_entity_extraction import (
    SYSTEM_PROMPT,
    EntityExtractionError,
    parse_entity_changes,
    validate_entity_changes,
)

SOURCE = (
    "周芸把帆布包往肩上托了托，从里面掏出一叠钱。\n"
    "“这是两千。”她说。\n"
)

EMPTY = {"new_characters": [], "state_changes": [], "new_relationships": [],
         "changed_relationships": [], "location_changes": []}


def _payload(**overrides):
    data = {key: [] for key in EMPTY}
    data.update(overrides)
    return data


def test_verbatim_evidence_passes():
    data = _payload(state_changes=[
        {"name": "周芸", "changes": {"持有": "两千元"}, "evidence": "从里面掏出一叠钱。"}])
    assert validate_entity_changes(data, SOURCE) == data


def test_rewritten_evidence_is_rejected_and_named():
    rewritten = "她把攒下的钱都拿了出来。"
    data = _payload(state_changes=[
        {"name": "周芸", "changes": {"持有": "两千元"}, "evidence": rewritten}])
    with pytest.raises(EntityExtractionError) as excinfo:
        validate_entity_changes(data, SOURCE)
    assert getattr(excinfo.value, "offending_evidence", None) == [rewritten]


def test_evidence_with_ellipsis_is_rejected():
    data = _payload(state_changes=[
        {"name": "周芸", "changes": {"持有": "两千元"},
         "evidence": "周芸把帆布包往肩上托了托......从里面掏出一叠钱。"}])
    with pytest.raises(EntityExtractionError):
        validate_entity_changes(data, SOURCE)


def test_prompt_demands_continuous_verbatim_copy():
    assert "直接复制连续的一段" in SYSTEM_PROMPT
    assert "不加省略号" in SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_repair_prompt_names_the_quote_that_could_not_be_located(monkeypatch):
    """第二次调用必须带上"哪句依据找不到",而不是只有字段下标。"""
    from app.services import prompt_registry

    rewritten = "她把攒下的钱都拿了出来。"
    good = _payload(state_changes=[
        {"name": "周芸", "changes": {"持有": "两千元"}, "evidence": "从里面掏出一叠钱。"}])
    calls: list[str] = []

    class _Result:
        finish_reason = "stop"

        def __init__(self, text):
            self.text = text

    async def _fake(task_type, user_content, **kwargs):
        calls.append(user_content)
        payload = (_payload(state_changes=[
            {"name": "周芸", "changes": {"持有": "两千元"}, "evidence": rewritten}])
            if len(calls) == 1 else good)
        return _Result(json.dumps(payload, ensure_ascii=False))


    monkeypatch.setattr(prompt_registry, "run_text_prompt", _fake)
    from app.services.chapter_entity_extraction import extract_entity_changes

    result = await extract_entity_changes(project_id="p", chapter_text=SOURCE, db=None,
                                          relationship_candidates=None,
                                          defer_relationship_scope=True)
    assert result["state_changes"][0]["evidence"] == "从里面掏出一叠钱。"
    assert len(calls) == 2
    assert rewritten in calls[1], "修复提示必须点名找不到的那句"
    assert "复制连续原文" in calls[1]


def test_parse_entity_changes_reports_locator_failure():
    raw = json.dumps(_payload(location_changes=[
        {"character": "周芸", "location": "渡口", "evidence": "她站在渡口边上。"}]),
        ensure_ascii=False)
    with pytest.raises(EntityExtractionError) as excinfo:
        parse_entity_changes(raw, SOURCE, defer_relationship_scope=True)
    assert getattr(excinfo.value, "offending_evidence", None)
