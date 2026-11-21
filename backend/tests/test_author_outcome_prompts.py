"""Prompt routing/constraints regression; passing is not a literary verdict."""
import copy
import logging

import pytest

from app.services.chapter_generator import ChapterGenerator
from app.services.chapter_evaluator import EVALUATION_SYSTEM_PROMPT, _build_user_prompt
from app.services.narrative_contract import (
    WORLD_LOGIC_CONTRACT, SCENE_CONTRACT_FIELDS_PROMPT, WRITER_CONTRACT_PROMPT,
    EVALUATOR_CONTRACT_PROMPT, EVALUATOR_CALIBRATION_PROMPT, REVISE_CONTRACT_PROMPT,
    contract_block,
)
from app.services.narrative_quality_gates import (
    BLUEPRINT_VERSION, contract_hard_gate_prompt, preflight_scene_blueprint_prompt,
)
from app.services.scene_orchestrator import _REQUIRED_CONTRACT_FIELDS


@pytest.mark.parametrize('prompt', [
    WORLD_LOGIC_CONTRACT, SCENE_CONTRACT_FIELDS_PROMPT, WRITER_CONTRACT_PROMPT,
    EVALUATOR_CONTRACT_PROMPT, EVALUATOR_CALIBRATION_PROMPT, REVISE_CONTRACT_PROMPT,
    contract_block(include_scene_fields=True, include_writer=True),
    contract_hard_gate_prompt(), preflight_scene_blueprint_prompt(chapter_idx=1),
])
def test_no_unconditional_downgrade_or_adversity_contract(prompt):
    for obsolete in (
        '最多一问一答', '每个窗口最多完成一个核心动作', '低资源方推进必须付出',
        '章末只许给方向和疑点', '不得完整完成多步低风险流程',
        '单次对白最多泄露 2 个信息碎片', '对话只能泄露碎片',
        '把全面胜利/定论/公开翻盘降级为',
        '没有高严重度合同违规时，accept',
        '不得仅因“偏离大纲”重判重写',
    ):
        assert obsolete not in prompt


def test_blueprint_injection_preserves_author_messages_and_reports_current_version(caplog):
    messages = [{'role': 'user', 'content': '今夜完成六本台账清点并交钥匙；明早站长核对后封存。'}]
    before = copy.deepcopy(messages)
    with caplog.at_level(logging.WARNING):
        result = ChapterGenerator._with_preflight_blueprint(messages, chapter_idx=1)
    assert messages == before
    assert result[1:] == before
    assert result[0]['role'] == 'system'
    assert f'direct_generation_first_{BLUEPRINT_VERSION}' in result[0]['content']
    assert 'markers_missing=none' in caplog.text
    assert '完成、确认、保留或延后' in result[0]['content']
    assert '计划中的结果不算已发生' in result[0]['content']


def test_scene_contract_retains_parser_fields_without_manufacturing_conflict():
    for field in _REQUIRED_CONTRACT_FIELDS:
        assert field in SCENE_CONTRACT_FIELDS_PROMPT
    assert '不涉及' in SCENE_CONTRACT_FIELDS_PROMPT
    assert '仅在相关情形补充' in SCENE_CONTRACT_FIELDS_PROMPT
    assert '普通合作无需虚构双方对抗或代价' in SCENE_CONTRACT_FIELDS_PROMPT


def test_evaluator_and_revision_share_author_obligations():
    body = '沈青答应明晚再给他钥匙。'
    outline = {'ending': '今夜沈青把钥匙交给陈川，陈川带钥匙去开末班车。'}
    prompt = _build_user_prompt(body, outline, '', '', [])
    assert body in prompt
    assert outline['ending'] in prompt
    assert EVALUATOR_CONTRACT_PROMPT in EVALUATION_SYSTEM_PROMPT
    assert EVALUATOR_CALIBRATION_PROMPT in EVALUATION_SYSTEM_PROMPT
    assert 'chapter_completion_violation' in EVALUATOR_CONTRACT_PROMPT
    assert 'chapter_completion_violation' in REVISE_CONTRACT_PROMPT
    assert '承诺不代替交付' in REVISE_CONTRACT_PROMPT
    assert '不能以“合理取舍”豁免' in EVALUATOR_CALIBRATION_PROMPT


@pytest.mark.parametrize('chapter_idx', [0, -1])
def test_invalid_chapter_position_does_not_get_opening_advice(chapter_idx):
    assert '开卷叙事关注' not in preflight_scene_blueprint_prompt(chapter_idx)


def test_default_generation_prompt_has_no_fixed_length_or_plot_template():
    from app.services.prompt_registry import BUILTIN_PROMPTS
    from app.services.prompts.scene_writing import CHAPTER_WRITER_SYSTEM
    generation = next(row for row in BUILTIN_PROMPTS if row['task_type'] == 'generation')
    assert generation['system_prompt'] == CHAPTER_WRITER_SYSTEM
    assert '每章至少3000字' not in generation['system_prompt']
    assert '本章篇幅服从本轮目标' in generation['system_prompt']
    assert '实际完成' in generation['system_prompt']


def test_revise_instruction_does_not_reintroduce_result_downgrades():
    from app.services.auto_revise import EvaluationLite, issues_to_revise_instruction
    instruction = issues_to_revise_instruction(EvaluationLite(overall=7, issues=[{
        'violation_type': 'chapter_completion_violation', 'description': '只承诺交钥匙，未交付',
    }]))
    assert '未涉及的问题保留原句' in instruction
    assert '不能把未发生的计划结果当事实' in instruction
    assert '只能带走台账允许的局部成果' not in instruction
    assert '不要重复原文句式' not in instruction
    assert '就必须降级结果或删除该桥段' not in instruction


@pytest.mark.asyncio
async def test_scene_writer_composed_request_preserves_author_and_actual_state(monkeypatch):
    from app.services import scene_orchestrator as so
    from tests.test_c1_scene_orchestrator import _FakePack
    captured = {}

    async def stream(**kwargs):
        captured.update(kwargs)
        yield '她把钥匙递了过去。'

    monkeypatch.setattr(so, 'stream_text_prompt', stream)
    author = '今夜只交钥匙，明早共同核对后正式封存；不增加临时事故。'
    prior = '她已经点清六本台账。钥匙仍在她手里。'
    plan = '本场计划把钥匙交给陈川，尚未正式封存。'
    scene = so.SceneBrief(idx=2, title='交钥匙', brief='交付', continuity_ledger=plan)
    output = ''.join([part async for part in so.SceneOrchestrator().write_scene_stream(
        scene=scene, pack=_FakePack(), prior_scenes_summary=prior, db=None,
        project_id='p', chapter_id='c', user_instruction=author)])
    request = captured['user_content']
    assert output == '她把钥匙递了过去。'
    assert author in request and prior in request and plan in request
    assert '场景台账尚待正文落实' in request
    assert request.count(plan) == 1
    assert '不能覆盖作者明确要求或已写事实' in request
    assert '都必须有可读路径或代价' not in request
    assert '支撑不足时降级为疑点或待验证线索' not in request


@pytest.mark.asyncio
async def test_scene_planner_and_emergency_do_not_add_unrequested_adversity(monkeypatch):
    from dataclasses import asdict
    from unittest.mock import AsyncMock
    from app.services import prompt_registry
    from app.services.scene_orchestrator import SceneOrchestrator, _fallback_scene_briefs
    from tests.test_c1_scene_orchestrator import _FakePack, _scene_contract
    author = '完成交接，直接回家。无追兵，无额外代价，不留悬念。'
    runner = AsyncMock(return_value={'items': [_scene_contract(target_words=900)]})
    monkeypatch.setattr(prompt_registry, 'run_structured_prompt', runner)
    await SceneOrchestrator().plan_scenes(pack=_FakePack(), db=None,
        project_id='p', chapter_id='c', target_words=900, user_instruction=author)
    request = runner.call_args.kwargs['user_content']
    assert author in request
    assert '日常到场、离场可合理省略' in request
    assert '新增实体必须写来源、入场路径、知情人和代价' not in request
    emergency = _fallback_scene_briefs(900, author)
    assert ''.join(scene.brief for scene in emergency) == author
    contract = str(asdict(emergency[-1]))
    assert '任何改变局势的机制必须有成本' not in contract
    assert '动作超预算则拆场或降级结果' not in contract
    assert '不强加钩子' in contract
