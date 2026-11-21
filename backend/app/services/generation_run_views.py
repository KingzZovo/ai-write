"""Readable run snapshots, including damaged legacy checkpoints.

Projection must not certify or repair model history. Preserve unparsed data
for inspection; only well-typed fields may supply the derived display values.
"""
from app.services.review_contract import critic_review_status


def checkpoint_payload(value):
    return value if isinstance(value, dict) else {'unparsed_checkpoint': value}


def checkpoint_section(checkpoint, name):
    value = checkpoint.get(name) if isinstance(checkpoint, dict) else None
    return value if isinstance(value, dict) else {}


def checkpoint_text(checkpoint, section='drafting', field='text'):
    value = checkpoint_section(checkpoint, section).get(field)
    return value if isinstance(value, str) else ''


def run_review_status(checkpoint):
    return critic_review_status(checkpoint_section(checkpoint, 'critic').get('report'),
                                checkpoint_text(checkpoint))


def run_stop_reason(checkpoint):
    value = checkpoint_section(checkpoint, 'awaiting_review').get('reason')
    return value if isinstance(value, str) else None
