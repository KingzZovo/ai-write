"""Worldview architecture extraction (dossier world section).

Unlike style/beat cards (which already exist per-slice), no worldview
artifact exists for reference books. This module derives one directly from
TextChunk rows with strict cost discipline:

  - stratified sampling: ~40 chunks evenly spaced over the whole book
    (≈60k chars) — the model never re-reads the full book;
  - MAP: batched extraction (``world_arch_extraction``) of 力量体系/规则约束/
    组织架构/地理格局/核心冲突源 candidates + the proper nouns seen;
  - REDUCE: one merge call (``world_arch_merge``) producing a 世界观架构档案
    describing SYSTEM DESIGN PATTERNS with proper nouns abstracted into
    placeholders (「主角」「A势力」) so it is reusable for new novels.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.project import TextChunk
from app.services.book_dossier import (
    LLMCallCounter,
    _dumps,
    _run_registry_structured,
    _scrub_obj,
    select_stratified,
)

logger = logging.getLogger(__name__)

SAMPLE_CHUNKS = 40
BATCH_SIZE = 8
MAX_CHARS_PER_CHUNK = 1600
MAX_CANDIDATES_PER_FIELD = 40

_FIELDS = (
    "power_system",
    "rules_constraints",
    "organizations",
    "geography",
    "conflict_sources",
)

_MAP_INSTRUCTIONS = (
    "从以下小说片段中提取世界观架构要素。只输出 JSON：\n"
    '{"power_system": ["力量/修炼体系的规则要点"], '
    '"rules_constraints": ["世界规则与硬性约束"], '
    '"organizations": ["组织/势力结构要点"], '
    '"geography": ["地理/空间格局要点"], '
    '"conflict_sources": ["核心冲突来源"], '
    '"proper_nouns": ["片段中出现的专有名词（人名/地名/门派名/功法名/法宝名）"]}\n'
    "要求：除 proper_nouns 外，其余字段不得出现具体专有名词，一律抽象为"
    "「主角」「A势力」「B功法」等占位符；每条不超过50字；没有的字段输出空数组。\n\n"
    "【小说片段】\n"
)

_MERGE_INSTRUCTIONS = (
    "以下是从一本小说分批提取并去重后的世界观架构要素候选。请合并归纳为一份"
    "「世界观架构档案」，描述该世界的系统设计模式——力量体系如何分层升级、"
    "规则如何制造冲突、势力如何互相制衡——供创作全新小说时复用。只输出 JSON：\n"
    '{"power_system": "力量体系设计模式", '
    '"rules_constraints": "规则约束设计", '
    '"organizations": "组织架构设计", '
    '"geography": "地理格局设计", '
    '"conflict_sources": "核心冲突源设计", '
    '"design_patterns": ["可复用的世界观设计模式，每条一句话"]}\n'
    "硬性要求：不得出现任何原书专有名词，一律抽象为「主角」「A势力」「圣器」"
    "等占位符；每个字符串字段不超过150字。\n\n"
)


def merge_candidates(map_outputs: list[dict]) -> tuple[dict, list[str]]:
    """Deterministically merge MAP outputs: dedupe (order-preserving) + cap.

    Returns ``(candidates_by_field, proper_nouns)``.
    """
    merged: dict[str, list[str]] = {f: [] for f in _FIELDS}
    seen: dict[str, set[str]] = {f: set() for f in _FIELDS}
    nouns: list[str] = []
    nouns_seen: set[str] = set()

    for out in map_outputs:
        if not isinstance(out, dict):
            continue
        for field in _FIELDS:
            vals = out.get(field)
            if isinstance(vals, str):
                vals = [vals]
            for v in vals or []:
                s = str(v).strip()[:80]
                if s and s not in seen[field] and len(merged[field]) < MAX_CANDIDATES_PER_FIELD:
                    seen[field].add(s)
                    merged[field].append(s)
        for n in out.get("proper_nouns") or []:
            s = str(n).strip()
            if s and s not in nouns_seen:
                nouns_seen.add(s)
                nouns.append(s)
    return merged, nouns


async def extract(
    book_id: str,
    db: AsyncSession | None = None,
    counter: LLMCallCounter | None = None,
) -> dict:
    """Extract the worldview architecture dossier section for a book.

    Returns ``{"profile": {...}, "chunks_sampled": int, "proper_nouns": [...]}``.
    ``proper_nouns`` is a scrub aid for the caller and is not meant to be
    stored in the dossier.
    """
    if db is None:
        async with async_session_factory() as session:
            return await extract(book_id, session, counter)
    counter = counter or LLMCallCounter()

    rows = await db.execute(
        select(TextChunk.content)
        .where(TextChunk.book_id == str(book_id))
        .order_by(TextChunk.sequence_id.asc())
    )
    chunks = [r[0] or "" for r in rows.all()]
    if not chunks:
        raise ValueError("no text chunks for book")

    sampled = select_stratified(chunks, SAMPLE_CHUNKS)

    map_outputs: list[dict] = []
    for i in range(0, len(sampled), BATCH_SIZE):
        batch = sampled[i:i + BATCH_SIZE]
        text = "\n\n".join(c[:MAX_CHARS_PER_CHUNK] for c in batch)
        try:
            out = await _run_registry_structured(
                "world_arch_extraction", _MAP_INSTRUCTIONS + text, db, counter
            )
            map_outputs.append(out)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "world MAP batch %d failed for book %s: %s",
                i // BATCH_SIZE, book_id, exc,
            )

    candidates, proper_nouns = merge_candidates(map_outputs)
    if not any(candidates[f] for f in _FIELDS):
        raise RuntimeError("world extraction produced no candidates")

    profile = await _run_registry_structured(
        "world_arch_merge", _MERGE_INSTRUCTIONS + _dumps(candidates), db, counter
    )
    # Defense in depth: the merge prompt demands abstraction, but scrub any
    # proper noun that slipped through using the names found during MAP.
    profile = _scrub_obj(profile, proper_nouns)

    return {
        "profile": profile,
        "chunks_sampled": len(sampled),
        "proper_nouns": proper_nouns,
    }
