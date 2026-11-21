"""Review delivery and draft identity, independent of literary acceptance."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

REVIEW_VERSION = 1
COMPLETE = 'complete'
INCOMPLETE = 'incomplete'
UNKNOWN = 'unknown'
NORMAL_FINISHES = {'stop', 'end_turn', 'stop_sequence'}
CRITIC_SEVERITIES = {'hard', 'soft', 'info'}


def draft_digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def parse_review_json(result: Any) -> Any:
    """Accept one complete JSON value, optionally enclosed in a code fence.

    No JSON repair or substring extraction: either can turn a truncated list
    of findings into an apparently valid empty/partial review.
    """
    if getattr(result, 'finish_reason', None) not in NORMAL_FINISHES:
        raise ValueError('review_response_not_finished')
    raw = getattr(result, 'text', None)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('review_response_empty')
    text = raw.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if len(lines) < 3 or lines[0].strip() not in {'```', '```json'} or lines[-1].strip() != '```':
            raise ValueError('review_response_invalid_json')
        text = '\n'.join(lines[1:-1])
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('review_duplicate_json_key')
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError('review_nonfinite_json_number')

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('review_nonfinite_json_number')
        return number

    try:
        return json.loads(text, object_pairs_hook=unique_object,
                          parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, TypeError) as exc:
        raise ValueError('review_response_invalid_json') from exc


def validate_critic_issues(value: Any, *, draft: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError('review_issues_missing')
    for issue in value:
        if not isinstance(issue, dict) or issue.get('severity') not in CRITIC_SEVERITIES:
            raise ValueError('review_issue_invalid')
        for key in ('category', 'desc'):
            if not isinstance(issue.get(key), str) or not issue[key].strip():
                raise ValueError('review_issue_invalid')
        if not isinstance(issue.get('location'), str):
            raise ValueError('review_issue_quote_missing')
        if issue['severity'] in {'hard', 'soft'}:
            quote = issue['location']
            if not quote.strip() or (draft is not None and quote not in draft):
                raise ValueError('review_issue_quote_not_found')
        if 'suggestion' in issue and not isinstance(issue['suggestion'], str):
            raise ValueError('review_issue_invalid')
    return value


def parse_checker_issues(result: Any, *, draft: str) -> list[dict[str, Any]]:
    issues = parse_review_json(result)
    if not isinstance(issues, list):
        raise ValueError('review_issue_array_required')
    for issue in issues:
        if not isinstance(issue, dict) or issue.get('severity') not in {'critical', 'high', 'medium', 'low'}:
            raise ValueError('review_issue_invalid')
        if not isinstance(issue.get('description'), str) or not issue['description'].strip():
            raise ValueError('review_issue_invalid')
        if not isinstance(issue.get('location'), str) or not issue['location'].strip() or issue['location'] not in draft:
            raise ValueError('review_issue_quote_not_found')
        for key in ('suggestion', 'type', 'character'):
            if key in issue and not isinstance(issue[key], str):
                raise ValueError('review_issue_invalid')
    return issues


def _stages_complete(stages: Any) -> bool:
    if not isinstance(stages, list) or not stages:
        return False
    statuses = {}
    for stage in stages:
        if not isinstance(stage, dict):
            return False
        name, status = stage.get('name'), stage.get('status')
        if not isinstance(name, str) or not name.strip() or name in statuses:
            return False
        if not isinstance(status, str) or status not in {COMPLETE, 'not_applicable'}:
            return False
        statuses[name] = status
    required = set(statuses) & {'critic', 'critic_hard', 'critic_soft'}
    if required not in ({'critic'}, {'critic_hard', 'critic_soft'}):
        return False
    # Optional observations cannot stand in for a skipped LLM review stream.
    return all(statuses[name] == COMPLETE for name in required)


def make_critic_report(
    draft: str, *, issues: list[dict], stages: list[dict], observations: list[dict] | None = None,
) -> dict[str, Any]:
    complete = _stages_complete(stages)
    return {
        'review_version': REVIEW_VERSION,
        'review_status': COMPLETE if complete else INCOMPLETE,
        'draft_sha256': draft_digest(draft),
        'stages': stages, 'issues': issues, 'observations': observations or [],
        'hard_count': sum(i['severity'] == 'hard' for i in issues),
        'soft_count': sum(i['severity'] == 'soft' for i in issues),
        'info_count': sum(i['severity'] == 'info' for i in issues),
        'editorial_status': 'needs_author_review',
    }


def incomplete_critic_report(draft: str, *, code: str = 'review_call_failed') -> dict[str, Any]:
    return make_critic_report(draft, issues=[], stages=[{'name': 'critic', 'status': INCOMPLETE, 'code': code}])


def critic_review_status(report: Any, draft: str | None = None) -> str:
    """Validate a receipt before ranking, rewriting or completing a run."""
    if not isinstance(report, dict) or type(report.get('review_version')) is not int or report['review_version'] != REVIEW_VERSION:
        return UNKNOWN
    if report.get('review_status') != COMPLETE:
        return INCOMPLETE
    digest = report.get('draft_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        return INCOMPLETE
    if draft is not None and (not draft.strip() or digest != draft_digest(draft)):
        return INCOMPLETE
    if not _stages_complete(report.get('stages')):
        return INCOMPLETE
    try:
        issues = validate_critic_issues(report.get('issues'), draft=draft)
    except (TypeError, ValueError):
        return INCOMPLETE
    for severity in CRITIC_SEVERITIES:
        count = report.get(f'{severity}_count')
        if type(count) is not int or count != sum(i['severity'] == severity for i in issues):
            return INCOMPLETE
    return COMPLETE


def critic_next_action(report: Any, draft: str, rewrites: int, limit: int) -> str:
    if critic_review_status(report, draft) != COMPLETE:
        return 'review_incomplete'
    if report['hard_count']:
        return 'rewrite' if rewrites < limit else 'rewrite_limit_reached'
    if report['soft_count']:
        return 'issues_need_review'
    return 'finalize'
