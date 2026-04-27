"""
v1.5.0 C4-2: Cascade Planner

Given a freshly-recorded ``chapter_evaluations`` row whose ``overall`` score
fell below threshold AND whose auto-revise loop reported
``rounds_exhausted=true``, derive the set of upstream entities
(``outline`` / ``character`` / ``world_rule`` / ``chapter``) that need to be
regenerated before the failing chapter can be re-evaluated.

This module is the *pure planner*: it inspects the evaluation row's
``issues_json`` and the project's existing upstream rows, then returns a
dedup'd, severity-stamped list of :class:`CascadeTaskCandidate` records
ready to be inserted into ``cascade_tasks`` (or matched against existing
ones via the UNIQUE idempotency key).

It does **not** insert rows, enqueue Celery jobs, or take any DB locks --
those responsibilities live in C4-3 (``app/tasks/cascade.py``) and C4-4
(``/api/evaluate`` callback + SSE).

Mapping (dimension -> upstream entity type)
-------------------------------------------
``plot_coherence``        -> ``outline``     (project's most-recent volume-level outline)
``foreshadow_handling``   -> ``outline``     (foreshadows are anchored to outline blueprints)
``character_consistency`` -> ``character``   (matched by name substring against ``characters.name``;
                                              falls back to project-wide character pool when no
                                              name matches)
``style_adherence``       -> SKIP            (chapter-local; auto_revise already handles it)
``narrative_pacing``      -> SKIP            (chapter-local; auto_revise already handles it)

``world_rule`` is reachable via the schema CHECK constraint but no current
evaluator dimension drives it; reserved for future planner extensions.

Severity
--------
The evaluator's ``issues_json`` carries no explicit ``severity`` field
(confirmed against chapter 3 / 2026-04-27 sample, 20 issues, none with
severity).  We synthesise it:

* default ``high`` for every in-scope issue,
* escalate to ``critical`` when a single ``(target_entity_type,
  target_entity_id)`` pair accumulates ``>= CRITICAL_ISSUE_COUNT`` issues
  (default ``3``).

The ``cascade_tasks.severity`` CHECK constraint allows only ``high`` /
``critical`` -- consistent with this synthesis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character, Chapter, Outline

logger = logging.getLogger(__name__)


# --- Public constants -------------------------------------------------------

#: Below this overall score a cascade may be triggered (callers compare).
DEFAULT_OVERALL_THRESHOLD: float = 9.0

#: Issues at or above this count for a single (entity_type, entity_id)
#: pair escalate that candidate from ``high`` to ``critical``.
CRITICAL_ISSUE_COUNT: int = 3

#: Dimensions that map to upstream regeneration. Other dimensions are
#: chapter-local and handled by auto_revise.
IN_SCOPE_DIMENSIONS: frozenset[str] = frozenset(
    {"plot_coherence", "foreshadow_handling", "character_consistency"}
)

#: Allowed values for ``cascade_tasks.target_entity_type``. Mirrors the DB
#: CHECK constraint ``ck_cascade_tasks_target_entity_type``.
ALLOWED_TARGET_TYPES: frozenset[str] = frozenset(
    {"chapter", "outline", "character", "world_rule"}
)

#: Allowed values for ``cascade_tasks.severity``. Mirrors
#: ``ck_cascade_tasks_severity``.
ALLOWED_SEVERITIES: frozenset[str] = frozenset({"high", "critical"})


# --- Result dataclass -------------------------------------------------------


@dataclass(frozen=True)
class CascadeTaskCandidate:
    """One upstream regeneration target derived from an evaluation row.

    Field names match the ``cascade_tasks`` columns; the C4-3 worker
    inserts these via ``INSERT ... ON CONFLICT DO NOTHING`` keyed on
    ``uq_cascade_tasks_idem``.
    """

    project_id: str
    source_chapter_id: str
    source_evaluation_id: str
    target_entity_type: str
    target_entity_id: str
    severity: str
    issue_summary: str
    #: Dimensions that contributed to this candidate; informational only
    #: (not persisted directly -- collapsed into ``issue_summary``).
    contributing_dimensions: tuple[str, ...] = field(default_factory=tuple)
    #: Number of underlying issues collapsed into this candidate (used
    #: for the severity escalation rule).
    issue_count: int = 1


# --- Trigger helper ---------------------------------------------------------


def should_trigger_cascade(
    *,
    overall: float | None,
    rounds_exhausted: bool,
    threshold: float = DEFAULT_OVERALL_THRESHOLD,
) -> bool:
    """Predicate used by C4-4 callers.

    The planner itself does not enforce this -- callers decide when to
    plan. We expose the rule here so /api/evaluate and the auto_revise
    loop share one source of truth.
    """
    if not rounds_exhausted:
        return False
    if overall is None:
        return False
    return overall < threshold


# --- Internal helpers -------------------------------------------------------


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _normalise_issues(issues_json: Any) -> list[dict[str, Any]]:
    """Defensive normaliser. ``issues_json`` is JSON, may already be a
    Python list (asyncpg + SQLAlchemy decode), or could be ``None`` / a
    string in pathological cases."""
    if issues_json is None:
        return []
    if isinstance(issues_json, list):
        return [i for i in issues_json if isinstance(i, Mapping)]
    # Fall back: tolerate dict-of-dimensions shape if anyone hands us the
    # raw evaluator payload by mistake.
    if isinstance(issues_json, Mapping):
        flat: list[dict[str, Any]] = []
        for dim, payload in issues_json.items():
            if not isinstance(payload, Mapping):
                continue
            for issue in payload.get("issues", []) or []:
                if isinstance(issue, Mapping):
                    item = dict(issue)
                    item.setdefault("dimension", dim)
                    flat.append(item)
        return flat
    return []


def _truncate(text: str, limit: int = 240) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def _match_character_names(
    description: str, characters: Sequence[Character]
) -> list[Character]:
    if not description:
        return []
    matched: list[Character] = []
    seen: set[UUID] = set()
    for ch in characters:
        name = (ch.name or "").strip()
        if not name:
            continue
        if name in description and ch.id not in seen:
            matched.append(ch)
            seen.add(ch.id)
    return matched


async def _load_target_outline(
    db: AsyncSession, project_id: str
) -> Outline | None:
    """Pick the outline row that should be regenerated for plot/
    foreshadow issues. Strategy: prefer the most-recently-created
    confirmed outline for the project; fall back to the most-recent of
    any status."""
    stmt = (
        select(Outline)
        .where(Outline.project_id == project_id)
        .order_by(Outline.is_confirmed.desc(), Outline.created_at.desc())
        .limit(1)
    )
    res = await db.execute(stmt)
    return res.scalar_one_or_none()


async def _load_project_characters(
    db: AsyncSession, project_id: str
) -> list[Character]:
    stmt = select(Character).where(Character.project_id == project_id)
    res = await db.execute(stmt)
    return list(res.scalars().all())


# --- Main planner -----------------------------------------------------------


@dataclass
class _Bucket:
    target_entity_type: str
    target_entity_id: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    dimensions: set[str] = field(default_factory=set)


async def plan_cascade(
    *,
    db: AsyncSession,
    project_id: str,
    source_chapter_id: str,
    source_evaluation_id: str,
    issues_json: Any,
) -> list[CascadeTaskCandidate]:
    """Plan the cascade for one evaluation row.

    Returns a deduplicated list of :class:`CascadeTaskCandidate`. The
    list may be empty (no in-scope issues / no resolvable upstream
    entity).

    The function is *idempotent and side-effect-free*: it issues only
    SELECTs and never writes to the DB.
    """
    issues = _normalise_issues(issues_json)
    if not issues:
        return []

    in_scope = [i for i in issues if i.get("dimension") in IN_SCOPE_DIMENSIONS]
    if not in_scope:
        return []

    # Lazily-loaded upstream rows. Only hit the DB if a dimension actually
    # demands the lookup.
    cached_outline: Outline | None | _Sentinel = _SENTINEL
    cached_characters: list[Character] | _Sentinel = _SENTINEL

    buckets: dict[tuple[str, str], _Bucket] = {}

    project_id_s = _coerce_str(project_id)
    source_chapter_id_s = _coerce_str(source_chapter_id)
    source_evaluation_id_s = _coerce_str(source_evaluation_id)

    for issue in in_scope:
        dim = issue.get("dimension")
        description = str(issue.get("description") or "")

        if dim in ("plot_coherence", "foreshadow_handling"):
            if cached_outline is _SENTINEL:
                cached_outline = await _load_target_outline(db, project_id_s)
            outline = cached_outline
            if outline is None:
                logger.info(
                    "cascade_planner: no outline for project %s; skipping %s issue",
                    project_id_s,
                    dim,
                )
                continue
            _add_to_bucket(
                buckets,
                target_entity_type="outline",
                target_entity_id=_coerce_str(outline.id),
                dimension=str(dim),
                issue=issue,
            )
            continue

        if dim == "character_consistency":
            if cached_characters is _SENTINEL:
                cached_characters = await _load_project_characters(
                    db, project_id_s
                )
            characters = cached_characters
            if not characters:
                logger.info(
                    "cascade_planner: no characters for project %s; skipping character issue",
                    project_id_s,
                )
                continue
            matched = _match_character_names(description, characters)
            # Fallback: when no name match, fan out to ALL project chars
            # so the upstream regenerator at least sees the issue. This
            # mirrors the conservative "better over-cover than miss"
            # stance: dedup is by UNIQUE key in the DB anyway.
            targets = matched or list(characters)
            for ch in targets:
                _add_to_bucket(
                    buckets,
                    target_entity_type="character",
                    target_entity_id=_coerce_str(ch.id),
                    dimension=str(dim),
                    issue=issue,
                )
            continue

        # Defensive: an unexpected in-scope dim slipped through.
        logger.warning(
            "cascade_planner: unhandled in-scope dimension %r; ignoring", dim
        )

    # Materialise buckets -> candidates.
    candidates: list[CascadeTaskCandidate] = []
    for (etype, eid), bucket in buckets.items():
        severity = (
            "critical" if len(bucket.issues) >= CRITICAL_ISSUE_COUNT else "high"
        )
        summary = _build_issue_summary(bucket.issues)
        candidates.append(
            CascadeTaskCandidate(
                project_id=project_id_s,
                source_chapter_id=source_chapter_id_s,
                source_evaluation_id=source_evaluation_id_s,
                target_entity_type=etype,
                target_entity_id=eid,
                severity=severity,
                issue_summary=summary,
                contributing_dimensions=tuple(sorted(bucket.dimensions)),
                issue_count=len(bucket.issues),
            )
        )

    # Sort for deterministic output (helpful for tests + logs).
    candidates.sort(
        key=lambda c: (
            c.target_entity_type,
            c.target_entity_id,
            c.severity,
        )
    )
    return candidates


def _add_to_bucket(
    buckets: dict[tuple[str, str], _Bucket],
    *,
    target_entity_type: str,
    target_entity_id: str,
    dimension: str,
    issue: Mapping[str, Any],
) -> None:
    if target_entity_type not in ALLOWED_TARGET_TYPES:
        raise ValueError(
            f"cascade_planner: target_entity_type {target_entity_type!r} not in "
            f"{sorted(ALLOWED_TARGET_TYPES)}"
        )
    key = (target_entity_type, target_entity_id)
    bucket = buckets.get(key)
    if bucket is None:
        bucket = _Bucket(
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
        )
        buckets[key] = bucket
    bucket.issues.append(dict(issue))
    bucket.dimensions.add(dimension)


def _build_issue_summary(issues: Iterable[Mapping[str, Any]]) -> str:
    """Compact, human-readable summary persisted to ``issue_summary``.

    Limits the total length so we don't bloat the cascade_tasks row;
    full issues remain queryable via the source_evaluation_id FK.
    """
    lines: list[str] = []
    for idx, issue in enumerate(issues):
        if idx >= 5:
            lines.append(f"... (+{sum(1 for _ in issues) - 5} more)")
            break
        dim = issue.get("dimension") or "?"
        loc = issue.get("location")
        desc = _truncate(str(issue.get("description") or ""), 160)
        loc_part = f"@{loc}" if loc not in (None, "") else ""
        lines.append(f"[{dim}{loc_part}] {desc}")
    return "\n".join(lines)[:2000]


class _Sentinel:
    """Marker for unloaded lazy-cache slots (distinct from ``None``)."""

    __slots__ = ()


_SENTINEL = _Sentinel()


__all__ = [
    "DEFAULT_OVERALL_THRESHOLD",
    "CRITICAL_ISSUE_COUNT",
    "IN_SCOPE_DIMENSIONS",
    "ALLOWED_TARGET_TYPES",
    "ALLOWED_SEVERITIES",
    "CascadeTaskCandidate",
    "plan_cascade",
    "should_trigger_cascade",
]
