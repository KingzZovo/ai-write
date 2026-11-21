"""Deterministic check: does a chapter's prose actually set up its planned foreshadows?

The gap this closes: an outline can plan a foreshadow at a chapter, and the
chapter text can then omit the setup entirely. The existing foreshadow
subsystem covers plans, discovery, author confirmation and lifecycle, but
nothing compared "what the plan says" against "what the chapter says".

This module only compares the current plan view (read from the current
outlines) with the current chapter prose and reports the items whose keywords
appear nowhere in that prose. It is purely read-only: no model call, no write,
no queue, and it is deliberately NOT part of any blocking gate.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select

from app.models.project import Chapter, Volume
from app.services.foreshadow_plans import read_foreshadow_plans
from app.services.foreshadow_scope import load_foreshadow_positions

# Bounded, explicit limits: an unbounded keyword list turns a plan description
# into a bag of stop words that matches any prose.
MAX_KEYWORDS = 8
MIN_KEYWORD_LEN = 2
EVIDENCE_CONTEXT = 30
EVIDENCE_LIMIT = 120
# plan_entries fills this in when the source array omits "type"; it names the
# mechanic, not anything the prose would say, so it is never a search anchor.
DEFAULT_CATEGORY = '伏笔'

# Anything that is not ASCII alphanumeric or CJK separates words. Full-width
# forms are folded to half-width first (U+FF0C, U+3000, U+FF1B all act as breaks).
_TOKEN_SPLIT = re.compile(r'[^0-9a-z\u4e00-\u9fff]+')
# High-frequency function characters never anchor a planned noun; splitting on
# them keeps "寄信的存根" from becoming one unmatchable 5-character token while
# leaving ordinary nouns intact.
_FUNCTION_SPLIT = re.compile('[的了着过而或之其已又]+')
_CJK = re.compile(r'[\u4e00-\u9fff]')


class ForeshadowSetupChapterUnavailable(LookupError):
    """The chapter does not exist in this project (or has no readable identity)."""


@dataclass(frozen=True)
class ForeshadowSetupItem:
    """One planned foreshadow judged against this chapter's prose."""

    plan_key: str
    title: str
    keywords: tuple[str, ...]
    grounded: bool
    evidence: str | None = None


@dataclass(frozen=True)
class ForeshadowSetupReport:
    chapter_id: str
    planned: int
    grounded: int
    missing: list[ForeshadowSetupItem]
    items: list[ForeshadowSetupItem]


def _pieces(source, *, cjk_only=False):
    """Split one plan field into normalized keyword candidates, order kept."""
    if not isinstance(source, str) or not source:
        return []
    folded = unicodedata.normalize('NFKC', source).lower()
    result = []
    for chunk in _TOKEN_SPLIT.split(folded):
        for piece in _FUNCTION_SPLIT.split(chunk):
            if len(piece) < MIN_KEYWORD_LEN:
                continue
            if cjk_only and not _CJK.search(piece):
                continue
            result.append(piece)
    return result


def keyword_tokens(description=None, hint=None, category=None):
    """Deterministic keyword list for one plan entry, longest-lived field first.

    ``read_foreshadow_plans`` exposes ``description``, ``type`` and a rendered
    ``hint``; there is no separate keyword field, so the searchable anchors are
    derived from those three. ``hint`` may be JSON, so only its CJK fragments
    are used (JSON key names are not prose anchors). The explicit ``type`` is
    last, so a generic type word cannot crowd out the description's nouns.
    """
    ordered, seen = [], set()

    def add(values):
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)

    add(_pieces(description))
    add(_pieces(hint, cjk_only=True))
    add(piece for piece in _pieces(category) if piece != DEFAULT_CATEGORY)
    return tuple(ordered[:MAX_KEYWORDS])


def _normalize_text(text):
    """Fold prose to the keyword form while preserving character indices."""
    if not text:
        return ''
    out = []
    for char in text:
        folded = unicodedata.normalize('NFKC', char)
        if len(folded) != 1:
            out.append(char)
            continue
        lowered = folded.lower()
        out.append(lowered if len(lowered) == 1 else folded)
    return ''.join(out)


def _evidence(text, index, length):
    """Quote the hit with ~30 characters of context from the original prose."""
    start = max(0, index - EVIDENCE_CONTEXT)
    end = min(len(text), index + length + EVIDENCE_CONTEXT)
    return text[start:end][:EVIDENCE_LIMIT]


def build_report(chapter_id, plans, text):
    """Ground every plan entry against one chapter text (pure, no I/O)."""
    normalized = _normalize_text(text or '')
    items = []
    for plan in plans:
        plan_key = str(plan.get('id') or '')
        title = str(plan.get('description') or '')
        keywords = keyword_tokens(title, plan.get('hint'), plan.get('type'))
        item = ForeshadowSetupItem(plan_key=plan_key, title=title, keywords=keywords,
                                   grounded=False, evidence=None)
        # No usable keyword means "not shown in this prose", never "grounded".
        for keyword in keywords:
            index = normalized.find(keyword)
            if index >= 0:
                item = replace(item, grounded=True, evidence=_evidence(text, index, len(keyword)))
                break
        items.append(item)
    missing = [item for item in items if not item.grounded]
    return ForeshadowSetupReport(chapter_id=str(chapter_id), planned=len(items),
                                 grounded=len(items) - len(missing), missing=missing, items=items)


async def check_chapter_foreshadow_setup(db, *, project_id, chapter_id) -> ForeshadowSetupReport:
    """Report which foreshadows planned for this chapter are absent from its prose."""
    try:
        pid = UUID(str(project_id))
        cid = UUID(str(chapter_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ForeshadowSetupChapterUnavailable('foreshadow_setup_chapter_unavailable') from exc

    # Body column only, and the volume join is what proves project membership.
    row = (await db.execute(
        select(Chapter.content_text)
        .select_from(Chapter)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Chapter.id == cid, Volume.project_id == pid)
        .execution_options(autoflush=False)
    )).first()
    if row is None:
        raise ForeshadowSetupChapterUnavailable('foreshadow_setup_chapter_unavailable')
    text = row[0] or ''

    positions = await load_foreshadow_positions(db, pid)
    position = positions.chapter(str(cid))
    boundary = None
    if position is not None:
        try:
            # Read plans up to this chapter in outline order, then keep this
            # chapter's own entries. Unknown/ambiguous ordering falls back to
            # the unbounded view and stays filtered by chapter identity.
            boundary = positions.boundary(global_idx=position['global_idx'])
        except ValueError:
            boundary = None
    plans = (await read_foreshadow_plans(db, pid, positions=positions, boundary=boundary))['plans']
    mine = [plan for plan in plans if str(plan.get('chapter_id') or '') == str(cid)]
    return build_report(str(cid), mine, text)


def setup_event_payload(report: ForeshadowSetupReport | None, *, limit: int = 5) -> dict | None:
    """SSE payload for \"planned foreshadow absent from this chapter\", or ``None``.

    Warning-only by contract: the caller emits this after a save; nothing here
    blocks a save, changes a status, or writes to the database. Returns ``None``
    when there is nothing to say, so callers can stay branch-free.
    """
    if report is None or report.planned <= 0 or not report.missing:
        return None
    head = report.missing[: max(0, int(limit))]
    return {
        'event': 'foreshadow_setup_missing',
        'status': 'warning',
        'chapter_id': report.chapter_id,
        'planned': report.planned,
        'grounded': report.grounded,
        'missing_count': len(report.missing),
        'missing': [
            {'plan_key': item.plan_key, 'title': item.title, 'keywords': list(item.keywords)}
            for item in head
        ],
    }