from app.services.chapter_acceptance import ChapterAcceptance, evaluation_passes
import pytest


@pytest.mark.parametrize('result', [None, {'overall': 9, 'parse_failed': True}, {'overall': float('nan')},
                                    {'overall': True}, {'overall': 11}, {'overall': 7.9}])
def test_untrusted_or_below_threshold_not_accepted(result):
    assert not evaluation_passes(result)


@pytest.mark.parametrize('tag', ['chapter_completion_violation', 'space_rule_violation', 'cognition_violation'])
def test_high_average_cannot_mask_severe_causal_issue(tag):
    assert not evaluation_passes({'overall': 9.5, 'issues': [{'violation_type': tag, 'severity': 'high'}]})
    assert evaluation_passes({'overall': 9.5, 'issues': [{'violation_type': tag, 'severity': 'low'}]})


def test_aesthetic_preference_does_not_block():
    assert evaluation_passes({'overall': 8.8, 'issues': [{'violation_type': 'expression_contract_violation', 'severity': 'high'}]})


def test_verdict_only_applies_to_evaluated_body():
    receipt = ChapterAcceptance(required=True)
    assert receipt.status('原稿。') == 'needs_review'
    receipt.record('原稿。', {'overall': 8.8})
    assert receipt.status('原稿。') == 'completed'
    assert receipt.status('润色后删掉关键事件。') == 'needs_review'
    assert receipt.status('原稿。', {'persisted_on_block': True}) == 'draft'


def test_optional_review_preserves_existing_behavior():
    assert ChapterAcceptance(required=False).status('原稿。') == 'completed'
    assert ChapterAcceptance(required=True).status('原稿还没写') == 'draft'


def test_real_high_scoring_duplicate_handoff_requires_revision_and_review():
    import json
    from pathlib import Path
    from app.services.auto_revise import should_revise
    root = Path(__file__).resolve().parents[2]
    sample = root / 'docs/evaluation/runtime-chain-20261006/read-session'
    result = json.loads((sample / 'evaluations.json').read_text())[0]
    body = (sample / 'saved-chapter.txt').read_text()
    assert result['overall'] == 9.8
    assert result['issues'][0]['quote'] in body
    assert should_revise(result)
    assert not evaluation_passes(result)
    receipt = ChapterAcceptance(required=True)
    receipt.record(body, result)
    assert receipt.status(body) == 'needs_review'


@pytest.mark.parametrize('change', [
    {'quote_verified': False}, {'quote_verified': 'true'}, {'quote': ''},
    {'description': ''}, {'description': '[space_rule_violation]'},
    {'description': '  [space_rule_violation]  '},
    {'violation_type': 'expression_contract_violation'},
])
def test_high_score_does_not_trigger_rewrite_from_unlocated_or_aesthetic_advice(change):
    from app.services.auto_revise import should_revise
    issue = {'violation_type': 'space_rule_violation', 'severity': 'low',
             'description': '人物位置与动作承接不一致。', 'quote': '他还站在屋里。', 'quote_verified': True}
    issue.update(change)
    result = {'overall': 9.8, 'issues': [issue]}
    assert not should_revise(result)
    assert evaluation_passes(result)


def test_parse_failure_never_rewrites_even_with_a_located_issue():
    from app.services.auto_revise import should_revise
    result = {'overall': 9.8, 'parse_failed': True, 'issues': [{
        'violation_type': 'space_rule_violation', 'severity': 'low',
        'description': '人物位置冲突。', 'quote': '他还站在屋里。', 'quote_verified': True,
    }]}
    assert not should_revise(result)
    assert not evaluation_passes(result)


def test_model_invented_quote_cannot_trigger_high_score_revision():
    import json
    from app.services.chapter_evaluator import _parse_evaluation_response
    from app.services.auto_revise import should_revise
    payload = {key: {'score': 9.8, 'issues': []} for key in (
        'plot_coherence', 'character_consistency', 'style_adherence',
        'narrative_pacing', 'foreshadow_handling',
    )}
    payload['plot_coherence']['issues'] = [{
        'description': '声称人物重复交接。', 'violation_type': 'mundane_plausibility_violation',
        'severity': 'low', 'quote': '这句话根本不在正文里。', 'quote_verified': True,
    }]
    result = _parse_evaluation_response(json.dumps(payload), chapter_text='沈青把钥匙递给陈川。')
    assert result.issues[0]['quote_verified'] is False
    assert not should_revise(result)
