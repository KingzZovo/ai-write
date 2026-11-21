from unittest.mock import AsyncMock

import pytest

from app.services.auto_revise import RevisionContext, revise_spans


@pytest.mark.asyncio
async def test_rewrite_receives_author_and_story_context_without_truncation():
    text = '远处原文事实。' + '普通叙述。' * 80 + '\n前邻段。\n唯一需要修正的动作。\n后邻段。\n后文已经交还钥匙。'
    context = RevisionContext(user_instruction='保持母女关系，不添加父亲',
        chapter_outline={'ending': '明确完结，不留续卷钩子'}, previous_context='钥匙原属母亲',
        following_context='次日搬家', style_profile='朴素克制', cognition_ledger='女儿尚未得知病情')
    async def rewrite(prompt):
        for value in ('保持母女关系，不添加父亲', '明确完结，不留续卷钩子', '钥匙原属母亲',
                      '次日搬家', '朴素克制', '女儿尚未得知病情', '远处原文事实', '后文已经交还钥匙'):
            assert value in prompt
        assert '计划不等于已经发生' in prompt and '不提前兑现' in prompt
        return '前邻段。\n已经修正的动作。\n后邻段。'
    result = await revise_spans(text, [{'quote': '唯一需要修正的动作'}], llm_call=rewrite, context=context)
    assert result.spans_revised == 1
    assert result.text == text.replace('唯一需要修正的动作', '已经修正的动作')


@pytest.mark.asyncio
async def test_oversized_context_preserves_prose_and_does_not_silently_drop_author_rules(monkeypatch):
    from app.services import auto_revise
    monkeypatch.setattr(auto_revise, 'SPAN_REWRITE_MAX_INPUT_CHARS', 500)
    text = '唯一需要修正的动作。'
    issue = {'quote': '唯一需要修正的动作'}
    llm = AsyncMock()
    result = await revise_spans(text, [issue], llm_call=llm,
        context=RevisionContext(user_instruction='作者要求。' * 200))
    llm.assert_not_awaited()
    assert result.text == text and result.spans_revised == 0
    assert result.unlocatable_issues == [issue]


@pytest.mark.asyncio
@pytest.mark.parametrize('indent', ['', '  '])
async def test_unchanged_return_is_not_reported_as_a_successful_revision(indent):
    text = indent + '唯一需要修正的动作。'
    result = await revise_spans(text, [{'quote': text}], llm_call=AsyncMock(return_value=text))
    assert result.spans_revised == 0 and result.text == text
