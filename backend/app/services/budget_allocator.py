"""Budget allocator for target_word_count across project/volume/chapter.

v1.3.0 chunk-29 (non-destructive).

- No DB coupling. Pure functions + a small planner that operates on
  lightweight dict structures so callers can integrate with SQLAlchemy or
  any other source.
- Strategy: equal split (weighted path reserved for chunk-30+).
- Override semantics:
    * force=True                -> always overwrite.
    * force=False (default)     -> overwrite only when the current value
      equals the documented default ("untouched").
- Remainder handling: integer division; the leftover is appended to the
  last slot (last volume / last chapter).
"""

from __future__ import annotations

from typing import Iterable, Optional

PROJECT_DEFAULT: int = 3_000_000
VOLUME_DEFAULT: int = 200_000
CHAPTER_DEFAULT: int = 50_000


def allocate_even(total: int, n: int) -> list[int]:
    """Split ``total`` into ``n`` non-negative ints summing exactly to ``total``.

    The remainder (``total % n``) is added to the last slot.
    """
    if n <= 0:
        raise ValueError("n must be >= 1")
    if total < 0:
        raise ValueError("total must be >= 0")
    base, rem = divmod(total, n)
    out = [base] * n
    out[-1] += rem
    return out


def allocate_weighted(total: int, weights: Iterable[float]) -> list[int]:
    """Split ``total`` according to non-negative weights.

    Remainder (rounding residue) is absorbed by the last slot so the sum is
    exact. If all weights are zero the call falls back to :func:`allocate_even`.
    Reserved for chunk-30+; the project allocator currently takes the
    even-split path.
    """
    ws = [float(w) for w in weights]
    if not ws:
        raise ValueError("weights must be non-empty")
    if any(w < 0 for w in ws):
        raise ValueError("weights must be non-negative")
    if total < 0:
        raise ValueError("total must be >= 0")
    s = sum(ws)
    if s == 0:
        return allocate_even(total, len(ws))
    out = [int(total * w / s) for w in ws]
    out[-1] += total - sum(out)
    return out


def _should_overwrite(
    current: Optional[int], default_value: int, force: bool
) -> bool:
    """Heuristic for whether the allocator may replace ``current``.

    - ``force=True`` always overwrites.
    - Otherwise overwrite only when the value is missing or still equals the
      documented default (treated as "untouched by the user").
    """
    if force:
        return True
    if current is None:
        return True
    return int(current) == default_value


def allocate_project_budget(
    *,
    project_total: int,
    volumes: list[dict],
    force: bool = False,
) -> dict:
    """Compute budget updates for a project's volumes and their chapters.

    ``volumes`` is an ordered list of dicts shaped like::

        {
            "id": str,
            "volume_idx": int,
            "current_target": int,
            "chapters": [
                {"id": str, "chapter_idx": int, "current_target": int},
                ...
            ],
        }

    The returned plan is pure data; applying it to the DB is the caller's
    responsibility.
    """
    if project_total < 0:
        raise ValueError("project_total must be >= 0")

    n_vols = len(volumes)
    if n_vols == 0:
        return {
            "project_total": project_total,
            "volumes": [],
            "volume_sum": 0,
            "volumes_changed": 0,
            "chapters_changed": 0,
        }

    vol_allocations = allocate_even(project_total, n_vols)
    out_volumes: list[dict] = []
    volumes_changed = 0
    chapters_changed = 0

    for vol, vol_alloc in zip(volumes, vol_allocations):
        vol_current = int(vol.get("current_target") or 0)
        vol_overwrite = _should_overwrite(vol_current, VOLUME_DEFAULT, force)
        vol_new = vol_alloc if vol_overwrite else vol_current

        chapters = list(vol.get("chapters") or [])
        ch_plan: list[dict] = []
        if chapters:
            ch_allocs = allocate_even(vol_new, len(chapters))
            for ch, ch_alloc in zip(chapters, ch_allocs):
                ch_current = int(ch.get("current_target") or 0)
                ch_overwrite = _should_overwrite(
                    ch_current, CHAPTER_DEFAULT, force
                )
                ch_new = ch_alloc if ch_overwrite else ch_current
                ch_changed = ch_overwrite and (ch_new != ch_current)
                if ch_changed:
                    chapters_changed += 1
                ch_plan.append(
                    {
                        "id": ch.get("id"),
                        "chapter_idx": ch.get("chapter_idx"),
                        "old_target": ch_current,
                        "new_target": ch_new,
                        "changed": ch_changed,
                    }
                )

        v_changed = vol_overwrite and (vol_new != vol_current)
        if v_changed:
            volumes_changed += 1
        out_volumes.append(
            {
                "id": vol.get("id"),
                "volume_idx": vol.get("volume_idx"),
                "old_target": vol_current,
                "new_target": vol_new,
                "changed": v_changed,
                "chapters": ch_plan,
            }
        )

    volume_sum = sum(v["new_target"] for v in out_volumes)
    return {
        "project_total": project_total,
        "volumes": out_volumes,
        "volume_sum": volume_sum,
        "volumes_changed": volumes_changed,
        "chapters_changed": chapters_changed,
    }
