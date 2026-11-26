"""Resolve authoritative chapter sources without local/count index fallbacks."""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any
from uuid import UUID
from sqlalchemy import select, func
from app.models.project import Chapter, Volume


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class EntitySource:
    chapter_id: str
    project_id: str
    global_idx: int
    content: str
    receipt: dict
    source_hash: str


@dataclass(frozen=True)
class EntitySourceHeader:
    """Current identity and stored body digest; contains no evidence proof."""
    chapter_id: str
    project_id: str
    global_idx: int
    receipt: dict
    source_hash: str


def _receipt_from_row(row, content_hash):
    """**Source 身份** —— 只包含"正文变了才会变"的东西,与读正文/走哈希两条路共用。

    ``source_hash = digest(receipt)``,所以回执里放什么,就等于把什么当成身份。
    唯一被**有意排除**的是 ``status``(评审态 ``needs_review``↔``completed``):
    审核动作会反复改它而正文一字未动;实测把它烤进身份后,状态一翻记录就查不到,
    ``record is None`` 让 ``completed`` 章走严格路径抛
    ``character_cognition_not_ready``,同批次后续**所有**章级联 failed。

    ``title`` 与位置字段都**留在**身份里 —— 这是既有契约:改题、重排、换卷都让旧
    source 失效(test_previous_proof_cannot_authorize_a_changed_current_source[title]、
    test_changed_source_or_record_is_never_reused[chapter_order|volume_order])。
    评审态则是展示字段,一律现读 ``chapters`` 行。
    """
    return dict(chapter_id=str(row.id), project_id=str(row.project_id), volume_id=str(row.volume_id),
                   title=row.title, chapter_idx=row.chapter_idx, global_idx=row.global_idx,
                   volume_idx=row.volume_idx, content_hash=content_hash)


def _source_from_row(row):
    """One receipt format for single-source, batched and fact reads."""
    receipt = _receipt_from_row(row, hashlib.sha256(row.content_text.encode()).hexdigest())
    return EntitySource(str(row.id), str(row.project_id), row.global_idx, row.content_text, receipt, digest(receipt))


async def resolve_entity_source_headers(db, project_id, chapter_ids, *, before_global_idx=None):
    """Read stored fingerprints, falling back to bodies only for unknown hashes.

    Hash maintenance requires the a1001949 trigger. A header alone does not
    establish nonblank prose or validate a quote; consumers need a full-body
    proof for the exact receipt before reusing it. Invalid chronology and
    completed-but-empty sources still fail, just as in the full resolver.
    """
    if before_global_idx is not None and (type(before_global_idx) is not int or before_global_idx <= 0):
        return {}
    project_id = UUID(str(project_id))
    ids = sorted({UUID(str(cid)) for cid in chapter_ids}, key=str)
    sources = {}
    for start in range(0, len(ids), 200):
        query = select(Chapter.id, Chapter.title, Chapter.content_hash, Chapter.status,
            Chapter.chapter_idx, Chapter.global_idx, Chapter.volume_id, Volume.project_id, Volume.volume_idx
        ).join(Volume, Chapter.volume_id == Volume.id).where(
            Volume.project_id == project_id, Chapter.id.in_(ids[start:start + 200])
        ).order_by(Chapter.id)
        if before_global_idx is not None:
            query = query.where(Chapter.status == 'completed', Chapter.global_idx > 0, Chapter.global_idx < before_global_idx)
        rows = (await db.execute(query.execution_options(autoflush=False))).all()
        indices = {row.global_idx for row in rows if row.global_idx and row.global_idx > 0}
        counts = dict((await db.execute(select(Chapter.global_idx, func.count(Chapter.id))
            .join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == project_id,
                Chapter.global_idx.in_(indices)).group_by(Chapter.global_idx)
            .execution_options(autoflush=False))).all()) if indices else {}
        unknown = []
        for row in rows:
            if row.status != 'completed' or counts.get(row.global_idx) != 1:
                continue
            if not isinstance(row.content_hash, str) or re.fullmatch('[0-9a-f]{64}', row.content_hash) is None:
                unknown.append(row.id)
                continue
            receipt = _receipt_from_row(row, row.content_hash)
            sources[str(row.id)] = EntitySourceHeader(str(row.id), str(row.project_id),
                row.global_idx, receipt, digest(receipt))
        if unknown:
            sources.update(await resolve_entity_sources(db, project_id, unknown))
    return sources


async def resolve_entity_sources(db, project_id, chapter_ids, *, lock=False,
                                 allow_review_drafts: bool = False):
    """Read bounded body batches, validating order against the entire project.

    ``allow_review_drafts``(2026-11-17):认知台账现在也纳入"已落库正文但状态被降级"
    的章,这些章的证据校验需要读它们的正文来核实引文;默认 False 保持旧调用方语义。
    """
    project_id = UUID(str(project_id))
    ids = sorted({UUID(str(cid)) for cid in chapter_ids}, key=str)
    sources = {}
    for start in range(0, len(ids), 200):
        stmt = select(Chapter.id, Chapter.title, Chapter.content_text, Chapter.status,
            Chapter.chapter_idx, Chapter.global_idx, Chapter.volume_id, Volume.project_id, Volume.volume_idx
        ).join(Volume, Chapter.volume_id == Volume.id).where(
            Volume.project_id == project_id, Chapter.id.in_(ids[start:start + 200])
        ).order_by(Chapter.id)
        if lock:
            stmt = stmt.with_for_update()
        rows = (await db.execute(stmt.execution_options(autoflush=False))).all()
        indices = {row.global_idx for row in rows if row.global_idx and row.global_idx > 0}
        counts = dict((await db.execute(select(Chapter.global_idx, func.count(Chapter.id))
            .join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == project_id,
                Chapter.global_idx.in_(indices)).group_by(Chapter.global_idx)
            .execution_options(autoflush=False))).all()) if indices else {}
        allowed = {'completed', 'draft', 'needs_review'} if allow_review_drafts else {'completed'}
        for row in rows:
            if (row.status in allowed and counts.get(row.global_idx) == 1
                    and row.content_text and row.content_text.strip()):
                sources[str(row.id)] = _source_from_row(row)
    return sources


async def resolve_entity_source(db, project_id, chapter_idx, chapter_id=None, *, lock=False, allow_review_drafts=False):
    project_id = UUID(str(project_id))
    stmt = select(Chapter.id, Chapter.title, Chapter.content_text, Chapter.status,
                  Chapter.chapter_idx, Chapter.global_idx, Chapter.volume_id,
                  Volume.project_id, Volume.volume_idx).join(Volume, Chapter.volume_id == Volume.id).where(
                      Volume.project_id == project_id)
    if chapter_id:
        stmt = stmt.where(Chapter.id == UUID(str(chapter_id)))
    else:
        stmt = stmt.where(Chapter.chapter_idx == chapter_idx)
    if lock:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt.limit(2).execution_options(autoflush=False))).all()
    if len(rows) != 1:
        return None
    row = rows[0]
    allowed_statuses = {'completed', 'draft', 'needs_review'} if allow_review_drafts else {'completed'}
    if row.status not in allowed_statuses or not row.content_text or not row.content_text.strip() or not row.global_idx or row.global_idx <= 0:
        return None
    duplicates = (await db.execute(select(Chapter.id).join(Volume, Chapter.volume_id == Volume.id).where(
        Volume.project_id == project_id, Chapter.global_idx == row.global_idx,
    ).limit(2).execution_options(autoflush=False))).scalars().all()
    if len(duplicates) != 1:
        return None
    return _source_from_row(row)


async def filter_current_entity_rows(db, project_id, rows, *, before_global_idx):
    """Validate bounded batches of source-tagged facts against committed chapters.

    Each dict needs source_chapter_id, source_hash and global_idx. Excerpts,
    when supplied, must additionally be verbatim in the verified full body.
    Unknown provenance is excluded, never guessed from chapter numbering.
    Successful full-body/quote proofs can be reused only with the current
    database-maintained body fingerprint and exact source receipt.
    """
    if type(before_global_idx) is not int or before_global_idx <= 0 or not rows:
        return []
    from app.services.entity_source_validation import filter_source_rows
    return await filter_source_rows(db, project_id, rows, before_global_idx=before_global_idx)
