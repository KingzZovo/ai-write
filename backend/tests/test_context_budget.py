import pytest
from app.services.context_budget import ContextBudgetExceeded
from app.services.context_pack import ContextPack


def test_pressure_preserves_chapter_and_truth():
    pack = ContextPack(current_outline={'summary': '交接密信', 'action_budget': '只能开一扇门'},
        authoritative_fact_contract='阿青失去左手', world_rules=['死者不能复活'],
        cognition_boundaries='守卫不知道秘密', future_outlines=['未来事件' * 10000],
        related_chapter_recall='历史回读' * 10000, style_tic_mirror='文风提醒' * 10000)
    prompt = pack.to_system_prompt(token_budget=1200)
    assert all(x in prompt for x in ('只能开一扇门', '阿青失去左手', '死者不能复活', '守卫不知道秘密'))
    assert len(prompt) <= 1800
    trace = {r['source']: r for r in pack.budget_trace}
    assert trace['future_outlines']['status'] == 'omitted_budget'
    assert trace['authoritative_fact_contract']['status'] == 'included'
    assert '阿青' not in str(pack.budget_trace)


def test_required_overflow_is_explicit():
    pack = ContextPack(current_outline={'summary': '必要剧情' * 500})
    with pytest.raises(ContextBudgetExceeded, match='current_outline'):
        pack.to_system_prompt(token_budget=100)
    assert pack.budget_trace == []


def test_prose_tail_is_labelled_and_bounded():
    pack = ContextPack(current_content='开头' * 3000 + '最后未完成动作')
    prompt = pack.to_system_prompt(token_budget=400)
    assert '最后未完成动作' in prompt and '仅保留最近内容' in prompt
    assert len(prompt) <= 600
    assert pack.budget_trace[0]['status'] == 'tail_only'


def test_required_facts_borrow_other_slots():
    prompt = ContextPack(authoritative_fact_contract='事实' * 300).to_system_prompt(token_budget=600)
    assert '事实' * 300 in prompt and len(prompt) <= 900


@pytest.mark.parametrize('budget', [0, -1])
def test_invalid_budget(budget):
    with pytest.raises(ValueError, match='预算'):
        ContextPack().to_system_prompt(token_budget=budget)


def test_trace_refreshes_between_renders():
    pack = ContextPack(rag_snippets=['参考'])
    pack.to_system_prompt()
    pack.rag_snippets = []
    assert pack.to_system_prompt() == '' and pack.budget_trace == []


def test_previous_chapter_survives_large_current_prose_and_older_summaries():
    pack = ContextPack(recent_summaries=['旧事' * 4000, '上一章停在桥上，密信尚未送到'],
                       current_content='本章正文' * 4000)
    prompt = pack.to_system_prompt(token_budget=500)
    assert '上一章停在桥上，密信尚未送到' in prompt
    trace = {r['source']: r for r in pack.budget_trace}
    assert trace['recent_summaries[0]']['status'] == 'omitted_budget'
    assert trace['recent_summaries[1]']['required'] is True


def test_identity_record_cannot_evict_character_state_under_budget_pressure():
    from app.services.context_pack import CharacterCard
    pack = ContextPack(authoritative_fact_contract='导师：陈川', character_cards=[
        CharacterCard(name='陈川', location='门外', power_level='左手受伤，无法持剑',
                      relationships={'沈青': '尚不信任'}, mental_state='仍在等人'),
    ], world_rules=['本书既定规则。' * 30], rag_snippets=['无关检索' * 1000])
    prompt = pack.to_system_prompt(token_budget=550)
    assert all(text in prompt for text in ('门外', '左手受伤，无法持剑', '尚不信任', '仍在等人'))
    trace = {row['source']: row for row in pack.budget_trace}
    assert trace['character_cards']['required']
    assert trace['character_cards']['status'] == 'included'
    assert trace['authoritative_fact_contract']['status'] == 'included'


def test_state_overflow_cannot_silently_fall_back_to_names_only():
    from app.services.context_pack import CharacterCard
    pack = ContextPack(authoritative_fact_contract='导师：陈川', character_cards=[
        CharacterCard(name='陈川', mental_state='必要状态' * 300),
    ])
    with pytest.raises(ContextBudgetExceeded, match='character_cards'):
        pack.to_system_prompt(token_budget=400)
