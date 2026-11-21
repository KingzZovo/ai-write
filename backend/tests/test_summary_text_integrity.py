import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services.chapter_summarizer import _clean_summary_output, summarize_chapter_text


@pytest.mark.parametrize('raw,expected', [
    ('{"summary":"中文第一段。\n\n第二段：仍未交信。"}', '中文第一段。\n\n第二段：仍未交信。'),
    ('{"summary":"\\u4e2d文与\\\"引号\\\"。"}', '中文与"引号"。'),
    ('{"summary":"甲","summary":"乙"}', ''),
    ('{"summary":123}', ''),
    ('{"key_events":["不能当摘要"]}', ''),
    ('["不能把数组当完整摘要"]', ''),
    ('```json\n{"summary":"围栏未结束"}', ''),
    ('{"summary":"\\ud800"}', ''),
    ('{"summary":"\\u0000"}', ''),
])
def test_cleaner_preserves_chinese_and_rejects_ambiguous_or_invalid_wrappers(raw, expected):
    assert _clean_summary_output(raw) == expected


@pytest.mark.asyncio
async def test_complete_middle_and_every_output_paragraph_survive(monkeypatch):
    body = '开篇。' * 1500 + '【中段唯一转折：交信失败，原信退还本人】' + '收尾。' * 1500
    summary = '前情。' * 80 + '\n\n信未送出，原信仍由本人保管。'
    run = AsyncMock(return_value=SimpleNamespace(text=json.dumps({'summary': summary}, ensure_ascii=False)))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', run)
    assert await summarize_chapter_text(title='交信', content_text=body, db=None) == summary
    sent = run.await_args.kwargs
    assert body in sent['user_content']
    assert '中部省略' not in sent['user_content']
    assert sent['max_tokens'] == 2048


@pytest.mark.asyncio
async def test_one_repair_retains_source_and_invalid_output(monkeypatch):
    raw = '{"summary": "断裂的输出'
    run = AsyncMock(side_effect=[SimpleNamespace(text=raw), SimpleNamespace(text='{"summary":"完整摘要。"}')])
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', run)
    body = '完整正文。' * 100
    assert await summarize_chapter_text(title='题', content_text=body, db=None) == '完整摘要。'
    assert run.await_count == 2
    assert body in run.await_args.kwargs['user_content'] and raw in run.await_args.kwargs['user_content']


@pytest.mark.asyncio
async def test_invalid_after_repair_fails_without_partial_summary(monkeypatch):
    run = AsyncMock(return_value=SimpleNamespace(text='{"summary": "缺少结尾'))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', run)
    assert await summarize_chapter_text(title='题', content_text='正文。' * 100, db=None) == ''
    assert run.await_count == 2
