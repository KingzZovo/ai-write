"""Canonical source for a saved manuscript, shared by durable executions."""
import json

from app.services.generation_submissions import chapter_material_values
from app.services.production.control import content_hash


def manuscript_source(chapter):
    # Derived summaries have their own CAS and may keep updated_at unchanged.
    # Author revisions and manuscript material remain part of this receipt.
    values = chapter_material_values(chapter)
    values.pop('summary')
    values.pop('summary_source_hash')
    return {'version': 1, 'kind': 'saved_manuscript', 'chapter_id': str(chapter.id),
            'material_hash': content_hash(json.dumps(values, sort_keys=True, ensure_ascii=False,
                separators=(',', ':'), allow_nan=False))}
