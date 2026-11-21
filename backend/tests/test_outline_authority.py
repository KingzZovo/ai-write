from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.services.outline_authority import record_text_edit, outline_for_generation, TEXT_REVISION
from app.services.context_pack import ContextPack
from app.services.outline_generator import OutlineGenerator, extract_chapter_breakdown
from app.services.chapter_outline_expander import _validate_and_normalize, _format_user_prompt
from app.services.outline_readiness import has_meaningful_outline_content
from app.services.scene_orchestrator import SceneOrchestrator

OLD = {'title': '交书', 'raw_text': '旧稿', 'summary': '母亲已经去世',
    'key_events': ['办葬礼'], 'chapter_idx': 1, 'word_count_target': 1800}
NEW_TEXT = '母亲还活着。林禾打电话告诉母亲，菜谱已经收好。'


def revision(): return record_text_edit(OLD, {**OLD, 'raw_text': NEW_TEXT})


def test_edit_preserves_source_structure_but_excludes_it_from_generation():
    before = deepcopy(OLD)
    saved = revision()
    assert saved['summary'] == '母亲已经去世'
    projected = outline_for_generation(saved)
    assert projected['raw_text'] == NEW_TEXT and projected['word_count_target'] == 1800
    assert 'summary' not in projected and 'key_events' not in projected
    assert TEXT_REVISION not in projected and OLD == before
    assert record_text_edit(saved, {k: v for k, v in saved.items() if k != TEXT_REVISION})[TEXT_REVISION]


def test_no_revision_does_not_discard_current_generated_structure():
    assert outline_for_generation(OLD) == OLD
    generated = {'summary': '完整新细纲', 'key_events': ['归还菜谱']}
    assert record_text_edit(revision(), generated) == generated


def test_revision_plan_keeps_counts_without_old_plot_details():
    old = {**OLD, 'volume_plan': [{'idx': 1, 'est_chapters': 3, 'core_conflict': '母亲已经去世'}]}
    value = outline_for_generation(record_text_edit(old, {**old, 'raw_text': NEW_TEXT}))
    assert value['volume_plan'] == [{'idx': 1, 'est_chapters': 3}]


def test_writer_render_and_planner_read_the_same_author_revision():
    saved = revision()
    pack = ContextPack()
    pack.current_outline = saved
    pack.volume_outline = saved
    for text in (pack._render_chapter_outline_block(saved), pack._render_volume_outline_block(),
                 SceneOrchestrator._extract_chapter_outline_text(pack)):
        assert NEW_TEXT in text and '母亲已经去世' not in text and '办葬礼' not in text


def test_book_anchor_and_chapter_breakdown_do_not_restore_old_plot():
    saved = revision()
    saved['premise'] = '母亲已经去世'
    saved['chapter_summaries'] = [{'chapter_idx': 1, 'summary': '办葬礼'}]
    assert OutlineGenerator._book_premise_digest(saved) == NEW_TEXT
    assert extract_chapter_breakdown(saved) == {}


def test_validated_expansion_replaces_text_authority_with_current_structure():
    saved = revision()
    saved['state_changes'] = {'characters': [{'name': '母亲', 'change': '去世'}]}
    chapter = SimpleNamespace(chapter_idx=1, title='交书', target_word_count=1800)
    result = _validate_and_normalize({'summary': '林禾向母亲报平安。', 'key_events': ['打电话']}, chapter, saved)
    assert result['summary'] == '林禾向母亲报平安。'
    assert result['key_events'] == ['打电话']
    assert 'raw_text' not in result and TEXT_REVISION not in result
    assert '母亲已经去世' not in str(result) and '去世' not in str(result)


def test_cleared_author_text_does_not_make_old_structure_ready_again():
    saved = record_text_edit(OLD, {**OLD, 'raw_text': ''})
    assert not has_meaningful_outline_content(saved)


@pytest.mark.asyncio
async def test_actual_chapter_generator_does_not_send_stale_fields():
    from unittest.mock import AsyncMock
    generator = object.__new__(OutlineGenerator)
    generator.project_id = None
    generator.chapter_naming_directive = ''
    generator.router = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(text='{"summary":"新细纲"}')))
    await generator.generate_chapter_outline(revision(), revision(), 1, target_word_count=1800)
    messages = generator.router.generate.call_args.kwargs['messages']
    user = messages[1]['content']
    assert NEW_TEXT in user and '母亲已经去世' not in user and '办葬礼' not in user
    assert TEXT_REVISION not in user


def test_expander_prompt_excludes_old_contract_and_uses_new_text():
    saved = revision()
    context = {'book_outline': saved, 'volume_outline': saved, 'stub': saved, 'prev_outline': saved,
        'prev_text': '已完成正文。', 'target_word_count': 1800, 'active_foreshadows': []}
    text = _format_user_prompt(context, SimpleNamespace(chapter_idx=1, target_word_count=1800))
    assert NEW_TEXT in text and '母亲已经去世' not in text and '办葬礼' not in text


def test_unedited_generated_structured_chapter_remains_available():
    pack = ContextPack()
    text = pack._render_chapter_outline_block(OLD)
    assert OLD['summary'] in text and '办葬礼' in text
