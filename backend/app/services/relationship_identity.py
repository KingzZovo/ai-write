"""Stable identities for exact chapter relationship occurrences."""
import hashlib
import json
import re


def relationship_wording(rel_type, raw_type=None):
    """Never collapse qualifications or different labels into a canonical type."""
    return raw_type if isinstance(raw_type, str) and raw_type.strip() else rel_type


def relationship_record_id(project_id, source, target, rel_type, chapter_id, source_hash, raw_type=None):
    if project_id is None or chapter_id is None:
        raise ValueError('invalid_relationship_record_identity')
    values = [str(project_id), source, target, rel_type, str(chapter_id), source_hash,
              relationship_wording(rel_type, raw_type)]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError('invalid_relationship_record_identity')
    body = json.dumps(values, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return 'rel_' + hashlib.sha256(body.encode()).hexdigest()


def valid_replacement_ids(value):
    return (isinstance(value, list)
            and all(isinstance(item, str) and re.fullmatch(r'rel_[0-9a-f]{64}', item) for item in value)
            and len(value) == len(set(value)))
