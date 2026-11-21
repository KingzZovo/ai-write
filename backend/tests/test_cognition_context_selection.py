"""Evidence-preserving excerpts recall original labels, never merge identities."""
from app.services.character_cognition import serialize_for_prompt
import pytest


def event(chapter, fact, evidence=None, kind='learns'):
    return dict(chapter_idx=chapter, kind=kind, fact=fact, evidence=evidence or fact)


def test_verified_evidence_is_rendered_in_full_including_line_breaks():
    quote = '沈青递来回执。\n陈川读罢，这才知道信已送到。'
    rendered = serialize_for_prompt({'陈川': {'history': [event(4, '父亲已经收信。', quote)]}})
    assert quote in rendered and '父亲已经收信。' in rendered


def test_unrelated_later_cast_cannot_evict_current_character_history():
    ledger = {'陈川': {'history': [event(1, '父亲已经收信。', '陈川读到父亲的回信。')]}}
    ledger.update({f'旁人{i}': {'history': [event(i + 10, f'无关支线消息{i}。' * 3)]} for i in range(500)})
    rendered = serialize_for_prompt(ledger, max_chars=450, priority_names=['陈川'])
    assert '陈川读到父亲的回信。' in rendered
    assert len(rendered) <= 450 and '节选不代表全部认知' in rendered


def test_current_text_labels_prioritize_original_records_without_name_substring_guess():
    ledger = {'陈川': {'history': [event(100, '属于陈川的消息。', '陈川读到属于他的回信。' * 4)]},
              '陈川北': {'history': [event(1, '属于陈川北的消息。', '陈川北读到属于他的回信。' * 4)]}}
    rendered = serialize_for_prompt(ledger, max_chars=250, context_text='陈川北独自回到客栈。')
    assert '陈川北·本章认知记录' in rendered
    assert '陈川·本章认知记录' not in rendered


def test_multiple_recalled_characters_each_get_recent_record_before_extra_history():
    ledger = {'陈川': {'history': [event(i, f'陈川在第{i}章得知的消息。') for i in range(1, 30)]},
              '沈青': {'history': [event(1, '沈青不知道父亲已经收到信。', kind='still_unknown')]}}
    rendered = serialize_for_prompt(ledger, max_chars=450, priority_names=['陈川', '沈青'])
    assert '陈川在第29章得知的消息。' in rendered
    assert '沈青·当时未知记录' in rendered


def test_quote_is_not_separated_from_its_claim_to_fit_budget():
    rendered = serialize_for_prompt({'陈川': {'history': [event(2, '很短的事实。', '很长的连续原句。' * 150)]}}, max_chars=200)
    assert '很短的事实。' not in rendered and '很长的连续原句。' not in rendered


def test_shared_original_alias_is_never_rewritten_as_selected_card_owner():
    ledger = {'先生': {'history': [event(3, '他听到了铃声。')]}}
    rendered = serialize_for_prompt(ledger, priority_names=['陈川', '先生'])
    assert '先生·本章认知记录' in rendered and '陈川·' not in rendered


def test_recalled_latest_evidence_overflow_is_explicit_instead_of_older_unknown():
    from app.services.context_errors import RequiredContextUnavailable
    ledger = {'陈川': {'history': [event(1, '信是否送到。', kind='still_unknown'),
                                 event(2, '父亲已经收信。', '完整获知过程。' * 100)]}}
    with pytest.raises(RequiredContextUnavailable, match='budget'):
        serialize_for_prompt(ledger, max_chars=200, priority_names=['陈川'])


@pytest.mark.parametrize('unrelated', [0, 300, 5000])
def test_long_history_keeps_known_later_discovery_and_reader_gap(unrelated):
    from time import perf_counter
    import json
    ledger = {'陈川': {'history': [event(1, '父亲已经收信。', '陈川不知道父亲已经收信。', kind='still_unknown'),
                                 event(2, '那封信早已送到父亲手里。', '陈川读罢回执，这才知道信已送到。')]},
              '__reader__': {'history': [event(1, '父亲已经收信而陈川不知。', '陈川不知道父亲已经收信。')]}}
    ledger.update({f'旁人{i}': {'history': [event(i + 10, f'无关支线消息{i}。' * 3)]} for i in range(unrelated)})
    start = perf_counter()
    rendered = serialize_for_prompt(ledger, max_chars=450, context_text='陈川今天回来。')
    elapsed = perf_counter() - start
    assert '陈川读罢回执，这才知道信已送到。' in rendered
    assert '读者·本章认知记录' in rendered and '陈川·当时未知记录' in rendered
    assert len(rendered) <= 450
    print(json.dumps(dict(unrelated=unrelated, chars=len(rendered), elapsed_seconds=elapsed)))


def test_skewed_history_keeps_every_observation_when_budget_fits():
    from time import perf_counter
    import json
    ledger = {'陈川': {'history': [event(i + 1, f'陈川获知的第{i}件事。') for i in range(5000)]}}
    ledger.update({f'旁人{i}': {'history': [event(1, f'旁人{i}获知的唯一消息。')]} for i in range(4999)})
    start = perf_counter()
    rendered = serialize_for_prompt(ledger, max_chars=1_000_000, priority_names=list(ledger))
    elapsed = perf_counter() - start
    assert rendered.count('·本章认知记录：') == 9999
    assert '陈川获知的第4999件事。' in rendered and '旁人4998获知的唯一消息。' in rendered
    assert len(rendered) <= 1_000_000
    print(json.dumps(dict(focus_labels=len(ledger), observations=9999, chars=len(rendered), elapsed_seconds=elapsed)))
