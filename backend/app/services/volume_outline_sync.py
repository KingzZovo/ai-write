"""Synchronize untouched chapter stubs; never replace authored chapter plans."""
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.models.project import Chapter, Volume
from app.services.outline_order import (
    REF, UNAVAILABLE, bind_volume_content, prepare_author_volume, stub_payload,
)


@dataclass
class SyncResult:
    synced: int = 0
    preserved: int = 0
    unmaterialized: int = 0


def chapter_map(items, *, allow_duplicates=False, ignore_unavailable=False):
    if items is None:
        return {}
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError('章节摘要必须是对象列表')
    if ignore_unavailable:
        items = [item for item in items if not item.get(UNAVAILABLE) and not (REF in item and not item[REF])]
    explicit = ['chapter_idx' in item for item in items]
    if any(explicit) and not all(explicit):
        raise ValueError('章节摘要混用了有章号和无章号记录，请先补齐章号')
    result = {}
    for position, item in enumerate(items, 1):
        idx = item.get('chapter_idx', position)
        if type(idx) is not int or idx < 1 or (idx in result and not allow_duplicates):
            raise ValueError('章节摘要章号无效或重复')
        result[idx] = item
    return result


async def sync_untouched_stubs(db, project_id, old, new):
    """Caller holds project then outline locks and commits all changes together."""
    result = SyncResult()
    if old.get('volume_id') and new.get('volume_id') != old['volume_id']:
        raise ValueError('不能通过编辑卷纲修改所属分卷')
    if (old.get('chapter_summaries') == new.get('chapter_summaries')
            and old.get('foreshadows') == new.get('foreshadows')
            and old.get('volume_idx') == new.get('volume_idx')):
        return result
    # Validate input shape now; after restoring system references below, old
    # unavailable entries may legitimately retain a number now used elsewhere.
    chapter_map(new.get('chapter_summaries'), allow_duplicates=True)
    volume_id, volume_idx = old.get('volume_id') or new.get('volume_id'), old.get('volume_idx') or new.get('volume_idx')
    if not volume_id and (old.get('volume_id') == '' or new.get('volume_id') == ''):
        raise ValueError('卷纲原所属分卷无法确认，请先明确关联分卷，不能按旧卷号自动替换')
    query = select(Volume).where(Volume.project_id == project_id)
    if volume_id:
        query = query.where(Volume.id == UUID(str(volume_id)))
    elif type(volume_idx) is int and volume_idx > 0:
        query = query.where(Volume.volume_idx == volume_idx)
    else:
        raise ValueError('卷纲缺少有效分卷标识，无法核对章节')
    volumes = (await db.scalars(query.with_for_update().execution_options(populate_existing=True))).all()
    if len(volumes) > 1:
        raise ValueError('卷号重复，请先整理分卷')
    if not volumes:
        if volume_id:
            raise ValueError('所属分卷不存在或不属于本作品')
        before_raw = chapter_map(old.get('chapter_summaries'), ignore_unavailable=True)
        after_raw = chapter_map(new.get('chapter_summaries'))
        result.unmaterialized = sum(before_raw.get(idx) != after_raw.get(idx) for idx in before_raw.keys() | after_raw.keys())
        return result
    volume = volumes[0]
    if old.get('volume_idx') is not None and new.get('volume_idx') not in (old['volume_idx'], volume.volume_idx):
        raise ValueError('不能通过编辑卷纲修改所属分卷')
    chapters = (await db.scalars(select(Chapter).where(Chapter.volume_id == volume.id)
        .order_by(Chapter.chapter_idx, Chapter.id).with_for_update()
        .execution_options(populate_existing=True))).all()
    indexed, by_id = {}, {}
    for chapter in chapters:
        if chapter.chapter_idx in indexed:
            raise ValueError('目标卷存在重复章号，请先整理章节')
        indexed[chapter.chapter_idx] = chapter
        by_id[str(chapter.id)] = chapter
    bound_old = bind_volume_content(old, volume, chapters)
    bound_new = prepare_author_volume(bound_old, new, volume, chapters)
    if any(not isinstance(item, dict) or 'chapter_idx' not in item
           for item in bound_new.get('chapter_summaries') or []):
        raise ValueError('修改章节摘要时请填写明确章号，不能按数组位置推测章节')
    old_items = bound_old.get('chapter_summaries') or []
    new_items = bound_new.get('chapter_summaries') or []
    def unchanged_unavailable(item):
        return item.get(UNAVAILABLE) and any(stub_payload(item) == stub_payload(previous) for previous in old_items)
    # Retained, unavailable history must not prevent editing the real chapter
    # now at that number. Newly introduced duplicate schedules still fail.
    chapter_map([item for item in new_items if not unchanged_unavailable(item)])
    result.unmaterialized = sum(bool(item.get(UNAVAILABLE)) and not unchanged_unavailable(item) for item in new_items)

    def by_reference(items):
        result = {}
        for item in chapter_map(items, ignore_unavailable=True).values():
            key = str(item[REF])
            if key in result:
                raise ValueError('章节摘要重复关联同一章节，请先核对')
            result[key] = item
        return result

    before = by_reference(bound_old.get('chapter_summaries'))
    after = by_reference(bound_new.get('chapter_summaries'))
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    # Validate everything before changing any row. Only exact old stubs prove
    # that an automatic update will not discard an author's detailed plan.
    updates = []
    for key in changed:
        chapter = by_id.get(key)
        if chapter is None:
            result.unmaterialized += 1
        elif (key in before and key in after
              and before[key].get(REF) == str(chapter.id) and after[key].get(REF) == str(chapter.id)
              and not before[key].get(UNAVAILABLE) and not after[key].get(UNAVAILABLE)
              and stub_payload(chapter.outline_json) == stub_payload(before[key])
              and not (chapter.content_text or '').strip() and chapter.status == 'draft'):
            updates.append((chapter, deepcopy(after[key])))
            result.synced += 1
        else:
            result.preserved += 1
    for chapter, value in updates:
        chapter.outline_json = value
    new.clear()
    new.update(bound_new)
    return result
