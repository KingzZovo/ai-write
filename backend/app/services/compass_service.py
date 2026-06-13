"""Narrative compass: direction anchor + completion gate (C4 / F3, ainovel).

Adapted from voocel/ainovel-cli's compass (design idea; wording our own).

The hierarchical outline answers "what happens next" but not "where does the
whole book land, and how far off is it". Over a long rolling outline that's the
main source of drift. The compass is a tiny per-project structure:

- ending_direction: the thematic terminus, one sentence,
- open_threads: live long-line ledger ``[{thread, since_chapter, status}]``,
- estimated_scale: a *range* (min/max chapters & volumes) -- never a single
  number, so mid-book adjustment stays possible.

Scale derivation is pure code; ending/threads come from a small LLM extraction
(reusing the cognition extractor's JSON-tolerant pattern); completion readiness
is mostly hard code with a couple of manual-check escalations.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

READER_SCALE_SLACK = 0.15  # +/-15% band around the derived chapter count
CLAMP_BAND = 0.30          # LLM scale adjustments may not exceed +/-30% of derived


# --- pure scale derivation --------------------------------------------------


def derive_estimated_scale(target_word_count: Any) -> dict:
    """Translate a target word count into a chapter/volume *range*.

    Reuses outline_generator.compute_scale for the midpoint, then widens to a
    +/-15% band. Returns an empty dict when the target is missing/non-positive.
    """
    from app.services.outline_generator import compute_scale

    scale = compute_scale(target_word_count)
    if not scale:
        return {}
    n_ch = int(scale.get("n_chapters") or 0)
    n_vol = int(scale.get("n_volumes") or 0)
    if n_ch <= 0:
        return {}
    lo = max(1, int(n_ch * (1 - READER_SCALE_SLACK)))
    hi = max(lo + 1, int(round(n_ch * (1 + READER_SCALE_SLACK))))
    vlo = max(1, int(n_vol * (1 - READER_SCALE_SLACK))) if n_vol else 1
    vhi = max(vlo, int(round(n_vol * (1 + READER_SCALE_SLACK)))) if n_vol else vlo
    return {
        "min_chapters": lo,
        "max_chapters": hi,
        "min_volumes": vlo,
        "max_volumes": vhi,
        "derived_chapters": n_ch,
    }


def clamp_scale(proposed: dict, derived: dict) -> dict:
    """Keep an LLM-proposed scale a valid range within +/-30% of the derived.

    Guarantees min<max, width >= 50% of the original derived band, and that the
    range stays inside the derived chapter count's +/-30% envelope.
    """
    if not derived:
        return proposed or {}
    dch = int(derived.get("derived_chapters") or derived.get("max_chapters") or 0)
    if dch <= 0:
        return derived
    floor = max(1, int(dch * (1 - CLAMP_BAND)))
    ceil = int(round(dch * (1 + CLAMP_BAND)))

    def _int(v, fallback):
        try:
            return int(v)
        except (TypeError, ValueError):
            return fallback

    lo = _int((proposed or {}).get("min_chapters"), derived.get("min_chapters"))
    hi = _int((proposed or {}).get("max_chapters"), derived.get("max_chapters"))
    lo = max(floor, min(lo, ceil))
    hi = max(floor, min(hi, ceil))
    if hi <= lo:
        hi = min(ceil, lo + max(1, int(dch * 0.05)))
    # Preserve volume bounds + derived anchor from the derived scale.
    out = dict(derived)
    out["min_chapters"] = lo
    out["max_chapters"] = hi
    return out


# --- serialization for prompts ----------------------------------------------


def render_compass_anchor(compass: dict, current_chapter_idx: int = 0, max_chars: int = 400) -> str:
    """Compact direction anchor for the generation prompt. Empty -> ""."""
    if not compass:
        return ""
    ending = (compass.get("ending_direction") or "").strip()
    threads = compass.get("open_threads") or []
    scale = compass.get("estimated_scale") or {}
    if not ending and not threads and not scale:
        return ""
    lines = ["【方向锚】"]
    if ending:
        lines.append(f"终局命题：{ending}")
    active = [
        t.get("thread", "") for t in threads
        if isinstance(t, dict) and t.get("status") in (None, "active", "closing")
    ][:5]
    if active:
        lines.append("活跃长线（不得提前收束/遗忘）：" + "；".join(a for a in active if a))
    if scale.get("min_chapters") and scale.get("max_chapters"):
        prog = f"，当前第{current_chapter_idx}章" if current_chapter_idx else ""
        lines.append(
            f"全书规模：约{scale['min_chapters']}-{scale['max_chapters']}章{prog}"
        )
    block = "\n".join(lines)
    return block[:max_chars]


# --- persistence ------------------------------------------------------------


async def load_compass(db, project_id) -> dict:
    """Load the compass row as a plain dict, or {} if absent."""
    from sqlalchemy import select

    from app.models.project import NarrativeCompass

    row = (
        await db.execute(
            select(
                NarrativeCompass.ending_direction,
                NarrativeCompass.open_threads,
                NarrativeCompass.estimated_scale,
            ).where(NarrativeCompass.project_id == str(project_id))
        )
    ).first()
    if not row:
        return {}
    return {
        "ending_direction": row[0] or "",
        "open_threads": row[1] or [],
        "estimated_scale": row[2] or {},
    }


async def save_compass(db, project_id, compass: dict) -> None:
    """Upsert the compass row."""
    from sqlalchemy.dialects.postgresql import insert

    from app.models.project import NarrativeCompass

    values = {
        "project_id": str(project_id),
        "ending_direction": compass.get("ending_direction", "") or "",
        "open_threads": compass.get("open_threads", []) or [],
        "estimated_scale": compass.get("estimated_scale", {}) or {},
    }
    await db.execute(
        insert(NarrativeCompass)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["project_id"],
            set_={
                "ending_direction": values["ending_direction"],
                "open_threads": values["open_threads"],
                "estimated_scale": values["estimated_scale"],
            },
        )
    )
    await db.commit()


# --- LLM-driven init/update (JSON-tolerant, fail-safe) ----------------------

_INIT_PROMPT = """你在为一部长篇小说建立"方向锚"。基于全书大纲，只输出 JSON 对象：
{"ending_direction": "一句话主题性终局（人物在什么命题上做出抉择/抵达什么状态）",
 "open_threads": [{"thread": "长线/关系线/伏笔一句话", "status": "active"}]}
open_threads 最多 8 条，只列贯穿全书的主线，不要罗列细节。没有可写空数组。"""

_UPDATE_PROMPT = """你在更新一部长篇小说的"方向锚"。给定当前方向锚和新一卷大纲，只输出 JSON：
{"ending_direction": "（如需微调则给出，否则原样返回）",
 "open_threads": [{"thread": "...", "status": "active|closing|closed"}],
 "scale_hint": {"min_chapters": int, "max_chapters": int}}
把本卷已收束的长线标记 closed；新增贯穿性长线追加；scale_hint 仅在确需调整篇幅时给出，且必须是区间。"""


def _parse_json_object(text: str) -> dict:
    """Tolerant JSON-object parse (mirrors character_cognition._parse_changes)."""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
    try:
        data = json.loads(t, strict=False)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            data = json.loads(t[start:end + 1], strict=False)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


async def _llm_json(system: str, user: str) -> dict:
    from app.services.model_router import get_model_router_async

    router = await get_model_router_async()
    result = await router.generate_with_tier_fallback(
        task_type="evaluation",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=900,
        _log_meta={"caller": "compass_service"},
    )
    return _parse_json_object(result.text)


def _book_outline_text(content_json: Any) -> str:
    if isinstance(content_json, dict):
        return (content_json.get("raw_text") or json.dumps(content_json, ensure_ascii=False))[:6000]
    if isinstance(content_json, str):
        return content_json[:6000]
    return ""


async def initialize_from_book_outline(db, project_id, content_json, target_word_count=None) -> dict:
    """Initialize the compass from the book outline. Fail-safe: never raises.

    The deterministic scale is persisted FIRST so it survives even when the LLM
    enrichment (ending_direction / open_threads) is unavailable -- the scale
    range alone already powers the completion-readiness floor/ceiling checks and
    the generation anchor's scale line.
    """
    compass: dict = {}
    try:
        compass = await load_compass(db, project_id)
        scale = derive_estimated_scale(target_word_count)
        if scale:
            compass["estimated_scale"] = scale
            # Persist the deterministic part immediately (LLM-independent).
            await save_compass(db, project_id, compass)
    except Exception as exc:
        logger.warning("compass scale init failed (project=%s): %s", project_id, exc)
        return compass

    # LLM enrichment is best-effort on top of the persisted scale.
    try:
        text = _book_outline_text(content_json)
        if text.strip():
            data = await _llm_json(_INIT_PROMPT, f"【全书大纲】\n{text}")
            if data.get("ending_direction"):
                compass["ending_direction"] = str(data["ending_direction"])[:500]
            threads = data.get("open_threads")
            if isinstance(threads, list):
                compass["open_threads"] = [
                    {"thread": str(t.get("thread", ""))[:200],
                     "status": t.get("status", "active")}
                    for t in threads if isinstance(t, dict) and t.get("thread")
                ][:8]
            await save_compass(db, project_id, compass)
    except Exception as exc:
        logger.warning(
            "compass LLM enrichment failed (project=%s, scale kept): %s", project_id, exc
        )
    return compass


async def update_on_new_volume(db, project_id, volume_outline_json) -> dict:
    """Update threads + scale when a new volume is planned. Fail-safe."""
    try:
        compass = await load_compass(db, project_id)
        if not compass:
            return {}
        vtext = json.dumps(volume_outline_json, ensure_ascii=False)[:4000] \
            if not isinstance(volume_outline_json, str) else volume_outline_json[:4000]
        current = json.dumps(
            {"ending_direction": compass.get("ending_direction", ""),
             "open_threads": compass.get("open_threads", [])},
            ensure_ascii=False,
        )
        data = await _llm_json(
            _UPDATE_PROMPT, f"【当前方向锚】\n{current}\n\n【新卷大纲】\n{vtext}"
        )
        if data.get("ending_direction"):
            compass["ending_direction"] = str(data["ending_direction"])[:500]
        threads = data.get("open_threads")
        if isinstance(threads, list) and threads:
            compass["open_threads"] = [
                {"thread": str(t.get("thread", ""))[:200],
                 "status": t.get("status", "active")}
                for t in threads if isinstance(t, dict) and t.get("thread")
            ][:8]
        hint = data.get("scale_hint")
        if isinstance(hint, dict):
            compass["estimated_scale"] = clamp_scale(hint, compass.get("estimated_scale", {}))
        await save_compass(db, project_id, compass)
        return compass
    except Exception as exc:
        logger.warning("compass update failed (project=%s): %s", project_id, exc)
        return {}


# --- completion readiness (code + manual-check) -----------------------------


def assess_completion_readiness(
    compass: dict,
    written_chapters: int,
    unresolved_foreshadows: int,
    recent_summaries: list[str] | None = None,
) -> dict:
    """Six-point completion checklist. Hard checks block; soft ones escalate.

    Returns ``{can_complete, blockers, warnings, manual_checks}``.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    manual_checks: list[str] = []

    scale = (compass or {}).get("estimated_scale") or {}
    min_ch = int(scale.get("min_chapters") or 0)
    max_ch = int(scale.get("max_chapters") or 0)

    # 1. Scale floor.
    if min_ch and written_chapters < min_ch:
        blockers.append(f"规模未达下限：已写 {written_chapters} 章 < 约定下限 {min_ch} 章。")
    # 2. Active threads.
    threads = (compass or {}).get("open_threads") or []
    active = [t.get("thread", "") for t in threads
              if isinstance(t, dict) and t.get("status") in (None, "active", "closing")]
    if active:
        blockers.append("以下长线尚未收束：" + "；".join(a for a in active if a))
    # 3. Unresolved foreshadows.
    if unresolved_foreshadows > 0:
        blockers.append(f"尚有 {unresolved_foreshadows} 条伏笔未回收。")
    # 4. Ending direction answered -> manual.
    if (compass or {}).get("ending_direction"):
        manual_checks.append(
            f"终局命题是否已正面回答：「{compass['ending_direction']}」（请对照最近章节人工确认）。"
        )
    # 5. Over max -> should wrap.
    if max_ch and written_chapters > max_ch:
        warnings.append(f"已写 {written_chapters} 章 > 约定上限 {max_ch} 章，应尽快收束。")
    # 6. "Open-ended daily steady-state mistaken for an ending" heuristic.
    if recent_summaries:
        joined = " ".join(recent_summaries[-5:])
        conflict_words = ("冲突", "危机", "决战", "对峙", "抉择", "牺牲", "真相", "反转")
        if not any(w in joined for w in conflict_words):
            warnings.append(
                "最近数章无明显冲突/危机词，警惕把开放式日常稳态误判为终点。"
            )

    return {
        "can_complete": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "manual_checks": manual_checks,
    }
