"""Bounded, persistent logical model attempts for the legacy run protocol.

A reservation is committed by the runner before entering a model phase. It
does not assert that a provider HTTP request ran exactly once: preparation,
tools and a critic may each contain their own bounded calls. Unknown outcomes
consume the original budget, and old runs never acquire an invented history.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.generation_run import CriticReport
from app.models.project import ChapterVariant


KEY = 'attempt_journal'
PHASE_LIMIT = 3
DELIVERIES = frozenset({'drafting', 'critic', 'draft_history', 'finalize'})
KINDS = frozenset({'draft', 'variant_draft', 'critic', 'variant_critic', 'rewrite'})
REVIEW_KINDS = frozenset({'critic', 'variant_critic'})
STOP_REASONS = frozenset({
    'model_call_history_unverified', 'model_call_delivery_unverified',
    'model_call_budget_exhausted',
})


class AttemptReviewRequired(ValueError):
    def __init__(self, reason='model_call_history_unverified'):
        self.reason = reason
        super().__init__(reason)


def digest(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise AttemptReviewRequired() from None
    return hashlib.sha256(encoded).hexdigest()


def delivery_digest(checkpoint):
    return digest({key: value for key, value in checkpoint.items() if key in DELIVERIES})


def initial_journal(run, *, bvsr_enabled, variant_count):
    """Only for creation in the same transaction as the fixed request."""
    return {
        'version': 1, 'run_id': str(run.id), 'project_id': str(run.project_id),
        'chapter_id': str(run.chapter_id) if run.chapter_id else None,
        'rewrite_limit': run.max_rewrite_count, 'phase_limit': PHASE_LIMIT,
        'bvsr_enabled': bvsr_enabled, 'variant_count': variant_count,
        'attempt_count': 0, 'attempts': [], 'delivery_sha256': delivery_digest({}),
    }


def checkpoint_updated(checkpoint, phase):
    """Seal an intentional output update; never normalize a damaged journal.

    An unfinished reservation keeps its separate before-delivery digest, so
    committing a candidate without confirming the call still stops recovery.
    """
    journal = checkpoint.get(KEY)
    if phase in DELIVERIES and isinstance(journal, dict) and journal.get('version') == 1:
        journal['delivery_sha256'] = delivery_digest(checkpoint)


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _time(value):
    if not isinstance(value, str):
        raise AttemptReviewRequired()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AttemptReviewRequired() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AttemptReviewRequired()
    return parsed


def _identity(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def require_journal(run):
    cp = run.checkpoint_data
    journal = cp.get(KEY) if isinstance(cp, dict) else None
    keys = {'version', 'run_id', 'project_id', 'chapter_id', 'rewrite_limit', 'phase_limit',
            'bvsr_enabled', 'variant_count', 'attempt_count', 'attempts', 'delivery_sha256'}
    if not isinstance(journal, dict) or set(journal) != keys:
        raise AttemptReviewRequired()
    if (type(journal['version']) is not int or journal['version'] != 1
            or journal['run_id'] != str(run.id) or journal['project_id'] != str(run.project_id)
            or journal['chapter_id'] != (str(run.chapter_id) if run.chapter_id else None)
            or type(journal['rewrite_limit']) is not int or not 0 <= journal['rewrite_limit'] <= 8
            or type(run.max_rewrite_count) is not int or journal['rewrite_limit'] != run.max_rewrite_count
            or type(journal['phase_limit']) is not int or journal['phase_limit'] != PHASE_LIMIT
            or type(journal['bvsr_enabled']) is not bool
            or type(journal['variant_count']) is not int or not 1 <= journal['variant_count'] <= 8
            or (not journal['bvsr_enabled'] and journal['variant_count'] != 1)
            or type(run.rewrite_count) is not int or not 0 <= run.rewrite_count <= journal['rewrite_limit']
            or not _hash(journal['delivery_sha256'])
            or journal['delivery_sha256'] != delivery_digest(cp)):
        raise AttemptReviewRequired()
    records = journal['attempts']
    maximum = PHASE_LIMIT * (2 * journal['variant_count'] + journal['rewrite_limit'] + 1) + journal['rewrite_limit']
    if (not isinstance(records, list) or type(journal['attempt_count']) is not int
            or journal['attempt_count'] != len(records) or len(records) > maximum):
        raise AttemptReviewRequired()
    counts, identities, delivered_drafts, rewrites = {}, set(), set(), 0
    base = {'id', 'kind', 'slot', 'number', 'status', 'started_at', 'input_sha256', 'before_delivery_sha256'}
    for index, entry in enumerate(records):
        if not isinstance(entry, dict):
            raise AttemptReviewRequired()
        kind, slot, status = entry.get('kind'), entry.get('slot'), entry.get('status')
        if (not isinstance(kind, str) or kind not in KINDS or type(slot) is not int
                or not isinstance(status, str)):
            raise AttemptReviewRequired()
        extra = ({'finished_at', 'output_sha256'} if status == 'returned' else
                 {'finished_at', 'reason'} if status == 'interrupted' else set())
        if kind in REVIEW_KINDS and status == 'returned':
            extra.add('report_id')
        if (set(entry) != base | extra or status not in {'started', 'returned', 'interrupted'}
                or not _identity(entry['id']) or entry['id'] in identities
                or not _hash(entry['input_sha256']) or not _hash(entry['before_delivery_sha256'])
                or (status == 'started' and index != len(records) - 1)):
            raise AttemptReviewRequired()
        identities.add(entry['id'])
        started = _time(entry['started_at'])
        if status != 'started' and _time(entry['finished_at']) < started:
            raise AttemptReviewRequired()
        if status == 'returned' and (not _hash(entry['output_sha256'])
                or (kind in REVIEW_KINDS and entry['report_id'] != entry['id'])):
            raise AttemptReviewRequired()
        if status == 'interrupted' and entry['reason'] != 'unconfirmed_on_resume':
            raise AttemptReviewRequired()
        if ((kind == 'draft' and slot != 0)
                or (kind == 'variant_draft' and not 1 <= slot < journal['variant_count'])
                or (kind == 'variant_critic' and not (journal['bvsr_enabled'] and 0 <= slot < journal['variant_count']))
                or (kind in {'critic', 'rewrite'} and slot != rewrites)):
            raise AttemptReviewRequired()
        key = (kind, slot)
        counts[key] = counts.get(key, 0) + 1
        if (type(entry['number']) is not int or entry['number'] != counts[key]
                or counts[key] > (1 if kind == 'rewrite' else PHASE_LIMIT)
                or key in delivered_drafts):
            raise AttemptReviewRequired()
        if kind in {'draft', 'variant_draft'} and status == 'returned':
            delivered_drafts.add(key)
        if kind == 'rewrite':
            rewrites += 1
    if rewrites != run.rewrite_count:
        raise AttemptReviewRequired()
    if any(key in cp for key in DELIVERIES) and not any(e['status'] == 'returned' for e in records):
        raise AttemptReviewRequired()
    return journal


def _store(run, journal):
    cp = deepcopy(run.checkpoint_data)
    cp[KEY] = journal
    run.checkpoint_data = cp
    run.updated_at = datetime.now(timezone.utc)


def reserve(run, kind, slot, inputs):
    journal = deepcopy(require_journal(run))
    records = journal['attempts']
    if records and records[-1]['status'] == 'started':
        raise AttemptReviewRequired('model_call_delivery_unverified')
    previous = [entry for entry in records if (entry['kind'], entry['slot']) == (kind, slot)]
    if (kind not in KINDS or type(slot) is not int
            or (kind == 'draft' and slot != 0)
            or (kind == 'variant_draft' and not 1 <= slot < journal['variant_count'])
            or (kind == 'variant_critic' and not (journal['bvsr_enabled'] and 0 <= slot < journal['variant_count']))
            or (kind in {'critic', 'rewrite'} and slot != run.rewrite_count)):
        raise AttemptReviewRequired()
    if kind in {'draft', 'variant_draft'} and any(e['status'] == 'returned' for e in previous):
        raise AttemptReviewRequired('model_call_delivery_unverified')
    if (len(previous) >= (1 if kind == 'rewrite' else PHASE_LIMIT)
            or (kind == 'rewrite' and run.rewrite_count >= journal['rewrite_limit'])):
        raise AttemptReviewRequired('model_call_budget_exhausted')
    record = {
        'id': str(uuid4()), 'kind': kind, 'slot': slot, 'number': len(previous) + 1,
        'status': 'started', 'started_at': datetime.now(timezone.utc).isoformat(),
        'input_sha256': digest(inputs), 'before_delivery_sha256': delivery_digest(run.checkpoint_data),
    }
    records.append(record)
    journal['attempt_count'] = len(records)
    if kind == 'rewrite':
        run.rewrite_count += 1
    _store(run, journal)
    require_journal(run)
    return record['id']


def finish(run, attempt_id, output):
    journal = deepcopy(require_journal(run))
    record = journal['attempts'][-1] if journal['attempts'] else {}
    if record.get('id') != attempt_id or record.get('status') != 'started':
        raise AttemptReviewRequired('model_call_delivery_unverified')
    record.update(status='returned', output_sha256=digest(output),
                  finished_at=max(datetime.now(timezone.utc), _time(record['started_at'])).isoformat())
    if record['kind'] in REVIEW_KINDS:
        record['report_id'] = attempt_id
    _store(run, journal)


async def reconcile_interrupted(run, db):
    """Called only after acquiring physical execution and checking sources.

    Look only at this run's candidate/report identities, never whole books or
    manuscript bodies. A known draft or report without the matching commit
    cannot be treated as an unused call.
    """
    journal = deepcopy(require_journal(run))
    if not journal['attempts'] or journal['attempts'][-1]['status'] != 'started':
        return
    record = journal['attempts'][-1]
    if record['before_delivery_sha256'] != delivery_digest(run.checkpoint_data):
        raise AttemptReviewRequired('model_call_delivery_unverified')
    verified_reports = [UUID(e['id']) for e in journal['attempts']
                        if e['kind'] in REVIEW_KINDS and e['status'] == 'returned']
    unexpected_report = (await db.execute(select(CriticReport.id).where(
        CriticReport.run_id == run.id, CriticReport.id.not_in(verified_reports),
    ).limit(1))).scalar_one_or_none()
    if unexpected_report is not None:
        raise AttemptReviewRequired('model_call_delivery_unverified')
    if record['kind'] in {'draft', 'variant_draft'}:
        query = select(ChapterVariant.id).where(
            ChapterVariant.run_id == run.id, ChapterVariant.chapter_id == run.chapter_id,
        )
        if record['kind'] == 'variant_draft':
            query = query.where(ChapterVariant.variant_idx == record['slot'])
        existing = (await db.execute(query.limit(1))).scalar_one_or_none()
        if existing is not None:
            raise AttemptReviewRequired('model_call_delivery_unverified')
    record.update(status='interrupted', reason='unconfirmed_on_resume',
                  finished_at=max(datetime.now(timezone.utc), _time(record['started_at'])).isoformat())
    _store(run, journal)
    await db.commit()
