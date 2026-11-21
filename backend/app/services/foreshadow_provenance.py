"""Content fingerprints for the foreshadow record reviewed by the model."""
import hashlib
import json
from collections.abc import Mapping
from typing import Any

# A planted clue can pay off without passing a model-score threshold.
REVIEWABLE_STATUSES = ('planted', 'ripening', 'ready')

# Proximity is a legacy advisory score, not part of the payoff contract.
REVIEW_SOURCE_FIELDS = (
    'type', 'description', 'planted_chapter', 'resolve_conditions_json',
    'resolution_blueprint_json', 'status', 'resolved_chapter', 'source',
)


def foreshadow_source_hash(record: Mapping[str, Any]) -> str:
    source = {field: record[field] for field in REVIEW_SOURCE_FIELDS}
    encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
