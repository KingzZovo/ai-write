"""Source-bound volume and book memory. Unknown/edited sources are never history.

Queries return chapter summaries and fingerprints, never whole-book prose.
The manifests bind membership, order, titles, chapter source and aggregate output.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, Project, Volume, VolumeSummary
from app.services.summary_provenance import current_summary_sql


def receipt_hash(manifest: Any, text: str) -> str:
    return hashlib.sha256(json.dumps([manifest, text], ensure_ascii=False,
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


async def volume_sources(db: AsyncSession, volume_ids: list) -> dict[str, dict]:
    """Batch validate every member, including unfinished or unsummarized chapters."""
    if not volume_ids:
        return {}
    rows = (await db.execute(select(
        Volume.id.label('volume_id'), Volume.project_id, Volume.volume_idx,
        Volume.title.label('volume_title'), Chapter.id.label('chapter_id'),
        Chapter.chapter_idx, Chapter.global_idx, Chapter.title,
        Chapter.status, Chapter.summary_source_hash,
        current_summary_sql().label('summary'),
    ).outerjoin(Chapter, Chapter.volume_id == Volume.id).where(
        Volume.id.in_(volume_ids),
    ).order_by(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
      .execution_options(autoflush=False))).all()
    sources: dict[str, dict] = {}
    for row in rows:
        key = str(row.volume_id)
        source = sources.setdefault(key, {
            'manifest': {'volume_id': key, 'project_id': str(row.project_id),
                         'volume_idx': row.volume_idx, 'title': row.volume_title,
                         'chapters': []}, 'summaries': [], 'valid': True,
        })
        if row.chapter_id is None or row.status != 'completed' or not row.summary:
            source['valid'] = False
            continue
        source['manifest']['chapters'].append({
            'id': str(row.chapter_id), 'chapter_idx': row.chapter_idx,
            'global_idx': row.global_idx, 'title': row.title,
            'source_hash': row.summary_source_hash,
        })
        source['summaries'].append(row.summary)
    for source in sources.values():
        chapters = source['manifest']['chapters']
        local = [ch['chapter_idx'] for ch in chapters]
        global_order = [ch['global_idx'] for ch in chapters]
        if (len(set(local)) != len(local) or any(not isinstance(i, int) or i <= 0 for i in global_order)
                or global_order != sorted(set(global_order))):
            source['valid'] = False
    return {key: value for key, value in sources.items() if value['valid']}


async def current_volume_summaries(
    db: AsyncSession, project_id: Any, *, volume_ids: list | None = None,
    before_volume_idx: int | None = None, before_global_idx: int | None = None,
) -> list[dict]:
    query = select(
        VolumeSummary.id, VolumeSummary.volume_id, VolumeSummary.summary_text,
        VolumeSummary.source_manifest, VolumeSummary.source_hash,
    ).join(Volume, VolumeSummary.volume_id == Volume.id).where(Volume.project_id == project_id)
    if volume_ids is not None:
        query = query.where(Volume.id.in_(volume_ids))
    if before_volume_idx is not None:
        query = query.where(Volume.volume_idx < before_volume_idx)
    rows = (await db.execute(query.order_by(Volume.volume_idx, Volume.id,
        VolumeSummary.created_at.desc(), VolumeSummary.id.desc())
        .execution_options(autoflush=False))).all()
    sources = await volume_sources(db, list({row.volume_id for row in rows}))
    seen: set[str] = set()
    valid = []
    for row in rows:
        key = str(row.volume_id)
        source = sources.get(key)
        if key in seen or source is None:
            continue
        manifest = source['manifest']
        if (row.source_manifest != manifest or not row.summary_text
                or row.source_hash != receipt_hash(manifest, row.summary_text)):
            continue
        if before_global_idx is not None and any(
            not isinstance(ch['global_idx'], int) or ch['global_idx'] <= 0
            or ch['global_idx'] >= before_global_idx for ch in manifest['chapters']
        ):
            continue
        seen.add(key)
        valid.append({'id': str(row.id), 'volume_id': key, 'text': row.summary_text,
                      'hash': row.source_hash, 'manifest': manifest})
    return valid


async def volume_prefix(db: AsyncSession, project_id: Any) -> list[dict]:
    """A book synopsis must not silently skip a missing/unfinished middle volume."""
    volumes = (await db.execute(select(Volume.id, Volume.volume_idx).where(
        Volume.project_id == project_id,
    ).order_by(Volume.volume_idx, Volume.id).execution_options(autoflush=False))).all()
    current = {row['volume_id']: row for row in await current_volume_summaries(db, project_id)}
    prefix = []
    orders = [row.volume_idx for row in volumes]
    if len(orders) != len(set(orders)):
        return []  # Ambiguous chronology cannot be presented as a book history.
    for volume in volumes:
        row = current.get(str(volume.id))
        if row is None:
            break
        if prefix and (prefix[-1]['manifest']['chapters'][-1]['global_idx']
                       >= row['manifest']['chapters'][0]['global_idx']):
            break
        prefix.append(row)
    return prefix


def book_receipts(volumes: list[dict]) -> list[dict]:
    return [{'volume_id': row['volume_id'], 'hash': row['hash']} for row in volumes]


async def current_book_synopsis(db: AsyncSession, project_id: Any, *,
                                before_volume_idx: int, before_global_idx: int) -> str:
    settings = await db.scalar(select(Project.settings_json).where(Project.id == project_id)
                               .execution_options(autoflush=False))
    stored = (settings or {}).get('book_synopsis')
    if not isinstance(stored, dict):
        return ''
    text, receipts = stored.get('text'), stored.get('sources')
    if (not isinstance(text, str) or not text.strip() or not isinstance(receipts, list)
            or not receipts or stored.get('source_hash') != receipt_hash(receipts, text)):
        return ''
    prefix = await volume_prefix(db, project_id)
    used = prefix[:len(receipts)]
    if book_receipts(used) != receipts:
        return ''
    for row in used:
        manifest = row['manifest']
        if manifest['volume_idx'] >= before_volume_idx:
            return ''
        if any(not isinstance(ch['global_idx'], int) or ch['global_idx'] <= 0
               or ch['global_idx'] >= before_global_idx for ch in manifest['chapters']):
            return ''
    return text


async def generate_volume_memory(db: AsyncSession, volume_id: Any, *, persist: bool = False) -> tuple[bool, str]:
    """Generate with a source receipt; refresh stale rows without losing author edits."""
    from sqlalchemy import text as sql_text, update
    from app.services.chapter_summarizer import _clean_summary_output
    from app.services.prompt_registry import run_text_prompt

    if db.new or db.dirty or db.deleted:
        return False, ''
    source = (await volume_sources(db, [volume_id])).get(str(volume_id))
    if source is None:
        return False, ''
    manifest = source['manifest']
    if persist:
        current = await current_volume_summaries(db, manifest['project_id'], volume_ids=[volume_id])
        if current:
            return False, current[0]['text']
    # No read transaction is held while the model runs.
    await db.rollback()
    prompt = ('请根据以下完整章节摘要生成本卷事实梗概，保留主线推进、人物状态、'
              '未解决事件与卷末结果。不要把计划、猜测写成已发生事实。直接返回完整梗概。\n\n')
    prompt += '\n\n'.join(f"[CH-{ch['chapter_idx']}]《{ch['title'] or ''}》：{summary}"
                           for ch, summary in zip(manifest['chapters'], source['summaries']))
    result = await run_text_prompt(task_type='summary', user_content=prompt, db=db,
                                   project_id=manifest['project_id'], max_tokens=2048)
    summary = _clean_summary_output(result.text or '')
    if not summary or db.new or db.dirty or db.deleted:
        return False, ''
    if persist:
        # Serialize only the brief commit phase, not model IO. No duplicate rows
        # from simultaneous backfills; no uniqueness migration deleting legacy data.
        await db.execute(sql_text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                         {'key': f'volume-memory:{volume_id}'})
    if (await volume_sources(db, [volume_id])).get(str(volume_id)) != source:
        await db.rollback()
        return False, ''
    if not persist:
        return False, summary
    current = await current_volume_summaries(db, manifest['project_id'], volume_ids=[volume_id])
    if current:
        await db.rollback()
        return False, current[0]['text']
    existing = await db.scalar(select(VolumeSummary.id).where(VolumeSummary.volume_id == volume_id)
        .order_by(VolumeSummary.created_at.desc(), VolumeSummary.id.desc()).limit(1)
        .execution_options(autoflush=False))
    values = dict(summary_text=summary, source_manifest=manifest, source_hash=receipt_hash(manifest, summary))
    if existing is None:
        db.add(VolumeSummary(volume_id=volume_id, **values))
    else:
        await db.execute(update(VolumeSummary).where(VolumeSummary.id == existing).values(**values)
                         .execution_options(synchronize_session=False, autoflush=False))
    await db.commit()
    return True, summary


async def generate_book_memory(db: AsyncSession, project_id: Any) -> str:
    """Persist an exact contiguous source prefix; never overwrite concurrent settings."""
    from sqlalchemy import cast, JSON, literal, update
    from sqlalchemy.dialects.postgresql import JSONB
    from app.services.chapter_summarizer import _clean_summary_output
    from app.services.prompt_registry import run_text_prompt

    if db.new or db.dirty or db.deleted:
        return ''
    settings = await db.scalar(select(Project.settings_json).where(Project.id == project_id)
                               .execution_options(autoflush=False))
    prefix = await volume_prefix(db, project_id)
    if not prefix:
        return ''
    sources = book_receipts(prefix)
    existing = (settings or {}).get('book_synopsis')
    if (isinstance(existing, dict) and isinstance(existing.get('text'), str)
            and existing['text'].strip() and existing.get('sources') == sources
            and existing.get('source_hash') == receipt_hash(sources, existing['text'])):
        return existing['text']
    await db.rollback()
    prompt = ('请根据以下全部卷摘要生成完整的全书至此事实梗概，尽量精炼，保留主线、'
              '人物关系与未解决事件。只概括已发生内容，不补写结局。直接返回完整梗概。\n\n')
    prompt += '\n\n'.join(f"[VOL-{row['manifest']['volume_idx']}]《{row['manifest']['title']}》：{row['text']}" for row in prefix)
    result = await run_text_prompt(task_type='summary', user_content=prompt, db=db,
                                   project_id=project_id, max_tokens=2048)
    synopsis = _clean_summary_output(result.text or '')
    if (not synopsis or db.new or db.dirty or db.deleted
            or book_receipts(await volume_prefix(db, project_id)) != sources):
        return ''
    from datetime import datetime, timezone
    new_settings = dict(settings or {})
    new_settings['book_synopsis'] = {
        'text': synopsis, 'sources': sources, 'source_hash': receipt_hash(sources, synopsis),
        'source_volumes': len(sources), 'updated_at': datetime.now(timezone.utc).isoformat(),
    }
    saved = await db.scalar(update(Project).where(
        Project.id == project_id,
        cast(Project.settings_json, JSONB).is_not_distinct_from(cast(literal(settings, type_=JSON), JSONB)),
    ).values(settings_json=new_settings).returning(Project.id)
      .execution_options(synchronize_session=False, autoflush=False))
    if saved is None:
        return ''
    await db.commit()
    return synopsis
