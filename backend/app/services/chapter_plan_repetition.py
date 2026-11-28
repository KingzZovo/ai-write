"""Volume-plan repetition detector (2026-11-28 全书质量检查).

问题来源:《白蜡树下》卷 5 的卷纲 ``chapter_summaries`` 从第 33 章起开始**循环
重规划同一个高潮** —— 第 33 章与第 52 章都是"解扣派首领撕开假封印…",第 34 章与
第 43 章都是"灰袍老人亲临门卫室…";100 章里 57 个标题是重复的。生成的正文因此
也成片重复(标题重复 238 章、相邻章 8-gram Jaccard 最高 0.97、章内句级模板循环
最多 33 次)。这是**规划层**的缺陷:章节计划没有推进,而是在原地打转。

为什么可以在这里设硬闸,而在正文层不能:文风契约允许正文"结合语境"的重复
(递进、强调、有意复沓),所以正文层只做证据注入(见 ``repetition_profile``);
但**同一卷里两章的计划完全相同**没有任何叙事解释力,那只是生成侧打转。

本模块是纯函数:输入 ``chapter_summaries`` 列表,输出可判定的重复证据。体积小、
无依赖,可离线测试。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

# 摘要相似度阈值。实测卷 5 的循环块是"同一情节轻微改写",8-gram Jaccard 落在
# 0.45~0.70;真正不同的两章通常在 0.10 以下。取 0.45 留出余量。
NEAR_DUPLICATE_SIMILARITY = 0.45
NGRAM = 8
MIN_SUMMARY_CHARS = 40


def _normalize(text: str) -> str:
    return re.sub(r"[\s\W]+", "", text or "")


def _grams(text: str) -> set[str]:
    s = _normalize(text)
    return {s[i:i + NGRAM] for i in range(len(s) - NGRAM + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def plan_repetition(entries: list[dict] | None) -> dict:
    """Count repeated titles and near-duplicate chapter summaries in one plan.

    Returns a dict with ``duplicate_titles`` (title -> [chapter_idx, ...] for
    titles used more than once) and ``near_duplicate_pairs`` (list of
    ``(chapter_idx_a, chapter_idx_b, similarity)``), plus ``worst_similarity``.
    """
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    by_title: dict[str, list] = defaultdict(list)
    for e in entries:
        title = (e.get("title") or "").strip()
        if title:
            by_title[title].append(e.get("chapter_idx"))
    duplicate_titles = {
        title: idxs for title, idxs in by_title.items() if len(idxs) > 1
    }

    grams = []
    for e in entries:
        summary = (e.get("summary") or "").strip()
        grams.append(_grams(summary) if len(summary) >= MIN_SUMMARY_CHARS else set())
    pairs = []
    worst = 0.0
    for i in range(len(entries)):
        if not grams[i]:
            continue
        for j in range(i + 1, len(entries)):
            if not grams[j]:
                continue
            sim = _jaccard(grams[i], grams[j])
            if sim > worst:
                worst = sim
            if sim >= NEAR_DUPLICATE_SIMILARITY:
                pairs.append((entries[i].get("chapter_idx"), entries[j].get("chapter_idx"), round(sim, 3)))
    return {
        "checked": len(entries),
        "duplicate_titles": duplicate_titles,
        "near_duplicate_pairs": pairs,
        "worst_similarity": round(worst, 3),
    }


def describe_violation(report: dict, *, index_span: str = "", titles: bool = True) -> str | None:
    """Render a rejection message, or None when the plan is clean.

    ``titles=False`` limits this to plan drift (near-duplicate summaries). A
    duplicate title on its own is a title-quality problem, handled by
    ``title_quality_checker``'s batch rewrite, not a reason to reject a volume.
    """
    if not report:
        return None
    scope = f"({index_span})" if index_span else ""
    duplicate_titles = (report.get("duplicate_titles") or {}) if titles else {}
    pairs = report.get("near_duplicate_pairs") or []
    if not duplicate_titles and not pairs:
        return None
    bits = []
    if duplicate_titles:
        sample = sorted(duplicate_titles.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:3]
        bits.append("标题重复: " + "; ".join(
            f"「{t}」x{len(idxs)}(章 {idxs[:6]})" for t, idxs in sample))
    if pairs:
        pairs_sorted = sorted(pairs, key=lambda p: -p[2])[:3]
        bits.append("章节摘要近似重复: " + "; ".join(

            f"第{a}章≈第{b}章 J={sim}" for a, b, sim in pairs_sorted))
    return (f"卷纲计划在打转{scope} —— " + " | ".join(bits)
            + "。每一章的计划必须推进冲突或改变处境,不能重规划同一情节;"
              "请重新规划该批次。")

def duplicate_title_violations(entries: list[dict] | None) -> list[dict]:
    """把"同名章"转成标题修订器能吃的违规项(只报第 2 次及之后的出现)。

    重复标题本身不该判整卷不合格 —— 实测卷 3 的 20 组重复标题配的是**互相不同**
    的计划(只是标题被复用),那是标题质量问题,走 `title_quality_checker` 的批量
    重写即可;真正该判不合格的是"计划在打转"(见 ``plan_repetition`` 的近似对)。
    """
    seen: dict[str, object] = {}
    out: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        if title in seen:
            out.append({
                "chapter_idx": entry.get("chapter_idx"),
                "title": title,
                "reasons": ["duplicate_title"],
                "summary": (entry.get("summary") or "").strip(),
                "key_events": entry.get("key_events") or [],
            })
        else:
            seen[title] = entry.get("chapter_idx")
    return out
