"""Source-bound character cognition deltas, reconstructed before each chapter.

Legacy cumulative rows remain available only through load_legacy_ledger for
administrative compatibility; generation must use load_ledger with a boundary.
Verbatim evidence locates a claim but does not prove its semantic correctness.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import and_, select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

READER = "__reader__"
COGNITION_KINDS = ('learns', 'still_unknown', 'believes', 'suspects')

EXTRACTION_PROMPT = """从完整章节正文抽取影响后续剧情的认知变化，只输出 JSON 数组。
每项是 {"character":"人物名或__reader__","learns":"本章明确得知的信息","evidence":"连续逐字原文"}
或 {"character":"人物名","still_unknown":"正文明确表明其仍不知道的信息","evidence":"连续逐字原文"}
或 {"character":"人物名","believes":"人物当时确信或误信的内容，保留主观限定","evidence":"连续逐字原文"}
或 {"character":"人物名","suspects":"人物当时怀疑的内容，保留不确定性","evidence":"连续逐字原文"}。
每项只用一种认知字段。learns 仅用于正文明确建立的获知；确信、误信用 believes，怀疑用 suspects。
不能把人物的误信判为作者写错，也不能替正文揭穿悬念；旁人说话不等于角色相信。
例如“他以为信已寄出”应记 believes，不是 learns；“她怀疑信被拆过”应记 suspects。
__reader__ 只允许 learns，记录正文明确提供给读者的信息，不推测真实读者的判断。
读者已知不等于角色已知；没有描写人物获知，不足以证明其不知道。
只提取本章实际建立的变化，不引用大纲或累计账本补事实。不限制条目数量，无变化输出 []。
每项必须有能支持该人物知情状态的原文依据。evidence 必须从正文里**直接复制连续的一段**:不改写、不概括、不加省略号、不把不相邻的两处拼在一起;复制时连标点一起抄。片段尽量短(10–40 字),只要能定位这条认知即可。写不出能逐字找到的依据,就不要提这一条。"""

# Per-character cap on each of knows / does_not_know. Without a bound the
# lists grow by up to 10 entries per chapter forever; with serialization
# budgeted at ~1200 chars an unbounded ledger only wastes DB rows and skews
# the entry-count importance ordering. Oldest facts (list head) are evicted
# first — the tail holds the most recent chapter's additions.
MAX_FACTS_PER_CHARACTER = 30


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def _copy_ledger(ledger: dict) -> dict:
    out: dict[str, dict[str, list[str]]] = {}
    for name, entry in (ledger or {}).items():
        entry = entry or {}
        out[str(name)] = {
            "knows": [str(x) for x in (entry.get("knows") or [])],
            "does_not_know": [str(x) for x in (entry.get("does_not_know") or [])],
        }
    return out


def apply_changes(ledger: dict, changes: list[dict]) -> dict:
    """Merge extracted cognition changes into a ledger; returns a new dict.

    - ``learns``: 加入 knows 并从 does_not_know 移除（去重）。
    - ``still_unknown``: 加入 does_not_know；若该信息已在 knows 中则忽略，
      绝不把已知信息降级回未知。读者行只用 knows，still_unknown 被忽略。

    Malformed entries are skipped silently — extraction output is LLM JSON
    and must never be able to crash the post-save pipeline.
    """
    out = _copy_ledger(ledger)
    for change in changes or []:
        if not isinstance(change, dict):
            continue
        name = str(change.get("character") or "").strip()
        if not name:
            continue
        learns = str(change.get("learns") or "").strip()
        still_unknown = str(change.get("still_unknown") or "").strip()
        if not learns and not still_unknown:
            continue
        entry = out.setdefault(name, {"knows": [], "does_not_know": []})
        if learns:
            if learns not in entry["knows"]:
                entry["knows"].append(learns)
            entry["does_not_know"] = [x for x in entry["does_not_know"] if x != learns]
        if still_unknown and name != READER:
            if still_unknown not in entry["knows"] and still_unknown not in entry["does_not_know"]:
                entry["does_not_know"].append(still_unknown)
    # Capacity bound: evict the oldest facts (list head) and keep the most
    # recent MAX_FACTS_PER_CHARACTER (list tail, where new entries land).
    for entry in out.values():
        if len(entry["knows"]) > MAX_FACTS_PER_CHARACTER:
            entry["knows"] = entry["knows"][-MAX_FACTS_PER_CHARACTER:]
        if len(entry["does_not_know"]) > MAX_FACTS_PER_CHARACTER:
            entry["does_not_know"] = entry["does_not_know"][-MAX_FACTS_PER_CHARACTER:]
    return out


def serialize_for_prompt(ledger: dict, max_chars: int = 1200, *, priority_names=(), context_text='') -> str:
    """Render source history with quotes; keep cumulative compatibility below.

    Reader line 『读者已知：…』 comes FIRST — the reader-knows/character-
    doesn't-know gap is the suspense backbone and must never be the first
    thing truncated away. Remaining per-character lines 『X知道：a；b』 /
    『X不知道：c』 follow, characters with more ledger entries first.

    Truncation never ends mid-sentence: when a line does not fit the
    remaining budget, its oldest facts (list head) are dropped item by item
    until the newest ones fit; if not even the newest single fact fits, the
    whole line is skipped and later (shorter) lines still get a chance.
    Empty ledger returns "".
    """
    if any(isinstance(entry, dict) and 'history' in entry for entry in (ledger or {}).values()):
        from app.services.cognition_context import render_history
        return render_history(ledger, max_chars, priority_names=priority_names, context_text=context_text)
    ledger = _copy_ledger(ledger)
    reader_entry = ledger.pop(READER, None)

    def _entry_weight(item: tuple[str, dict]) -> int:
        _, e = item
        return len(e["knows"]) + len(e["does_not_know"])

    # (prefix, facts) candidates in priority order: reader first, then
    # characters by entry count descending.
    candidates: list[tuple[str, list[str]]] = []
    if reader_entry and reader_entry["knows"]:
        candidates.append(("读者已知：", reader_entry["knows"]))
    for name, entry in sorted(ledger.items(), key=_entry_weight, reverse=True):
        if entry["knows"]:
            candidates.append((f"{name}知道：", entry["knows"]))
        if entry["does_not_know"]:
            candidates.append((f"{name}不知道：", entry["does_not_know"]))

    kept: list[str] = []
    used = 0
    for prefix, facts in candidates:
        sep_cost = 1 if kept else 0  # newline separator
        budget = max_chars - used - sep_cost - len(prefix)
        # Drop oldest facts (head) until the newest ones fit the budget.
        while facts and len("；".join(facts)) > budget:
            facts = facts[1:]
        if not facts:
            continue  # line unfittable; later, shorter lines may still fit
        line = prefix + "；".join(facts)
        kept.append(line)
        used += sep_cost + len(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------


async def load_legacy_ledger(db: AsyncSession, project_id: Any) -> dict:
    """Load the full cognition ledger for a project. Empty dict if none."""
    from app.models.project import CharacterCognition

    result = await db.execute(
        select(CharacterCognition).where(
            CharacterCognition.project_id == str(project_id)
        )
    )
    ledger: dict[str, dict[str, list[str]]] = {}
    for row in result.scalars().all():
        ledger[row.character_name] = {
            "knows": list(row.knows or []),
            "does_not_know": list(row.does_not_know or []),
        }
    return ledger


async def save_ledger(db: AsyncSession, project_id: Any, ledger: dict) -> None:
    """Upsert the ledger: one row per character, updated in place."""
    from app.models.project import CharacterCognition

    result = await db.execute(
        select(CharacterCognition).where(
            CharacterCognition.project_id == str(project_id)
        )
    )
    existing = {row.character_name: row for row in result.scalars().all()}
    for name, entry in _copy_ledger(ledger).items():
        row = existing.get(name)
        if row is not None:
            row.knows = entry["knows"]
            row.does_not_know = entry["does_not_know"]
        else:
            db.add(
                CharacterCognition(
                    id=uuid.uuid4(),
                    project_id=str(project_id),
                    character_name=name,
                    knows=entry["knows"],
                    does_not_know=entry["does_not_know"],
                )
            )
    await db.commit()


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------


def _parse_changes(raw: str, source: str) -> list[dict]:
    """Reject malformed/truncated results, never silently turn failure into []."""
    from app.services.chapter_entity_extraction import EntityExtractionError
    import re
    text = (raw or '').strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```\s*', text, re.DOTALL)
        if not match:
            raise EntityExtractionError('认知 JSON 围栏不完整')
        text = match.group(1)
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise EntityExtractionError('认知字段重复')
            out[key] = value
        return out
    try:
        changes = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, TypeError) as exc:
        raise EntityExtractionError('认知提取不是有效 JSON 数组') from exc
    return validate_changes(changes, source)


def _validate_change_shape(changes):
    from app.services.chapter_entity_extraction import EntityExtractionError
    if not isinstance(changes, list):
        raise EntityExtractionError('认知变化必须是数组')
    for item in changes:
        if not isinstance(item, dict) or set(item) not in (
            {'character', kind, 'evidence'} for kind in COGNITION_KINDS):
            raise EntityExtractionError('认知字段缺失或未知')
        if any(not isinstance(v, str) or not v.strip() or '\x00' in v for v in item.values()):
            raise EntityExtractionError('认知字段必须为非空文本')
        if item['character'] == READER and 'learns' not in item:
            raise EntityExtractionError('读者行只记录正文明确提供的信息')
    return changes


def validate_changes(changes, source):
    from app.services.chapter_entity_extraction import EntityExtractionError, evidence_locatable
    _validate_change_shape(changes)
    offenders = [item['evidence'] for item in changes
                 if not evidence_locatable(item['evidence'], source)]
    if offenders:
        # 带上具体是哪几条,调用方才能给出"从正文里复制"的定向修复提示,
        # 而不是笼统地要求重来一遍(实测重来一遍仍然会改写)。
        error = EntityExtractionError('认知依据无法在正文逐字定位')
        error.offending_evidence = offenders[:5]
        raise error
    return changes


async def _history_boundary(db, project_id, before_global_idx, before_chapter_id):
    """Resolve an explicit boundary without inventing missing chapter order."""
    from sqlalchemy import func
    from app.models.project import Chapter, Volume
    from app.services.context_errors import RequiredContextUnavailable
    if before_chapter_id is not None:
        expected_idx = before_global_idx
        before_global_idx = await db.scalar(select(Chapter.global_idx).join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == uuid.UUID(str(project_id)), Chapter.id == uuid.UUID(str(before_chapter_id)))
            .execution_options(autoflush=False))
        if expected_idx is not None and (type(expected_idx) is not int or before_global_idx != expected_idx):
            raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
        count = await db.scalar(select(func.count(Chapter.id)).join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == uuid.UUID(str(project_id)), Chapter.global_idx == before_global_idx)
            .execution_options(autoflush=False))
        if count != 1:
            raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
    if type(before_global_idx) is not int or before_global_idx <= 0:
        if before_chapter_id is not None or before_global_idx is not None:
            raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
        return None
    return before_global_idx


def _has_saved_body():
    """"这一章已经有落库正文"的廉价信号:**content_hash 非空**(不读正文列)。

    a1001949 的触发器在每次写入正文时维护 ``content_hash``;用它判断比 detoast
    ``content_text`` 便宜几个数量级,也避免 5M 字符规模下的整书正文读取
    (既有规模用例会断言 SQL 里不出现 ``chapters.content_text``)。
    没有哈希的历史行按"无正文"处理 —— 它们本来也不会被下面的事件路径读到。
    """
    from app.models.project import Chapter

    return and_(Chapter.content_hash.isnot(None), Chapter.word_count > 0)


def _has_prose(column):
    """Portable "body is more than whitespace" predicate (PG + SQLite agree)."""
    text = func.coalesce(column, '')
    for blank in (' ', '\t', '\r', '\n'):
        text = func.replace(text, blank, '')
    return func.length(text) > 0


def _ledger_position_query(project_id, boundary):
    """Every chapter with saved prose before the boundary, whatever its status.

    Status trails prose: generation commits the body first and only then
    downgrades the chapter (api/generate.py -> ``draft``,
    tasks/generation_tasks.py -> ``needs_review``). Filtering on ``completed``
    alone therefore drops facts the story already established from every later
    chapter's ledger, silently. Empty bodies stay out (there is nothing to
    rebuild from), and completed chapters keep the old predicate verbatim — the
    prose check is only evaluated for the other statuses — so a book without
    review drafts produces the exact same rows.
    """
    from app.models.project import Chapter, Volume
    return (select(Chapter.id, Chapter.global_idx, Chapter.status)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == uuid.UUID(str(project_id)),
               or_(Chapter.global_idx.is_(None), Chapter.global_idx < boundary),
               or_(Chapter.status == 'completed', _has_saved_body()))
        .order_by(Chapter.global_idx, Chapter.id).execution_options(autoflush=False))


async def _ledger_positions(db, project_id, boundary):
    """Positions, statuses, and the chapters this ledger has to skip visibly."""
    from app.models.project import Chapter, Volume
    from app.services.context_errors import RequiredContextUnavailable
    rows = (await db.execute(_ledger_position_query(project_id, boundary))).all()
    positions, statuses, skipped = {}, {}, []
    for cid, idx, status in rows:
        key = str(cid)
        statuses[key] = status
        if status == 'completed' or (type(idx) is int and idx > 0):
            positions[key] = idx
        else:
            skipped.append((key, idx, 'boundary_unavailable'))
    # Unknown completed positions cannot safely be classified as later prose.
    # Committed chapters keep exact strictness: no review draft softens this.
    if any(type(idx) is not int or idx <= 0
           for cid, idx in positions.items() if statuses[cid] == 'completed'):
        raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
    drafts = [cid for cid in positions if statuses[cid] != 'completed']
    if drafts:
        # One chapter number, one position: an ambiguous review draft must not
        # make the witness itself ambiguous. Duplicate completed positions still
        # fail on the unchanged strict source path below.
        counts = dict((await db.execute(select(Chapter.global_idx, func.count(Chapter.id))
            .join(Volume, Chapter.volume_id == Volume.id).where(
                Volume.project_id == uuid.UUID(str(project_id)),
                Chapter.global_idx.in_(list({positions[cid] for cid in drafts})))
            .group_by(Chapter.global_idx).execution_options(autoflush=False))).all())
        for cid in drafts:
            if counts.get(positions[cid]) != 1:
                index = positions.pop(cid)
                skipped.append((cid, index, 'boundary_unavailable'))
    return positions, statuses, skipped


def _ledger_degraded(chapter_id, global_idx, status, reason):
    """Skipping one chapter's facts is allowed; doing it silently is not."""
    from app.observability.metrics import COGNITION_LEDGER_DEGRADED_TOTAL
    COGNITION_LEDGER_DEGRADED_TOTAL.labels(reason=reason).inc()
    logger.warning('cognition_ledger_degraded chapter=%s global_idx=%s status=%s reason=%s;'
                   '该章已有正文却无法得到可用认知记录,本台账按"无认知变化"计入,建议补跑提取',
                   chapter_id, global_idx, status, reason)


async def _review_draft_sources(db, project_id, chapter_ids):
    """非 completed 章的来源头:**用库里的 content_hash,不读正文**。

    2026-11-17:最初这版为了纳入"已写正文但被降级"的章而读了正文,结果 1000 章的书
    每次取认知台账都要把全书正文 detoast 一遍 —— 直接破坏了"热读 0 正文"的性质
    (既有 5M 字符规模用例因此失败)。改为与 completed 章同一条无正文路径:
    用 a1001949 触发器维护的 ``content_hash`` 造回执;没有可用哈希的章按
    ``missing_content_hash`` 降级跳过(可见、可计数),不为了它去读正文。
    """
    import re as _re

    from app.models.project import Chapter, Volume
    from app.services.entity_sources import _receipt_from_row, digest
    from app.services.entity_sources import EntitySourceHeader
    if not chapter_ids:
        return {}
    rows = (await db.execute(select(Chapter.id, Chapter.title, Chapter.content_hash, Chapter.status,
        Chapter.chapter_idx, Chapter.global_idx, Chapter.volume_id, Volume.project_id, Volume.volume_idx)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == uuid.UUID(str(project_id)),
               Chapter.id.in_([uuid.UUID(str(cid)) for cid in chapter_ids]))
        .order_by(Chapter.id).execution_options(autoflush=False))).all()
    sources = {}
    for row in rows:
        if row.status == 'completed' or not row.global_idx or row.global_idx <= 0:
            continue
        if not isinstance(row.content_hash, str) or _re.fullmatch('[0-9a-f]{64}', row.content_hash) is None:
            _ledger_degraded(str(row.id), row.global_idx, row.status, 'missing_content_hash')
            continue
        receipt = _receipt_from_row(row, row.content_hash)
        sources[str(row.id)] = EntitySourceHeader(str(row.id), str(row.project_id),
            row.global_idx, receipt, digest(receipt))
    return sources


async def _ledger_headers(db, project_id, batch, statuses):
    """Resolve sources: committed chapters strictly and batched, drafts apart."""
    from app.services.context_errors import RequiredContextUnavailable
    from app.services.entity_sources import resolve_entity_source_headers
    strict = [cid for cid in batch if statuses.get(cid) == 'completed']
    drafts = [cid for cid in batch if statuses.get(cid) != 'completed']
    sources = await resolve_entity_source_headers(db, project_id, strict) if strict else {}
    if len(sources) != len(strict):
        raise RequiredContextUnavailable('character_cognition_source_unavailable')
    if drafts:
        sources.update(await _review_draft_sources(db, project_id, drafts))
    return sources


async def _ledger_payloads(db, project_id, batch, sources, records, revisions, positions, statuses):
    """Whole-batch validation first; relax only when the batch holds a draft."""
    from app.services.cognition_validation import validate_evidence_batch
    from app.services.context_errors import RequiredContextUnavailable
    try:
        return await validate_evidence_batch(db, project_id, sources, records, revisions)
    except RequiredContextUnavailable:
        if not any(statuses.get(cid) != 'completed' for cid in batch):
            raise
    # An unfinished draft extraction must not erase its neighbours' facts:
    # retry per chapter, committed chapters strictly, drafts visibly skipped.
    payloads = {}
    for cid in batch:
        if cid not in sources:
            _ledger_degraded(cid, positions.get(cid), statuses.get(cid), 'source_unavailable')
            continue
        row = records.get(cid)
        try:
            payloads.update(await validate_evidence_batch(db, project_id, {cid: sources[cid]},
                {cid: row} if row is not None else {}, revisions))
        except RequiredContextUnavailable:
            if statuses.get(cid) == 'completed':
                raise
            _ledger_degraded(cid, positions.get(cid), statuses.get(cid),
                'invalid_evidence' if row is not None else 'missing_records')
    return payloads


async def load_ledger(db, project_id, *, before_global_idx=None, before_chapter_id=None):
    """Check all current sources; reuse only exact, full-body evidence proofs."""
    from app.services.cognition_reader import load_source_records, load_revisions, record_version
    from app.services.cognition_validation import read_payload, CognitionLedger
    from app.services.entity_sources import digest
    from app.services.context_errors import RequiredContextUnavailable
    boundary = await _history_boundary(db, project_id, before_global_idx, before_chapter_id)
    if boundary is None:
        return {}
    # Enumerate source chapters by saved prose, not by pipeline status: a
    # chapter downgraded after its body landed still happened in the story.
    positions, statuses, skipped = await _ledger_positions(db, project_id, boundary)
    for cid, index, reason in skipped:
        _ledger_degraded(cid, index, statuses.get(cid), reason)
    ids = list(positions)
    ledger = CognitionLedger()
    source_hashes = {}
    decision_versions = {}
    proof_keys = {}
    for start in range(0, len(ids), 200):
        batch = ids[start:start + 200]
        sources = await _ledger_headers(db, project_id, batch, statuses)
        if any(source.global_idx != positions[source.chapter_id] for source in sources.values()):
            raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
        source_hashes.update({cid: source.source_hash for cid, source in sources.items()})
        records = await load_source_records(db, project_id, sources)
        revisions = await load_revisions(db, project_id, records)
        payloads = await _ledger_payloads(db, project_id, batch, sources, records, revisions,
                                         positions, statuses)
        for source in sorted(sources.values(), key=lambda s: s.global_idx):
            if source.chapter_id not in payloads:
                continue
            record = records[source.chapter_id]
            payload = payloads[source.chapter_id]
            decision_versions[source.chapter_id] = payload.version
            proof_keys[source.chapter_id] = payload.proof_key
            for change in payload.changes:
                kind = next(kind for kind in COGNITION_KINDS if kind in change)
                ledger.setdefault(change['character'], {'history': []})['history'].append({
                    'chapter_idx': source.global_idx, 'kind': kind, 'fact': change[kind],
                    'source_chapter_id': source.chapter_id, 'source_hash': source.source_hash,
                    'evidence': change['evidence'], 'source_mode': record.source_mode,
                })
    # Recheck every source and actual payload, not just the selected excerpt.
    # Stored body hashes avoid the second whole-book prose read; unknown
    # legacy hashes fall back to full sources. Timestamps alone are not proof.
    for start in range(0, len(ids), 200):
        batch = ids[start:start + 200]
        # A skipped review draft contributed nothing, so it is not rechecked;
        # everything that did contribute is rechecked exactly as before.
        used = [cid for cid in batch if cid in decision_versions]
        current = await _ledger_headers(db, project_id, used, statuses)
        if {cid: source.source_hash for cid, source in current.items()} != {cid: source_hashes[cid] for cid in used}:
            raise RequiredContextUnavailable('character_cognition_source_unavailable')
        current_records = await load_source_records(db, project_id, current)
        current_revisions = await load_revisions(db, project_id, current_records)
        for cid, source in current.items():
            row = current_records.get(cid)
            if record_version(row, source) != decision_versions[cid]:
                raise RequiredContextUnavailable('character_cognition_decision_changed')
            payload = read_payload(row, source, current_revisions.get(row.author_revision_id) if row else None)
            if payload.proof_key != proof_keys[cid]:
                raise RequiredContextUnavailable('character_cognition_decision_changed')
    # 降级跳过(记录不可用)的章不能留在 positions 里:下游快照要求
    # positions 与 sources/decisions/proofs 三个集合一致,否则弧规划会判"覆盖不全"
    # 直接 409。降级已经通过 warning + 计数器可见,这里只是让账本自洽。
    dropped = [cid for cid in positions if cid not in decision_versions]
    for cid in dropped:
        positions.pop(cid, None)
    ids = [cid for cid in ids if cid in positions]
    current_positions, _, _ = await _ledger_positions(db, project_id, boundary)
    for _cid in dropped:
        current_positions.pop(_cid, None)
    if (current_positions != positions or
            await _history_boundary(db, project_id, boundary, before_chapter_id) != boundary):
        raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
    ledger.snapshot = dict(boundary=boundary, positions=positions, sources=source_hashes,
        decisions=decision_versions, proofs=proof_keys)
    ledger.snapshot_hash = digest(ledger.snapshot)
    return ledger


async def load_prompt_context(db, project_id, *, before_global_idx=None, before_chapter_id=None,
                              chapter_outline=None, chapter_text='', previous_text='', priority_names=(), max_chars=1200,
                              defer_retrieval=False):
    """Read-only excerpt or a deferred semantic request; never embed inside DB IO.

    Generation/evaluation pass defer_retrieval=True and resolve the returned
    request after closing their read transaction. The default remains a
    database-only diagnostic excerpt for existing read-only callers.
    """
    if before_global_idx is None and before_chapter_id is None:
        from app.services.context_errors import RequiredContextUnavailable
        raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
    priority_names = tuple(priority_names)
    ledger = await load_ledger(db, project_id, before_global_idx=before_global_idx, before_chapter_id=before_chapter_id)
    context = '\n'.join((json.dumps(chapter_outline or {}, ensure_ascii=False, allow_nan=False),
                         chapter_text or '', previous_text or ''))
    rendered = serialize_for_prompt(ledger, max_chars=max_chars, priority_names=priority_names, context_text=context)
    if defer_retrieval:
        from app.services.cognition_context import history_fits
        if not history_fits(ledger, max_chars):
            from app.services.cognition_retrieval import CognitionPrompt
            return CognitionPrompt.create(project_id, ledger, context, priority_names, max_chars,
                before_global_idx=before_global_idx, before_chapter_id=before_chapter_id)
    return rendered


async def extract_and_update(db, project_id, chapter_text: str, *, chapter_id=None) -> dict:
    """Persist a source-bound delta; model IO uses no borrowed DB transaction."""
    from app.db.session import async_session_factory, engine
    from app.services.entity_sources import resolve_entity_source
    if chapter_id is None:
        raise ValueError('认知提取必须提供章节身份')
    async with async_session_factory() as session:
        source = await resolve_entity_source(session, project_id, 0, chapter_id)
    if source is None or source.content != chapter_text:
        return {}
    from app.services.cognition_extraction import run_cognition_extraction
    return await run_cognition_extraction(source, sessions=async_session_factory, engine=engine)
