"""Whole-book style statistics (F1 / ainovel stylestat).

Adapted from voocel/ainovel-cli's stylestat (design idea; wording is our own).

The single-chapter evaluation window is structurally blind to *book-level*
style ossification: a phrasing that reads fine in any one chapter becomes a
tic when it recurs dozens of times across 200 chapters, and the model's own
pet phrases are not in any static blacklist. This module computes those
patterns deterministically (pure Python, zero LLM, zero hallucination) and
feeds the result both ways:

- generation prompt gets a "mirror of your own high-frequency tics, dampen
  them" block (``render_style_mirror_block``),
- evaluation prompt gets the raw numbers and lets the LLM adjudicate
  (``render_evaluator_stats_block``).

Statistics belong in code (deterministic); judgement belongs to the LLM.

Module import is side-effect free: no DB, no model_router, no LLM. The Celery
task in ``app.tasks.style_tasks`` owns the IO; everything here is pure.
"""

from __future__ import annotations

import re
from collections import Counter

# Han ideographs only -- n-grams never cross punctuation/whitespace.
_HAN_RUN_RE = re.compile(r"[一-鿿]+")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？\n]+")

# --- tic patterns (book-level cliché shapes) --------------------------------
# 矫正句 "不是X而是Y"
_CORRECTIVE_RE = re.compile(r"不是[^。！？\n]{0,20}而是")
# 计时量词 "几息 / 数瞬 / 半炷香" 类 -- aligns with the blueprint time-window vocab.
_TIME_QUANTIFIER_RE = re.compile(r"(?:几|数|半|一|三|十|片刻|须臾)(?:息|瞬|炷香)")
# 明喻 -- reuse anti_ai_checker's simile markers + "像...一样/一般/似的".
_SIMILE_MARKERS = ("如同", "宛如", "恰似", "犹如", "好似", "仿佛", "好像", "宛若", "犹如一道")
_SIMILE_LIKE_RE = re.compile(r"像[^。，！？\n]{1,12}(?:一样|一般|似的)")
# 沉默节拍
_SILENCE_BEAT_RE = re.compile(
    r"沉默(?:了)?(?:片刻|半晌|良久|一瞬|一会儿)"
    r"|(?:空气|四周|房间|周围)[^。\n]{0,6}(?:沉默|安静)(?:了)?(?:下来)?"
)

# Opening-time words: a chapter that starts on one of these has a "time-of-day
# cold open" -- a high book-level rate signals formulaic openings.
_OPENING_TIME_WORDS = (
    "清晨", "黎明", "夜色", "翌日", "第二天", "次日", "此时",
    "黄昏", "深夜", "天刚", "拂晓", "破晓", "入夜", "傍晚",
)

# Function-word skiplist: normal high-frequency Chinese fragments that would
# otherwise dominate the n-gram ranking as noise.
_FUNCTION_WORD_SKIPLIST = frozenset({
    "的时候", "了一下", "什么的", "这个时候", "在这个", "了一口",
    "了一眼", "了起来", "了下来", "了出来", "了过去", "了过来",
    "的样子", "的声音", "的时候，", "也不会", "不知道", "怎么样",
    "这样的", "那样的", "一般的", "似乎是", "仿佛是", "好像是",
})

_TIC_PATTERNS: dict[str, re.Pattern] = {
    "corrective_not_but": _CORRECTIVE_RE,
    "time_quantifier": _TIME_QUANTIFIER_RE,
    "simile_like": _SIMILE_LIKE_RE,
    "silence_beat": _SILENCE_BEAT_RE,
}


def extract_tic_counts(text: str) -> dict[str, int]:
    """Count occurrences of each book-level tic pattern in one text."""
    if not text:
        return {k: 0 for k in _TIC_PATTERNS}
    counts = {key: len(pat.findall(text)) for key, pat in _TIC_PATTERNS.items()}
    # Simile markers are plain substrings (not in the "像...一样" regex).
    counts["simile_marker"] = sum(text.count(m) for m in _SIMILE_MARKERS)
    return counts


def char_ngrams(text: str, n: int) -> list[str]:
    """Character-level n-grams over Han runs only (never crossing punctuation)."""
    if n <= 0 or not text:
        return []
    grams: list[str] = []
    for run in _HAN_RUN_RE.findall(text):
        if len(run) < n:
            continue
        for i in range(len(run) - n + 1):
            grams.append(run[i : i + n])
    return grams


def _contains_any_name(gram: str, names: set[str]) -> bool:
    return any(name and name in gram for name in names)


def top_ngram_phrases(
    texts: list[str],
    stopnames: set[str],
    *,
    threshold: int,
    top_k: int = 8,
    n_min: int = 3,
    n_max: int = 6,
) -> list[dict]:
    """Most over-used 3-6 char phrases across ``texts``.

    - entity names (characters/locations/orgs) are stop-listed: any gram
      *containing* a name is dropped (not just exact matches),
    - function-word fragments are dropped,
    - only grams with count >= ``threshold`` survive,
    - substring dedup: a short gram that is a substring of a higher/equal long
      gram with a close count (<25% diff) is dropped (keep the longer phrase),
    - top ``top_k`` by count returned as ``[{"phrase", "count"}]``.
    """
    counter: Counter[str] = Counter()
    for n in range(n_min, n_max + 1):
        for text in texts:
            counter.update(char_ngrams(text, n))

    # Filter: threshold, names, function words.
    candidates: dict[str, int] = {}
    for gram, count in counter.items():
        if count < threshold:
            continue
        if gram in _FUNCTION_WORD_SKIPLIST:
            continue
        if _contains_any_name(gram, stopnames):
            continue
        candidates[gram] = count

    # Substring dedup: drop short gram contained in a longer one with close count.
    by_len_desc = sorted(candidates, key=lambda g: (-len(g), -candidates[g]))
    dropped: set[str] = set()
    for i, longg in enumerate(by_len_desc):
        if longg in dropped:
            continue
        for shortg in by_len_desc[i + 1 :]:
            if shortg in dropped or len(shortg) >= len(longg):
                continue
            if shortg in longg:
                hi = max(candidates[longg], candidates[shortg])
                if hi and abs(candidates[longg] - candidates[shortg]) / hi < 0.25:
                    dropped.add(shortg)

    survivors = [(g, c) for g, c in candidates.items() if g not in dropped]
    survivors.sort(key=lambda gc: (-gc[1], -len(gc[0]), gc[0]))
    return [{"phrase": g, "count": c} for g, c in survivors[:top_k]]


def repeated_sentences(
    chapters: list[tuple[int, str]],
    *,
    min_chapters: int = 3,
    min_len: int = 10,
    top_k: int = 8,
) -> list[dict]:
    """Verbatim sentences appearing in >= ``min_chapters`` distinct chapters."""
    sentence_chapters: dict[str, set[int]] = {}
    for global_idx, text in chapters:
        if not text:
            continue
        seen_in_chapter: set[str] = set()
        for raw in _SENTENCE_SPLIT_RE.split(text):
            sent = raw.strip().strip('“”"\'』『「」 　')
            if len(sent) < min_len:
                continue
            if sent in seen_in_chapter:
                continue
            seen_in_chapter.add(sent)
            sentence_chapters.setdefault(sent, set()).add(global_idx)

    hits = [
        {"sentence": s, "chapter_count": len(chs)}
        for s, chs in sentence_chapters.items()
        if len(chs) >= min_chapters
    ]
    hits.sort(key=lambda h: (-h["chapter_count"], -len(h["sentence"])))
    return hits[:top_k]


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def ending_shape(chapters: list[tuple[int, str]], *, short_threshold: int = 15) -> dict:
    """Chapter-ending homogeneity: last-sentence length median + short-end rate."""
    last_lens: list[int] = []
    short_end = 0
    counted = 0
    for _idx, text in chapters:
        if not text or not text.strip():
            continue
        sents = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        if not sents:
            continue
        counted += 1
        last = sents[-1]
        last_lens.append(len(last))
        if len(last) <= short_threshold:
            short_end += 1
    return {
        "last_sentence_len_median": _median(last_lens),
        "short_ending_rate": round(short_end / counted, 3) if counted else 0.0,
        "chapters_counted": counted,
    }


def opening_time_rate(chapters: list[tuple[int, str]], *, head_chars: int = 100) -> dict:
    """Fraction of chapters that cold-open on a time-of-day word."""
    counted = 0
    time_open = 0
    for _idx, text in chapters:
        if not text or not text.strip():
            continue
        counted += 1
        head = text[:head_chars]
        if any(w in head for w in _OPENING_TIME_WORDS):
            time_open += 1
    return {
        "opening_time_rate": round(time_open / counted, 3) if counted else 0.0,
        "chapters_counted": counted,
    }


def compute_style_stats(
    chapters: list[tuple[int, str]],
    character_names: set[str],
    *,
    recent_window: int = 20,
) -> dict:
    """Aggregate all book-level style statistics.

    ``chapters`` is ``[(global_chapter_idx, content_text), ...]`` and need not
    be sorted; recency is taken by global index. ``character_names`` should
    include character/location/organization names to stop-list from n-grams.
    """
    chapters = [(i, t) for (i, t) in chapters if t and t.strip()]
    n_chapters = len(chapters)
    if n_chapters == 0:
        return {"chapter_count": 0}

    # Tics: book-wide per-chapter average frequency.
    tic_totals: Counter[str] = Counter()
    for _idx, text in chapters:
        for key, count in extract_tic_counts(text).items():
            tic_totals[key] += count
    tics = {
        key: {
            "total": total,
            "per_chapter": round(total / n_chapters, 3),
        }
        for key, total in tic_totals.items()
    }

    # n-grams: most-recent window only (recent ossification matters most).
    recent = sorted(chapters, key=lambda c: c[0])[-recent_window:]
    recent_texts = [t for _i, t in recent]
    ngram_threshold = max(8, len(recent) // 2)
    phrases = top_ngram_phrases(
        recent_texts, set(character_names), threshold=ngram_threshold
    )

    return {
        "chapter_count": n_chapters,
        "recent_window": len(recent),
        "ngram_threshold": ngram_threshold,
        "tics": tics,
        "top_phrases": phrases,
        "repeated_sentences": repeated_sentences(chapters),
        "ending_shape": ending_shape(chapters),
        "opening": opening_time_rate(chapters),
    }


# --- per-chapter stats + aggregation (incremental recompute, W14) -----------
# A chapter's contribution to every whole-book statistic is captured once in a
# compact per-chapter dict (persisted in chapter_style_stats); the whole-book
# stats are then aggregated from those dicts. Only the recent-window n-grams
# still need raw text -- and only for the window (O(1) chapters), never the
# whole book.


def compute_chapter_style_stats(text: str) -> dict | None:
    """One chapter's style facts, sufficient to aggregate whole-book stats.

    Returns ``None`` for empty/whitespace-only text (the chapter is not
    counted, matching ``compute_style_stats``'s filter). Keys:

    - ``tics``: ``extract_tic_counts(text)``
    - ``sentences``: per-chapter deduped sentences (len >= 10, stripped the
      same way ``repeated_sentences`` does)
    - ``last_sentence_len``: ``ending_shape`` semantics, ``None`` if the text
      has no sentences after splitting
    - ``opening_time``: whether the first 100 chars contain a time-of-day word
    """
    if not text or not text.strip():
        return None
    seen: set[str] = set()
    sentences: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sent = raw.strip().strip('“”"\'』『「」 　')
        if len(sent) < 10 or sent in seen:
            continue
        seen.add(sent)
        sentences.append(sent)
    ending_sents = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return {
        "tics": extract_tic_counts(text),
        "sentences": sentences,
        "last_sentence_len": len(ending_sents[-1]) if ending_sents else None,
        "opening_time": any(w in text[:100] for w in _OPENING_TIME_WORDS),
    }


def aggregate_style_stats(
    per_chapter: list[tuple[int, dict]],
    recent_texts: list[str],
    character_names: set[str],
) -> dict:
    """Aggregate per-chapter rows into the ``compute_style_stats`` shape.

    ``per_chapter`` is ``[(global_idx, compute_chapter_style_stats(text)), ...]``
    for every non-empty chapter. ``recent_texts`` must be the content of the
    most-recent ``recent_window`` non-empty chapters in ascending global-idx
    order (n-grams are window-scoped, so only those texts are needed).

    Given consistent inputs this returns exactly what
    ``compute_style_stats(chapters, character_names)`` returns on the full
    chapter list -- pinned by tests -- so callers can switch between the two
    freely (full rebuild vs incremental aggregate).
    """
    rows = [(i, s) for (i, s) in per_chapter if s]
    n_chapters = len(rows)
    if n_chapters == 0:
        return {"chapter_count": 0}

    tic_totals: Counter[str] = Counter()
    for _idx, s in rows:
        for key, count in (s.get("tics") or {}).items():
            tic_totals[key] += int(count or 0)
    tics = {
        key: {
            "total": total,
            "per_chapter": round(total / n_chapters, 3),
        }
        for key, total in tic_totals.items()
    }

    ngram_threshold = max(8, len(recent_texts) // 2)
    phrases = top_ngram_phrases(
        recent_texts, set(character_names), threshold=ngram_threshold
    )

    # Repeated sentences: per-chapter dedup already applied at compute time.
    sentence_chapters: dict[str, set[int]] = {}
    for idx, s in sorted(rows, key=lambda r: r[0]):
        for sent in s.get("sentences") or []:
            sentence_chapters.setdefault(sent, set()).add(idx)
    hits = [
        {"sentence": sent, "chapter_count": len(chs)}
        for sent, chs in sentence_chapters.items()
        if len(chs) >= 3
    ]
    hits.sort(key=lambda h: (-h["chapter_count"], -len(h["sentence"])))

    last_lens = [
        int(s["last_sentence_len"])
        for _idx, s in rows
        if s.get("last_sentence_len") is not None
    ]
    short_end = sum(1 for length in last_lens if length <= 15)
    ending = {
        "last_sentence_len_median": _median(last_lens),
        "short_ending_rate": round(short_end / len(last_lens), 3) if last_lens else 0.0,
        "chapters_counted": len(last_lens),
    }

    time_open = sum(1 for _idx, s in rows if s.get("opening_time"))
    opening = {
        "opening_time_rate": round(time_open / n_chapters, 3),
        "chapters_counted": n_chapters,
    }

    return {
        "chapter_count": n_chapters,
        "recent_window": len(recent_texts),
        "ngram_threshold": ngram_threshold,
        "tics": tics,
        "top_phrases": phrases,
        "repeated_sentences": hits[:8],
        "ending_shape": ending,
        "opening": opening,
    }


# --- prompt rendering (with budget contracts) -------------------------------


def _pack_lines(lines: list[str], max_chars: int) -> str:
    """Greedily pack lines under a char budget (line order = priority)."""
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > max_chars:
            continue
        out.append(line)
        used += cost
    return "\n".join(out)


def _has_signal(stats: dict) -> bool:
    if not stats or stats.get("chapter_count", 0) == 0:
        return False
    tics = stats.get("tics") or {}
    has_tic = any(v.get("per_chapter", 0) >= 0.5 for v in tics.values())
    return bool(
        has_tic
        or stats.get("top_phrases")
        or stats.get("repeated_sentences")
    )


def render_style_mirror_block(stats: dict, max_chars: int = 800) -> str:
    """Generation-side block: the writer's own high-frequency tics to dampen."""
    if not _has_signal(stats):
        return ""
    lines: list[str] = [
        "【文风镜像（高频口头禅，主动压低）】",
        "以下是你在本书已写章节里反复出现的句式/短语，写作时主动降低其频率：",
    ]
    tics = stats.get("tics") or {}
    tic_bits = [
        f"{key}（章均{v['per_chapter']}次）"
        for key, v in sorted(tics.items(), key=lambda kv: -kv[1].get("per_chapter", 0))
        if v.get("per_chapter", 0) >= 0.5
    ]
    if tic_bits:
        lines.append("- 高频句式：" + "；".join(tic_bits[:5]))
    phrases = stats.get("top_phrases") or []
    if phrases:
        lines.append(
            "- 高频短语：" + "／".join(f"{p['phrase']}({p['count']})" for p in phrases[:8])
        )
    reps = stats.get("repeated_sentences") or []
    if reps:
        sample = reps[0]
        lines.append(
            f"- 跨章重复句（已在{sample['chapter_count']}个章节逐字出现）：{sample['sentence'][:30]}…"
        )
    ending = stats.get("ending_shape") or {}
    if ending.get("short_ending_rate", 0) >= 0.5:
        lines.append(
            f"- 章末同构：{int(ending['short_ending_rate']*100)}% 的章节以短句收尾，"
            "换些有动作或悬念的收束方式。"
        )
    opening = stats.get("opening") or {}
    if opening.get("opening_time_rate", 0) >= 0.4:
        lines.append(
            f"- 开篇套路：{int(opening['opening_time_rate']*100)}% 的章节以时间词开场，避免固定起手式。"
        )
    lines.append("压低频率即可，禁止为规避而生造表达或堆砌生僻词。")
    return _pack_lines(lines, max_chars)


def render_evaluator_stats_block(stats: dict, max_chars: int = 600) -> str:
    """Evaluation-side block: raw numbers for the LLM to adjudicate."""
    if not _has_signal(stats):
        return ""
    lines: list[str] = [
        "## 全书文风统计（参考数字，是否构成问题由你裁定）",
        "统计归代码，下列为确定性测量；本章是否复用全书高频套路，请据此判断：",
    ]
    tics = stats.get("tics") or {}
    tic_bits = [
        f"{key}={v['per_chapter']}/章"
        for key, v in sorted(tics.items(), key=lambda kv: -kv[1].get("per_chapter", 0))
        if v.get("per_chapter", 0) >= 0.5
    ]
    if tic_bits:
        lines.append("- 句式章均频率：" + "，".join(tic_bits[:5]))
    phrases = stats.get("top_phrases") or []
    if phrases:
        lines.append(
            "- 近窗高频短语：" + "，".join(f"{p['phrase']}×{p['count']}" for p in phrases[:8])
        )
    reps = stats.get("repeated_sentences") or []
    if reps:
        lines.append(f"- 跨章逐字重复句：{len(reps)} 句（≥3 章复现）")
    return _pack_lines(lines, max_chars)


# --- dispatch helper (non-blocking, celery-optional) ------------------------


def dispatch_style_recompute(project_id, caller: str, *, full: bool = False) -> bool:
    """Enqueue the style-stats recompute for a project.

    The task is incremental: it only re-reads chapters whose content changed
    since their per-chapter stats row was computed, then refreshes the cheap
    aggregates. Pass ``full=True`` to force a per-chapter rebuild of every
    chapter (manual repair / backfill for pre-existing projects); the rebuild
    is idempotent, so repeated runs converge to the same stats and roster.

    Non-blocking by design: any failure (missing id, broker down, celery
    unconfigured in tests) is swallowed with a WARNING so the user-facing
    chapter-save path never fails. Mirrors entity_dispatch.dispatch_entity_extraction.
    """
    import logging

    _log = logging.getLogger(__name__)
    if not project_id:
        _log.debug("dispatch_style_recompute skipped: missing project_id (caller=%s)", caller)
        return False
    try:
        from app.tasks import celery_app
        from app.tasks.style_tasks import STYLE_STATS_TASK
    except Exception as e:  # pragma: no cover - import failure is exceptional
        _log.warning("dispatch_style_recompute: celery unavailable (caller=%s): %s", caller, e)
        return False
    try:
        celery_app.send_task(
            STYLE_STATS_TASK,
            kwargs={"project_id": str(project_id), "caller": caller, "full": bool(full)},
        )
    except Exception as e:
        _log.warning(
            "dispatch_style_recompute: send_task failed (caller=%s project=%s): %s",
            caller, project_id, e,
        )
        return False
    _log.info("style stats recompute enqueued (caller=%s project=%s)", caller, project_id)
    return True


async def load_style_stats_text(db, project_id, *, max_chars: int = 600) -> str:
    """Load the project's style_stats row and render the evaluator block.

    Fail-safe: returns "" on any error (missing row, DB hiccup) so the
    evaluation path is never blocked. Returns a string with no ORM/session
    dependency, safe to carry out of the loading session.
    """
    import logging

    try:
        from sqlalchemy import select

        from app.models.project import StyleStat

        row = (
            await db.execute(
                select(StyleStat.stats_json).where(StyleStat.project_id == str(project_id))
            )
        ).first()
        if row and row[0]:
            return render_evaluator_stats_block(row[0], max_chars=max_chars)
    except Exception as e:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning("load_style_stats_text failed: %s", e)
    return ""
