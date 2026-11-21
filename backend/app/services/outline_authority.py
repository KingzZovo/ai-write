"""Distinguish an author's text revision from its older extracted structure."""
from copy import deepcopy
import hashlib

TEXT_REVISION = '_author_text_revision'
CONTROL_FIELDS = {'title', 'chapter_id', 'chapter_idx', 'volume_id', 'volume_idx',
                  'chapter_count', 'word_count_target', 'target_word_count'}


def record_text_edit(old: dict | None, new: dict) -> dict:
    result = deepcopy(new)
    old = old or {}
    raw = result.get('raw_text')
    has_structure = any(key not in CONTROL_FIELDS | {'raw_text', 'volume_plan', TEXT_REVISION, '_consistency_report'} for key in result)
    plan = result.get('volume_plan')
    has_structure = has_structure or (isinstance(plan, list) and any(isinstance(item, dict) and any(key not in ('idx', 'est_chapters') for key in item) for item in plan))
    if isinstance(raw, str) and (old.get(TEXT_REVISION) or (has_structure and raw != old.get('raw_text'))):
        result[TEXT_REVISION] = hashlib.sha256(raw.encode()).hexdigest()
    else:
        result.pop(TEXT_REVISION, None)
    return result


def outline_for_generation(value):
    if not isinstance(value, dict):
        return _without_order_metadata(value)
    if not value.get(TEXT_REVISION):
        return _without_order_metadata(value)
    # The original structure remains in storage. Only this prompt projection
    # suppresses it, so a prior synopsis cannot overrule the author's revision.
    result = {key: deepcopy(value[key]) for key in CONTROL_FIELDS if key in value}
    result['raw_text'] = value.get('raw_text', '')
    if isinstance(value.get('volume_plan'), list):
        result['volume_plan'] = [{key: item[key] for key in ('idx', 'est_chapters') if key in item}
                                 for item in value['volume_plan'] if isinstance(item, dict)]
    return result


def _without_order_metadata(value):
    from app.services.outline_order import REF, UNAVAILABLE
    if isinstance(value, dict):
        if value.get(UNAVAILABLE) or (REF in value and not value[REF]):
            return {}
        result = {key: _without_order_metadata(item) for key, item in value.items() if key not in (REF, UNAVAILABLE)}
        if (any(key in value for key in ('chapter_id', 'volume_id', REF)) and any(
                key not in CONTROL_FIELDS | {'raw_text', TEXT_REVISION} and not key.startswith('_') and bool(value[key])
                for key in value)):
            # Once the saved structure owns positions, the unedited original
            # model response may still contain old labels or discarded entries.
            # Keep it in storage. Text revisions use the separate branch above.
            result.pop('raw_text', None)
        return result
    if isinstance(value, list):
        return [_without_order_metadata(item) for item in value if not isinstance(item, dict) or (
            not item.get(UNAVAILABLE) and (REF not in item or item[REF]))]
    return deepcopy(value)
