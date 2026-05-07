"""Title quality checker (PR-TITLE-Q1, 2026-05-07).

After volume-outline staged generation produces chapter_summaries, scan every
title for language-habit violations: main-verb subject mismatch, abuse of 2nd
person as a meta-address to the reader, modern/工程 terms, 中文 colon,
placeholder N-章 markers, etc.

When any violations are found, batch-rewrite ONLY the offending titles via a
single LLM call (one round-trip, all violations in one prompt) so we never
ship chapter rows with sloppy titles in the first place.

Goal: replace the manual \"find bad titles in DB and SQL-update\" workflow
with a code-time gate.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Rule sets
# --------------------------------------------------------------------- #

MODERN_TERMS = (
    "流程", "工序", "系统", "数据", "版本", "接口", "SOP",
    "怪癖", "BUG", "bug", "API", "OK",
)

ABSTRACT_EMPTY = (
    "恐惚", "恒惚", "争锄", "虚妄", "混沌", "迷茫", "怍然", "虚无",
)

OBJECT_NOUNS = (
    "钟声", "钟", "灯火", "灯", "债", "山门", "剑", "印", "锁",
    "墙", "门", "塔", "石", "瓷", "纸", "笔", "棺", "鞋",
)

ABSTRACT_VERBS = (
    "认账", "认错", "认人", "悔过", "决断", "判罪", "定罪", "原谅",
    "宽恕", "决心", "明白", "理解", "怀疑", "嫉妬", "怨恨", "懊悔",
    "还债",
)

META_ADDRESS_TOKENS = (
    "写", "应该", "一直", "其实", "早就", "总是", "明明",
    "知道的", "忽略的",
)


def _has_modern_term(t: str) -> bool:
    return any(kw in t for kw in MODERN_TERMS)


def _has_abstract_empty(t: str) -> bool:
    return t.strip() in ABSTRACT_EMPTY


def _has_chinese_colon(t: str) -> bool:
    return "：" in t


def _has_chapter_n(t: str) -> bool:
    return bool(re.search(r"^第[0-9零一二三四五六七八九十百千]+章$", t.strip()))


def _has_pure_digits(t: str) -> bool:
    return bool(re.match(r"^[0-9]+$", t.strip()))


def _looks_like_object_doing_abstract(t: str) -> bool:
    s = t.strip()
    for obj in OBJECT_NOUNS:
        if not s.startswith(obj):
            continue
        rest = s[len(obj):]
        for v in ABSTRACT_VERBS:
            if v in rest:
                return True
    return False


def _looks_like_2p_meta(t: str) -> bool:
    """第二人称 + 对读者解释剧情的标题。这不是禁“你”，只拦允理错误语感。"""
    s = t.strip()
    if not s.startswith("你"):
        return False
    return any(tok in s[1:] for tok in META_ADDRESS_TOKENS)


def _too_long(t: str) -> bool:
    # PR-TITLE-Q1.1 (2026-05-08): widened per user feedback "字数不齐 OK".
    # Hard ceiling guards against runaway prose-as-title (e.g. one-sentence summary
    # accidentally written into title slot), but does NOT enforce a tight 8-char limit.
    chinese_count = sum(1 for c in t.strip() if "\u4e00" <= c <= "\u9fff")
    if chinese_count > 14:
        return True
    return len(t.strip()) > 18


def _too_short(t: str) -> bool:
    return len(t.strip()) < 2


def _violations_for(title: str) -> list[str]:
    if not title or not isinstance(title, str):
        return ["empty"]
    reasons: list[str] = []
    if _has_chapter_n(title):
        reasons.append("placeholder_chapter_n")
    if _has_pure_digits(title):
        reasons.append("pure_digits")
    if _has_chinese_colon(title):
        reasons.append("chinese_colon")
    if _has_modern_term(title):
        reasons.append("modern_term")
    if _has_abstract_empty(title):
        reasons.append("abstract_empty")
    if _looks_like_object_doing_abstract(title):
        reasons.append("object_abstract_verb")
    if _looks_like_2p_meta(title):
        reasons.append("2p_meta_address")
    if _too_long(title):
        reasons.append("too_long")
    if _too_short(title):
        reasons.append("too_short")
    return reasons


def check_titles(chapter_summaries: list[dict]) -> list[dict]:
    out: list[dict] = []
    for cs in chapter_summaries or []:
        if not isinstance(cs, dict):
            continue
        title = (cs.get("title") or "").strip()
        reasons = _violations_for(title)
        if reasons:
            out.append({
                "chapter_idx": cs.get("chapter_idx"),
                "title": title,
                "reasons": reasons,
                "summary": (cs.get("summary") or "").strip(),
                "key_events": cs.get("key_events") or [],
            })
    return out


REWRITE_SYSTEM = """你是中文小说章名修订师。下面给你一批章名+本章摘要。这些章名都被规则检测器判定为「不合普通中文语感」或「不符合作者命名风格」。

你需要为每一章重写一个合格的标题。

【硬规则】
- 物体（钟/灯/债/山门/剑/印 等）不能直接做需要意识的抽象动作（认账/还债/判罪/原谅）。需要物体可配可见动作（如「钟声起」「山门点灯」「剑出鞘」）。
- 第二人称「你」允许出现，但只能作为人物对话引语或视角对象指称（「你别回头」「你欠的债」），不能让标题像在向读者解释剧情（不允许「你把他写得太干净」「你应该知道的那件事」）。
- 不允许：「第N章」/纯数字/重复卷名/纯抽象空词（恐惚 此类）/中文全角冒号「：」/现代化或工程类词汇（流程/系统/SOP 等）/生造不可读单字。
- 字数 2-8 字之间。
- 优先选择：本章主事件的关键具象意象、关键道具、关键人物动作，或章末状态的诗化短句。

输出严格 JSON：
{"rewrites": [{"chapter_idx": 整数, "title": "新标题"}, ...]}

只输出 JSON 本身，不要 markdown 代码块。"""


async def rewrite_titles(
    violations: list[dict],
    *,
    volume_meta: dict | None = None,
    project_id: str | None = None,
) -> dict[int, str]:
    if not violations:
        return {}
    try:
        from app.services.model_router import get_model_router
    except Exception as e:  # noqa: BLE001
        logger.warning("title_quality: cannot import model_router: %s", e)
        return {}

    items_payload = []
    for v in violations:
        items_payload.append({
            "chapter_idx": v.get("chapter_idx"),
            "old_title": v.get("title"),
            "violation_reasons": v.get("reasons", []),
            "summary": v.get("summary", ""),
            "key_events": v.get("key_events") or [],
        })

    vol_block = ""
    if volume_meta:
        try:
            vm = {k: volume_meta.get(k) for k in ("volume_idx", "title", "theme", "core_conflict") if k in volume_meta}
            vol_block = json.dumps(vm, ensure_ascii=False, indent=2)
        except Exception:
            vol_block = ""

    user_prompt = (
        f"【本卷信息】\n{vol_block or '（无）'}\n\n"
        f"【需要重写的章名（共 {len(items_payload)} 条）】\n"
        f"{json.dumps(items_payload, ensure_ascii=False, indent=2)}\n\n"
        "请为每一条输出新的 title。chapter_idx 必须与输入一致。"
    )

    log_meta = {"project_id": project_id, "task_type": "title_rewrite"} if project_id else None
    router = get_model_router()
    try:
        result = await router.generate(
            task_type="outline_volume",
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            _log_meta=log_meta,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("title_quality: rewrite LLM call failed: %s", e)
        return {}

    raw_text = (getattr(result, "text", None) or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)
    try:
        parsed = json.loads(raw_text)
    except Exception as e:  # noqa: BLE001
        logger.warning("title_quality: rewrite JSON parse failed: %s; raw=%s", e, raw_text[:200])
        return {}

    out: dict[int, str] = {}
    for item in (parsed.get("rewrites") or []):
        if not isinstance(item, dict):
            continue
        try:
            cidx = int(item.get("chapter_idx"))
        except (TypeError, ValueError):
            continue
        new_title = (item.get("title") or "").strip()
        if not new_title:
            continue
        if _violations_for(new_title):
            logger.info(
                "title_quality: rewrite still violates rules, skip idx=%s new=%r",
                cidx, new_title,
            )
            continue
        out[cidx] = new_title
    return out


async def check_and_rewrite_in_place(
    chapter_summaries: list[dict],
    *,
    volume_meta: dict | None = None,
    project_id: str | None = None,
) -> dict:
    violations = check_titles(chapter_summaries)
    stats: dict[str, Any] = {
        "checked": len(chapter_summaries or []),
        "violations": len(violations),
        "rewritten": 0,
        "kept": 0,
    }
    if not violations:
        return stats
    rewrites = await rewrite_titles(
        violations, volume_meta=volume_meta, project_id=project_id,
    )
    if not rewrites:
        stats["kept"] = len(violations)
        return stats
    for cs in chapter_summaries:
        if not isinstance(cs, dict):
            continue
        cidx = cs.get("chapter_idx")
        try:
            cidx_int = int(cidx)
        except (TypeError, ValueError):
            continue
        if cidx_int in rewrites:
            old = cs.get("title")
            cs["title"] = rewrites[cidx_int]
            stats["rewritten"] += 1
            logger.info("title_quality: rewrote idx=%s old=%r new=%r", cidx_int, old, cs["title"])
    stats["kept"] = stats["violations"] - stats["rewritten"]
    return stats
