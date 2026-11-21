"""Keep outline references attached to saved chapter identities across moves.

REF belongs to the application, not the model. A present but empty reference
means that a legacy association could not be proved; it must never fall back
to the next chapter occupying the same number. Narrative strings stay intact.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import load_only

from app.models.project import Chapter, Outline, Volume

REF = '_chapter_id'
UNAVAILABLE = '_order_unavailable'
IDENTITY_FIELDS = {REF, UNAVAILABLE, 'chapter_id', 'volume_id', 'volume_idx', 'global_idx'}


def positive_index(value):
    return type(value) is int and 0 < value <= 2147483647


def volume_matches(content, volume_id, volume_idx):
    if not isinstance(content, dict):
        return False
    if content.get('volume_id') is not None:
        return volume_id is not None and str(content['volume_id']) == str(volume_id)
    return str(content.get('volume_idx')) == str(volume_idx)


def volume_condition(volume_id, volume_idx):
    vid = Outline.content_json['volume_id'].as_string()
    idx = Outline.content_json['volume_idx'].as_string()
    # Empty string is an explicitly unavailable legacy owner. Only an absent
    # (or legacy null) identity retains the old declared-volume lookup.
    return or_(vid == str(volume_id), and_(vid.is_(None), idx == str(volume_idx)))


def stub_payload(value):
    """Compare complete authored stubs, ignoring application identity fields."""
    if not isinstance(value, dict):
        return value
    def clean(item):
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items() if key not in (REF, UNAVAILABLE)}
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    return {key: clean(item) for key, item in value.items() if key not in IDENTITY_FIELDS}


def _reference(item, chapter):
    result = deepcopy(item)
    if REF not in result:
        result[REF] = str(chapter.id) if chapter is not None else None
    if chapter is None:
        result[UNAVAILABLE] = True
    else:
        result.pop(UNAVAILABLE, None)
    return result


class ExactStubs:
    def __init__(self, chapters):
        self.entries = defaultdict(list)
        for chapter in chapters:
            payload = stub_payload(chapter.outline_json)
            if isinstance(payload, dict) and payload:
                self.entries[self.key(payload)].append((payload, chapter))

    @staticmethod
    def key(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).digest()

    def match(self, item):
        payload = stub_payload(item)
        matches = [chapter for stored, chapter in self.entries.get(self.key(payload), []) if stored == payload]
        return matches[0] if len(matches) == 1 else None


def bind_chapter_content(content, chapter, *, volume_idx=None):
    if not isinstance(content, dict) or not content:
        return deepcopy(content)
    result = deepcopy(content)
    original_idx = result.get('chapter_idx', chapter.chapter_idx)
    result.update(chapter_id=str(chapter.id), volume_id=str(chapter.volume_id), chapter_idx=chapter.chapter_idx)
    if REF in result:
        result[REF] = str(chapter.id)
        result.pop(UNAVAILABLE, None)
    if volume_idx is not None and 'volume_idx' in result:
        result['volume_idx'] = volume_idx
    entries = result.get('foreshadows_planted')
    if isinstance(entries, list):
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            if REF in entry:
                known = str(entry[REF]) == str(chapter.id)
            else:
                idx = entry.get('planted_chapter')
                known = idx is None or (positive_index(idx) and idx in (original_idx, chapter.chapter_idx))
            entry = _reference(entry, chapter if known else None)
            if known and 'planted_chapter' in entry:
                entry['planted_chapter'] = chapter.chapter_idx
            entries[index] = entry
    return result


def prepare_author_chapter(old, new, chapter):
    result = deepcopy(new)
    before = old.get('foreshadows_planted') if isinstance(old, dict) else None
    after = result.get('foreshadows_planted')
    if isinstance(after, list):
        before = before if isinstance(before, list) else []
        for item in after:
            if not isinstance(item, dict):
                continue
            exact = [entry for entry in before if isinstance(entry, dict) and stub_payload(entry) == stub_payload(item)]
            for field in (REF, UNAVAILABLE):
                item.pop(field, None)
                if len(exact) == 1 and field in exact[0]:
                    item[field] = exact[0][field]
    # The route's saved chapter owns the new plan, not a client-supplied ID.
    result.pop(REF, None)
    result.pop(UNAVAILABLE, None)
    result['chapter_idx'] = chapter.chapter_idx
    return bind_chapter_content(result, chapter)


def bind_volume_content(content, volume, chapters):
    """Project known identities; recover legacy stubs only by exact content."""
    result = deepcopy(content)
    result.update(volume_id=str(volume.id), volume_idx=volume.volume_idx)
    result.pop(UNAVAILABLE, None)
    by_id = {str(chapter.id): chapter for chapter in chapters}
    exact = ExactStubs(chapters)
    old_indices = defaultdict(list)
    summaries = result.get('chapter_summaries')
    if isinstance(summaries, list):
        for index, item in enumerate(summaries):
            if not isinstance(item, dict):
                continue
            chapter = by_id.get(str(item[REF])) if REF in item else exact.match(item)
            if positive_index(item.get('chapter_idx')):
                old_indices[item['chapter_idx']].append(chapter)
            item = _reference(item, chapter)
            if chapter is not None:
                projected = bind_chapter_content(item, chapter)
                for field in ('chapter_id', 'volume_id'):
                    if field not in item:
                        projected.pop(field, None)
                item = projected
            summaries[index] = item
        # Unavailable historical entries remain available to the author. They
        # are omitted from the prompt projection, not deleted from storage.
        summaries.sort(key=lambda item: (1, 0) if not isinstance(item, dict) or item.get(UNAVAILABLE)
                       else (0, item.get('chapter_idx', 0)))
    block = result.get('foreshadows')
    entries = block.get('planted') if isinstance(block, dict) else None
    if isinstance(entries, list):
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            idx = entry.get('planted_chapter')
            if REF in entry:
                chapter = by_id.get(str(entry[REF]))
            elif idx is None:
                continue  # An unscheduled volume plan remains unscheduled.
            else:
                matches = old_indices.get(idx, []) if positive_index(idx) else []
                chapter = matches[0] if len(matches) == 1 else None
            entry = _reference(entry, chapter)
            if chapter is not None:
                entry['planted_chapter'] = chapter.chapter_idx
            entries[index] = entry
    return result


def prepare_author_volume(old, new, volume, chapters):
    """Bind changed, explicitly numbered author entries to current chapters.

    An unrelated raw-text or title edit cannot silently reassign unresolved
    historical plans. System references supplied by a client are not trusted.
    """
    result = deepcopy(new)
    by_idx = defaultdict(list)
    for chapter in chapters:
        by_idx[chapter.chapter_idx].append(chapter)

    def prepare(before, after, key, *, unscheduled=False):
        if not isinstance(after, list):
            return
        before = before if isinstance(before, list) else []
        for index, item in enumerate(after):
            if not isinstance(item, dict):
                continue
            # First preserve an exact original, even when the list is reordered.
            exact = [entry for entry in before if isinstance(entry, dict) and stub_payload(entry) == stub_payload(item)]
            if len(exact) == 1:
                for field in (REF, UNAVAILABLE):
                    item.pop(field, None)
                    if field in exact[0]:
                        item[field] = exact[0][field]
                continue
            item.pop(REF, None)
            item.pop(UNAVAILABLE, None)
            idx = item.get(key)
            if unscheduled and idx is None:
                continue
            matches = by_idx.get(idx, []) if positive_index(idx) else []
            after[index] = _reference(item, matches[0] if len(matches) == 1 else None)

    prepare(old.get('chapter_summaries'), result.get('chapter_summaries'), 'chapter_idx')
    before = old.get('foreshadows'); after = result.get('foreshadows')
    if isinstance(after, dict):
        prepare(before.get('planted') if isinstance(before, dict) else None,
                after.get('planted'), 'planted_chapter', unscheduled=True)
    return bind_volume_content(result, volume, chapters)


def chapter_stub(content, chapter):
    """No array-position fallback, and no numeric fallback for a declared ID."""
    summaries = content.get('chapter_summaries') if isinstance(content, dict) else None
    if not isinstance(summaries, list):
        return {}
    matches = [item for item in summaries if isinstance(item, dict) and not item.get(UNAVAILABLE) and (
        str(item[REF]) == str(chapter.id) if REF in item else (
            positive_index(item.get('chapter_idx')) and item['chapter_idx'] == chapter.chapter_idx))]
    return deepcopy(matches[0]) if len(matches) == 1 else {}


async def maintain_outline_order(db, project_id, *, previous_volume_indices=None):
    """Caller holds the structure/project lock and commits with the move.

    Read only chapter metadata and plans, never chapter prose. Exact matching
    uses the pre-projection stubs, so a moved legacy stub can identify itself
    even while it still carries its original local chapter number.
    """
    pid = UUID(str(project_id))
    volumes = list((await db.scalars(select(Volume).where(Volume.project_id == pid)
        .options(load_only(Volume.id, Volume.volume_idx)).order_by(Volume.id)
        .execution_options(populate_existing=True, autoflush=False))).all())
    chapters = list((await db.scalars(select(Chapter).join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == pid).options(load_only(Chapter.id, Chapter.volume_id,
            Chapter.chapter_idx, Chapter.outline_json, Chapter.updated_at))
        .order_by(Chapter.id).execution_options(populate_existing=True, autoflush=False))).all())
    outlines = list((await db.scalars(select(Outline).where(Outline.project_id == pid,
        Outline.level.in_(('volume', 'chapter'))).order_by(Outline.id).with_for_update()
        .execution_options(populate_existing=True, autoflush=False))).all())
    by_volume = defaultdict(list)
    for chapter in chapters:
        by_volume[str(chapter.volume_id)].append(chapter)
    volumes_by_id = {str(volume.id): volume for volume in volumes}
    chapters_by_id = {str(chapter.id): chapter for chapter in chapters}
    exact = ExactStubs(chapters)
    previous_volume_indices = previous_volume_indices or {}
    changes = []
    for outline in outlines:
        content = outline.content_json
        if not isinstance(content, dict):
            continue
        if outline.level == 'volume':
            if content.get('volume_id') is not None:
                volume = volumes_by_id.get(str(content['volume_id']))
            else:
                # Legacy explicit volume labels are read against the structure
                # before this move. Exact stubs can also identify an old owner.
                evidence = set()
                summaries = content.get('chapter_summaries')
                for item in summaries if isinstance(summaries, list) else []:
                    if isinstance(item, dict):
                        chapter = exact.match(item)
                        if chapter is not None:
                            evidence.add(str(chapter.volume_id))
                matches = [v for v in volumes if str(previous_volume_indices.get(str(v.id), v.volume_idx))
                           == str(content.get('volume_idx'))]
                volume = volumes_by_id.get(next(iter(evidence))) if len(evidence) == 1 else (
                    matches[0] if not evidence and len(matches) == 1 else None)
            value = bind_volume_content(content, volume, by_volume[str(volume.id)]) if volume else {
                **deepcopy(content), 'volume_id': content.get('volume_id') or '', UNAVAILABLE: True}
        else:
            chapter = (chapters_by_id.get(str(content['chapter_id'])) if 'chapter_id' in content
                       else exact.match(content))
            value = bind_chapter_content(content, chapter) if chapter else {
                **deepcopy(content), 'chapter_id': content.get('chapter_id') or '', UNAVAILABLE: True}
        if value != content:
            changes.append((outline, value))
    # Do not modify chapter plans until all legacy exact comparisons finished.
    for chapter in chapters:
        value = bind_chapter_content(chapter.outline_json, chapter,
            volume_idx=volumes_by_id[str(chapter.volume_id)].volume_idx)
        if value != chapter.outline_json:
            changes.append((chapter, value))
    for row, value in changes:
        if isinstance(row, Chapter):
            row.outline_json = value
            from app.services.chapter_order import next_order_timestamp
            row.updated_at = next_order_timestamp([{'updated_at': row.updated_at}])
        else:
            row.content_json = value
    if changes:
        await db.flush()
    return len(changes)
