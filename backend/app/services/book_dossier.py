"""Book dossier — consolidation layer over the decompile micro-cards.

The decompile pipeline produces tens of thousands of per-slice
StyleProfileCard / BeatSheetCard rows that are never aggregated. This module
condenses them into ONE structured per-book dossier stored in
``ReferenceBook.metadata_json["dossier"]``:

  - consolidate_style: deterministic MAP over all style cards (categorical
    counts + stratified representative cards), then a single LLM REDUCE call
    that emits a book-level style profile.
  - consolidate_plot: deterministic MAP over all beat cards grouped by
    chapter (scene-type distributions, climax spacing, foreshadow
    plant→payoff distances, per-chapter beat patterns), then a single LLM
    REDUCE call that emits a plot-architecture profile.
  - worldview_extractor.extract (separate module): stratified TextChunk
    sampling + batched MAP extraction + one merge REDUCE.
  - build_dossier: runs all three (tolerating partial failure), renders three
    compact injection blocks (proper nouns scrubbed) and persists the result.

Cost discipline: everything aggregatable stays in Python; the model only
consumes compressed artifacts (统计 + 代表性卡片), never the full book.
Total LLM calls per consolidation ≈ 8 (1 style + 1 plot + ~5 world MAP +
1 world merge), logged via ``LLMCallCounter``.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import async_session_factory
from app.models.decompile import BeatSheetCard, ReferenceBookSlice, StyleProfileCard
from app.models.project import ReferenceBook
from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)

# Contract caps (a parallel agent codes injection against these).
STYLE_BLOCK_CAP = 1200
STRUCTURE_BLOCK_CAP = 800
WORLD_BLOCK_CAP = 1000

REPRESENTATIVE_CARDS = 30
EVIDENCE_EXCERPT_CHARS = 120

_CLIMAX_PAT = re.compile(r"高潮|决战|爆发|大战|摊牌|反杀|逆转")
_PAYOFF_PAT = re.compile(r"回收|兑现|揭晓|应验|呼应|揭示")
_PLANT_PAT = re.compile(r"埋|设下|铺垫|伏笔|留下|暗示")
_EMPTY_VALUES = {"", "无", "none", "null", "n/a"}


class LLMCallCounter:
    """Counts LLM calls per consolidation so cost discipline is observable."""

    def __init__(self) -> None:
        self.total = 0
        self.by_task: Counter[str] = Counter()

    def tick(self, task_type: str) -> None:
        self.total += 1
        self.by_task[task_type] += 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =========================================================================
# Deterministic MAP helpers (pure Python, no LLM — unit-testable)
# =========================================================================

def select_stratified(items: list, k: int) -> list:
    """Pick ``k`` items evenly spaced across the sequence (first + last kept).

    Deterministic diversity sampling: with n items and k picks, indices are
    ``round(i * (n-1) / (k-1))``. Returns all items when ``n <= k``.
    """
    n = len(items)
    if k <= 0:
        return []
    if n <= k:
        return list(items)
    if k == 1:
        return [items[0]]
    idxs = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    return [items[i] for i in idxs]


def _norm(val) -> str:
    s = str(val or "").strip()
    return "" if s.lower() in _EMPTY_VALUES else s


def _top(counter: Counter, n: int) -> list[dict]:
    return [{"value": v, "count": c} for v, c in counter.most_common(n)]


def aggregate_style_stats(profiles: list[dict]) -> dict:
    """Aggregate categorical fields of style cards into frequency tables."""
    pov: Counter[str] = Counter()
    tense: Counter[str] = Counter()
    pacing: Counter[str] = Counter()
    register: Counter[str] = Counter()
    vocab: Counter[str] = Counter()
    moves: Counter[str] = Counter()
    tells: Counter[str] = Counter()

    scalar_fields = (
        ("pov", pov), ("tense", tense), ("pacing", pacing),
        ("emotional_register", register),
    )
    list_fields = (
        ("vocab_tone", vocab), ("signature_moves", moves),
        ("forbidden_tells", tells),
    )
    for p in profiles:
        if not isinstance(p, dict):
            continue
        for field, ctr in scalar_fields:
            v = _norm(p.get(field))
            if v:
                ctr[v[:40]] += 1
        for field, ctr in list_fields:
            vals = p.get(field)
            if isinstance(vals, str):
                vals = [vals]
            for v in vals or []:
                v = _norm(v)
                if v:
                    ctr[v[:60]] += 1

    return {
        "cards": len(profiles),
        "pov": _top(pov, 5),
        "tense": _top(tense, 3),
        "pacing": _top(pacing, 8),
        "emotional_register": _top(register, 8),
        "vocab_tone": _top(vocab, 15),
        "signature_moves": _top(moves, 15),
        "forbidden_tells": _top(tells, 10),
    }


def aggregate_beat_stats(beats: list[dict]) -> dict:
    """Aggregate beat cards (``{"chapter_idx", "sequence_id", "beat"}``).

    Derives deterministic stats: 场景类型分布、章内节拍模式 top-N、情感曲线
    转移、高潮间隔(章)、铺垫→回收距离分布、铺垫密度(按章节位置十分位).
    """
    ordered = sorted(beats, key=lambda b: b.get("sequence_id") or 0)

    scene_types: Counter[str] = Counter()
    arcs: Counter[str] = Counter()
    arc_transitions: Counter[str] = Counter()
    chapter_patterns: Counter[str] = Counter()
    per_chapter: dict[int, list[dict]] = {}

    prev_arc = ""
    for b in ordered:
        beat = b.get("beat") or {}
        if not isinstance(beat, dict):
            continue
        st = _norm(beat.get("scene_type"))
        if st:
            scene_types[st[:20]] += 1
        arc = _norm(beat.get("emotional_arc"))
        if arc:
            arcs[arc[:30]] += 1
            if prev_arc:
                arc_transitions[f"{prev_arc[:20]}→{arc[:20]}"] += 1
            prev_arc = arc
        ch = b.get("chapter_idx")
        if isinstance(ch, int):
            per_chapter.setdefault(ch, []).append(beat)

    for blist in per_chapter.values():
        pattern = "→".join(
            _norm(x.get("scene_type"))[:12]
            for x in blist if _norm(x.get("scene_type"))
        )
        if pattern:
            chapter_patterns[pattern] += 1

    # 高潮间隔(章): chapters whose scene_type/turn mentions a climax marker.
    climax_chapters = sorted(
        ch for ch, blist in per_chapter.items()
        if any(
            _CLIMAX_PAT.search(_norm(x.get("scene_type")) + _norm(x.get("turn")))
            for x in blist
        )
    )
    gaps = [b - a for a, b in zip(climax_chapters, climax_chapters[1:])]

    # 铺垫→回收: classify foreshadow strings; payoff keywords win when both
    # appear ("回收伏笔" is a payoff even though it contains 伏笔).
    plants: list[int] = []
    payoffs: list[int] = []
    for ch, blist in per_chapter.items():
        for x in blist:
            fs = _norm(x.get("foreshadow"))
            if not fs:
                continue
            if _PAYOFF_PAT.search(fs):
                payoffs.append(ch)
            elif _PLANT_PAT.search(fs):
                plants.append(ch)

    payoffs_sorted = sorted(payoffs)
    distances = []
    for p in sorted(plants):
        nxt = next((c for c in payoffs_sorted if c >= p), None)
        if nxt is not None:
            distances.append(nxt - p)

    # 铺垫密度 by chapter-position decile.
    density = [0] * 10
    if per_chapter:
        min_ch, max_ch = min(per_chapter), max(per_chapter)
        span = max_ch - min_ch + 1
        for ch in plants:
            density[min(9, (ch - min_ch) * 10 // span)] += 1

    return {
        "beats": len(ordered),
        "chapters": len(per_chapter),
        "scene_type_distribution": _top(scene_types, 12),
        "emotional_arcs": _top(arcs, 10),
        "arc_transitions": _top(arc_transitions, 10),
        "chapter_beat_patterns": _top(chapter_patterns, 10),
        "climax": {
            "chapters_with_climax": len(climax_chapters),
            "gaps_sample": gaps[:20],
            "avg_gap": round(sum(gaps) / len(gaps), 1) if gaps else None,
        },
        "foreshadow": {
            "plants": len(plants),
            "payoffs": len(payoffs),
            "plant_to_payoff_distance_sample": distances[:20],
            "avg_distance": (
                round(sum(distances) / len(distances), 1) if distances else None
            ),
            "plant_density_by_decile": density,
        },
    }


def scrub_proper_nouns(text: str, nouns) -> str:
    """Replace reference-book proper nouns with a neutral placeholder.

    Longest-first so "林晚晴" is scrubbed before "林晚". Single-char names
    are skipped (too collision-prone in Chinese).
    """
    if not text:
        return text
    cleaned = sorted(
        {n.strip() for n in nouns if isinstance(n, str) and len(n.strip()) >= 2},
        key=len,
        reverse=True,
    )
    for noun in cleaned:
        text = text.replace(noun, "某某")
    return text


def _scrub_obj(obj, nouns):
    """Recursively scrub proper nouns from strings inside a JSON-ish object."""
    if isinstance(obj, str):
        return scrub_proper_nouns(obj, nouns)
    if isinstance(obj, list):
        return [_scrub_obj(x, nouns) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_obj(v, nouns) for k, v in obj.items()}
    return obj


# =========================================================================
# Rendering — deterministic, capped injection blocks
# =========================================================================

def _cap(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _fmt_val(val, sep: str = "；", limit: int = 200) -> str:
    if isinstance(val, list):
        s = sep.join(str(x).strip() for x in val if str(x).strip())
    elif isinstance(val, dict):
        s = sep.join(f"{k}:{v}" for k, v in val.items())
    else:
        s = str(val or "").strip()
    return s[:limit]


_STYLE_FIELDS = [
    ("narrative_pov_rules", "视角"),
    ("syntax_rhythm", "句法节奏"),
    ("dialogue_style", "对话"),
    ("sensory_rhetoric", "感官修辞"),
    ("emotional_curve", "情绪基调"),
    ("opening_patterns", "章节开场"),
    ("hook_patterns", "章末钩子"),
]

_PLOT_FIELDS = [
    ("macro_structure", "宏观结构"),
    ("chapter_beat_template", "章节节拍"),
    ("conflict_escalation", "冲突升级"),
    ("foreshadow_strategy", "伏笔管理"),
    ("payoff_rhythm", "爽点节奏"),
]

_WORLD_FIELDS = [
    ("power_system", "力量体系"),
    ("rules_constraints", "规则约束"),
    ("organizations", "组织架构"),
    ("geography", "地理格局"),
    ("conflict_sources", "核心冲突源"),
]


def render_style_block(style_data: dict) -> str:
    profile = (style_data or {}).get("profile") or {}
    if not profile:
        return ""
    lines = ["【风格档案】"]
    for key, label in _STYLE_FIELDS:
        v = _fmt_val(profile.get(key), limit=140)
        if v:
            lines.append(f"{label}：{v}")
    moves = _fmt_val(profile.get("signature_moves"), limit=180)
    if moves:
        lines.append(f"标志性手法：{moves}")
    forbidden = _fmt_val(profile.get("forbidden"), limit=140)
    if forbidden:
        lines.append(f"禁忌：{forbidden}")
    quotes = profile.get("evidence_quotes") or []
    if isinstance(quotes, str):
        quotes = [quotes]
    qs = "；".join(str(q).strip()[:60] for q in quotes[:3] if str(q).strip())
    if qs:
        lines.append(f"例证：{qs}")
    return _cap("\n".join(lines), STYLE_BLOCK_CAP)


def render_structure_block(plot_data: dict) -> str:
    profile = (plot_data or {}).get("profile") or {}
    if not profile:
        return ""
    lines = ["【剧情架构】"]
    for key, label in _PLOT_FIELDS:
        v = _fmt_val(profile.get(key), limit=130)
        if v:
            lines.append(f"{label}：{v}")
    stats = (plot_data or {}).get("stats") or {}
    avg_gap = ((stats.get("climax") or {}).get("avg_gap"))
    if avg_gap:
        lines.append(f"高潮间隔：约{avg_gap}章")
    avg_dist = ((stats.get("foreshadow") or {}).get("avg_distance"))
    if avg_dist:
        lines.append(f"铺垫→回收：平均{avg_dist}章")
    return _cap("\n".join(lines), STRUCTURE_BLOCK_CAP)


def render_world_block(world_data: dict) -> str:
    profile = (world_data or {}).get("profile") or {}
    if not profile:
        return ""
    lines = ["【世界观架构】"]
    for key, label in _WORLD_FIELDS:
        v = _fmt_val(profile.get(key), limit=150)
        if v:
            lines.append(f"{label}：{v}")
    patterns = _fmt_val(profile.get("design_patterns"), limit=200)
    if patterns:
        lines.append(f"设计模式：{patterns}")
    return _cap("\n".join(lines), WORLD_BLOCK_CAP)


# =========================================================================
# LLM plumbing
# =========================================================================

async def _run_registry_structured(
    task_type: str,
    user_content: str,
    db: AsyncSession,
    counter: LLMCallCounter,
) -> dict:
    """Run a registry structured prompt, degrading to the generic
    ``extraction`` prompt when the dedicated prompt exists but has no
    endpoint bound yet (fresh seed). Full output-format instructions live in
    ``user_content``, so the degrade path still produces the right schema.
    """
    counter.tick(task_type)
    try:
        data = await run_structured_prompt(task_type, user_content, db)
    except ValueError as exc:
        logger.warning(
            "task %s unavailable (%s); degrading to 'extraction'",
            task_type, exc,
        )
        counter.tick("extraction")
        data = await run_structured_prompt("extraction", user_content, db)
    if not isinstance(data, dict) or data.get("parse_error"):
        raise RuntimeError(f"{task_type} returned unparsable output")
    return data


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


_STYLE_REDUCE_INSTRUCTIONS = (
    "以下是从一本参考小说的全部风格卡中确定性聚合出的统计数据，以及按全书顺序"
    "分层抽取的代表性卡片（含片段摘录）。请归纳出全书级风格档案。只输出 JSON：\n"
    '{"narrative_pov_rules": "叙事视角与切换规则", '
    '"syntax_rhythm": "句法节奏（长短句比例、段落长度）", '
    '"dialogue_style": "对话风格与密度", '
    '"sensory_rhetoric": "感官与修辞偏好", '
    '"emotional_curve": "情绪基调曲线（全书走向）", '
    '"opening_patterns": "章节开场模式", '
    '"hook_patterns": "章末钩子模式", '
    '"signature_moves": ["标志性手法"], '
    '"forbidden": ["禁忌（反AI腔、该风格应避免的写法）"], '
    '"evidence_quotes": ["从代表性片段摘录的短例证"]}\n'
    "要求：每个字符串字段不超过120字；evidence_quotes 共2-3条、每条不超过60字、"
    "必须出自代表性卡片的片段摘录且不含人名地名等专有名词。\n\n"
)

_PLOT_REDUCE_INSTRUCTIONS = (
    "以下是从一本参考小说的全部情节骨架卡中确定性聚合出的统计数据"
    "（场景类型分布、章内节拍模式、高潮间隔、铺垫→回收距离等），以及分层抽取的"
    "可复用骨架模板。请归纳出全书级剧情架构档案。只输出 JSON：\n"
    '{"macro_structure": "宏观结构（卷/弧形态）", '
    '"chapter_beat_template": "章节节拍模板", '
    '"conflict_escalation": "冲突升级模式", '
    '"foreshadow_strategy": "伏笔管理策略", '
    '"payoff_rhythm": "爽点节奏"}\n'
    "要求：每个字段不超过120字；不得出现书中人名/地名等专有名词，"
    "用「主角」「A势力」等占位符表述。\n\n"
)


# =========================================================================
# Consolidation entry points
# =========================================================================

async def consolidate_style(
    book_id: str,
    db: AsyncSession | None = None,
    counter: LLMCallCounter | None = None,
) -> dict:
    """MAP (deterministic) + REDUCE (1 LLM call) over all style cards."""
    if db is None:
        async with async_session_factory() as session:
            return await consolidate_style(book_id, session, counter)
    counter = counter or LLMCallCounter()

    rows = await db.execute(
        select(
            StyleProfileCard.profile_json,
            StyleProfileCard.slice_id,
            ReferenceBookSlice.sequence_id,
        )
        .join(ReferenceBookSlice, StyleProfileCard.slice_id == ReferenceBookSlice.id)
        .where(StyleProfileCard.book_id == str(book_id))
        .order_by(ReferenceBookSlice.sequence_id.asc())
    )
    cards = [r for r in rows.all() if isinstance(r[0], dict)]
    if not cards:
        raise ValueError("no style cards for book")

    stats = aggregate_style_stats([r[0] for r in cards])
    reps = select_stratified(cards, REPRESENTATIVE_CARDS)

    # Fetch short excerpts only for the ~30 representative slices — never
    # long verbatim passages, never the whole book.
    rep_ids = [r[1] for r in reps]
    excerpt_rows = await db.execute(
        select(ReferenceBookSlice.id, ReferenceBookSlice.raw_text).where(
            ReferenceBookSlice.id.in_(rep_ids)
        )
    )
    excerpts = {r[0]: (r[1] or "")[:EVIDENCE_EXCERPT_CHARS] for r in excerpt_rows.all()}

    rep_payload = []
    for profile, slice_id, seq in reps:
        rep_payload.append({
            "seq": seq,
            "profile": {
                k: profile.get(k)
                for k in (
                    "pov", "sentence_rhythm", "dialogue_style",
                    "sensory_mix", "pacing", "emotional_register",
                )
                if profile.get(k)
            },
            "excerpt": excerpts.get(slice_id, ""),
        })

    user_content = (
        _STYLE_REDUCE_INSTRUCTIONS
        + "【聚合统计】" + _dumps(stats)
        + "\n【代表性卡片】" + _dumps(rep_payload)
    )
    profile = await _run_registry_structured(
        "style_consolidation", user_content, db, counter
    )
    # Enforce evidence-quote caps regardless of what the model did.
    quotes = profile.get("evidence_quotes") or []
    if isinstance(quotes, str):
        quotes = [quotes]
    profile["evidence_quotes"] = [
        str(q).strip()[:60] for q in quotes[:3] if str(q).strip()
    ]
    return {"profile": profile, "stats": stats}


async def consolidate_plot(
    book_id: str,
    db: AsyncSession | None = None,
    counter: LLMCallCounter | None = None,
) -> dict:
    """MAP (deterministic, per-chapter) + REDUCE (1 LLM call) over beat cards."""
    if db is None:
        async with async_session_factory() as session:
            return await consolidate_plot(book_id, session, counter)
    counter = counter or LLMCallCounter()

    rows = await db.execute(
        select(
            BeatSheetCard.beat_json,
            ReferenceBookSlice.chapter_idx,
            ReferenceBookSlice.sequence_id,
        )
        .join(ReferenceBookSlice, BeatSheetCard.slice_id == ReferenceBookSlice.id)
        .where(BeatSheetCard.book_id == str(book_id))
        .order_by(ReferenceBookSlice.sequence_id.asc())
    )
    beats = [
        {"beat": r[0], "chapter_idx": r[1], "sequence_id": r[2]}
        for r in rows.all()
        if isinstance(r[0], dict)
    ]
    if not beats:
        raise ValueError("no beat cards for book")

    stats = aggregate_beat_stats(beats)
    rep_patterns = [
        _norm((b["beat"] or {}).get("reusable_pattern"))
        for b in select_stratified(beats, REPRESENTATIVE_CARDS)
    ]
    rep_patterns = [p[:60] for p in rep_patterns if p]

    # Reuse the coarse plot_structure some books already carry.
    book = await db.get(ReferenceBook, str(book_id))
    plot_structure = (book.metadata_json or {}).get("plot_structure") if book else None
    if not isinstance(plot_structure, dict) or "error" in (plot_structure or {}):
        plot_structure = None

    payload = {"statistics": stats, "reusable_patterns": rep_patterns}
    if plot_structure:
        payload["existing_plot_structure"] = plot_structure
    user_content = _PLOT_REDUCE_INSTRUCTIONS + _dumps(payload)
    profile = await _run_registry_structured(
        "plot_consolidation", user_content, db, counter
    )
    return {"profile": profile, "stats": stats}


# =========================================================================
# Dossier assembly + storage
# =========================================================================

def _write_meta(book: ReferenceBook, **updates) -> dict:
    """metadata_json write discipline: copy the dict + flag_modified.

    Plain in-place mutation of a JSON column silently no-ops with SQLAlchemy;
    mirror reference_ingestor's copy-then-flag pattern.
    """
    meta = dict(book.metadata_json or {})
    meta.update(updates)
    book.metadata_json = meta
    flag_modified(book, "metadata_json")
    return meta


async def _safe_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass


async def _count(db: AsyncSession, model, book_id: str) -> int:
    return (
        await db.scalar(
            select(func.count(model.id)).where(model.book_id == str(book_id))
        )
    ) or 0


async def build_dossier(book_id: str, db: AsyncSession | None = None) -> dict:
    """Run style/plot/world consolidation and persist the dossier.

    Tolerates partial failure: a failed section stores ``{"error": ...}``
    and the others proceed. Idempotent — re-runs overwrite the stored
    dossier. Returns ``{"status", "llm_calls", "dossier"}``.
    """
    if db is None:
        async with async_session_factory() as session:
            return await build_dossier(book_id, session)

    from app.services import worldview_extractor  # local import: worldview imports us

    book = await db.get(ReferenceBook, str(book_id))
    if book is None:
        return {"status": "error", "error": "reference book not found"}

    counter = LLMCallCounter()
    try:
        _write_meta(book, dossier_status={"state": "running", "updated_at": _now_iso()})
        await db.commit()

        async def _section(name: str, coro) -> dict:
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001
                await _safe_rollback(db)
                logger.warning("dossier section %s failed for book %s: %s",
                               name, book_id, exc)
                return {"error": str(exc)[:500]}

        style_data = await _section("style", consolidate_style(book_id, db, counter))
        plot_data = await _section("plot", consolidate_plot(book_id, db, counter))
        world_res = await _section("world", worldview_extractor.extract(book_id, db, counter))

        # Names found during extraction drive the scrub; world profiles are
        # already abstracted, the noun list is only a scrub aid and is NOT
        # stored in the dossier.
        proper_nouns = list(world_res.pop("proper_nouns", []) or [])
        book_obj = await db.get(ReferenceBook, str(book_id))
        scrub_names = set(proper_nouns)
        for extra in ((book_obj.title if book_obj else ""), (book_obj.author if book_obj else "")):
            if extra and len(extra.strip()) >= 2:
                scrub_names.add(extra.strip())

        # Evidence quotes come from raw slice text — scrub them in the data too.
        if "error" not in style_data:
            profile = style_data.get("profile") or {}
            profile["evidence_quotes"] = _scrub_obj(
                profile.get("evidence_quotes") or [], scrub_names
            )

        world_data = world_res
        style_block = scrub_proper_nouns(render_style_block(style_data), scrub_names)
        structure_block = scrub_proper_nouns(render_structure_block(plot_data), scrub_names)
        world_block = scrub_proper_nouns(render_world_block(world_data), scrub_names)

        dossier = {
            "style_block": _cap(style_block, STYLE_BLOCK_CAP),
            "structure_block": _cap(structure_block, STRUCTURE_BLOCK_CAP),
            "world_block": _cap(world_block, WORLD_BLOCK_CAP),
            "style_data": style_data,
            "plot_data": plot_data,
            "world_data": world_data,
            "consolidated_at": _now_iso(),
            "source_counts": {
                "style_cards": await _count(db, StyleProfileCard, book_id),
                "beat_cards": await _count(db, BeatSheetCard, book_id),
                "chunks_sampled": int(world_data.get("chunks_sampled") or 0),
            },
        }

        all_failed = all(
            "error" in section for section in (style_data, plot_data, world_data)
        )
        state = "error" if all_failed else "done"
        book = await db.get(ReferenceBook, str(book_id))
        _write_meta(
            book,
            dossier=dossier,
            dossier_status={
                "state": state,
                "updated_at": _now_iso(),
                "llm_calls": counter.total,
            },
        )
        await db.commit()
        logger.info(
            "dossier built for book %s: state=%s, %d LLM calls (%s)",
            book_id, state, counter.total, dict(counter.by_task),
        )
        return {"status": state, "llm_calls": counter.total, "dossier": dossier}
    except Exception as exc:  # noqa: BLE001
        await _safe_rollback(db)
        logger.exception("dossier build failed for book %s", book_id)
        try:
            book = await db.get(ReferenceBook, str(book_id))
            if book is not None:
                _write_meta(book, dossier_status={
                    "state": "error",
                    "updated_at": _now_iso(),
                    "error": str(exc)[:500],
                })
                await db.commit()
        except Exception:  # noqa: BLE001
            await _safe_rollback(db)
        return {"status": "error", "error": str(exc)}
