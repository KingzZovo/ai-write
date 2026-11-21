"""Explicit, synthetic version-1 call history for advanced run fixtures.

Never used to upgrade application rows. Old/missing-history tests deliberately
omit this helper; production resume must not manufacture these records.
"""
from copy import deepcopy
from uuid import UUID

from app.models.generation_run import CriticReport
from app.services import generation_run_attempts as attempts


def verified_history(run, *, bvsr_enabled=False, variant_count=1):
    checkpoint = deepcopy(run.checkpoint_data or {})
    consumed = run.rewrite_count or 0
    run.rewrite_count = 0
    run.checkpoint_data = {attempts.KEY: attempts.initial_journal(
        run, bvsr_enabled=bvsr_enabled, variant_count=variant_count,
    )}
    draft = checkpoint.get('drafting')
    if draft is not None:
        identity = attempts.reserve(run, 'draft', 0, {'fixture': 'prepared draft'})
        attempts.finish(run, identity, draft)
    for index in range(consumed):
        identity = attempts.reserve(run, 'rewrite', index, {'fixture': 'known prior revision'})
        attempts.finish(run, identity, draft or {})
    reports = []
    critic = checkpoint.get('critic')
    if isinstance(critic, dict) and isinstance(critic.get('report'), dict):
        report = critic['report']
        identity = attempts.reserve(run, 'critic', consumed, {'fixture': 'known prior review'})
        attempts.finish(run, identity, report)
        reports.append(CriticReport(id=UUID(identity), run_id=run.id, round=critic.get('round', consumed + 1),
            issues_json=report.get('issues', []), review_json={k: v for k, v in report.items() if k != 'issues'}))
    checkpoint[attempts.KEY] = run.checkpoint_data[attempts.KEY]
    attempts.checkpoint_updated(checkpoint, 'drafting')
    run.checkpoint_data = checkpoint
    attempts.require_journal(run)
    return reports
