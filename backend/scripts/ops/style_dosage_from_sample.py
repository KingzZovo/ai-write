#!/usr/bin/env python3
"""从参考书样本算出"剂量画像",派生风格规则写回 StyleProfile(纯本地,不调模型)。

为什么需要这一步:`reference_ingestor` 蒸馏出的是**切片级卡片**(`style_profile_cards`),
而写作链注入文笔靠的是 `StyleProfile.rules_json`(经 `style_compiler.compile_style` 编译成
style_text)。两者之间**没有任何一步回流** —— 结果是"跑完蒸馏,风格卡里 0 条规则,
写作时文笔照旧是模型默认"。本脚本补上这一环。

剂量画像 schema 对齐 `dosage_to_rules.derive_rules_from_dosage` 的期望(见该模块 docstring),
统计口径全部是**确定性**的:同样的输入永远得到同样的规则,便于回归与复核。

用法::

    python -m scripts.ops.style_dosage_from_sample \\
      --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \\
      --file /root/ai-write/data/reference/_style_sample.txt \\
      --profile <style_profile_id 或名称> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# 中文句末标点(含省略号与引号收尾)
_SENT_SPLIT = re.compile(r"[。!?!?…]+")
# 引号一律用 unicode 转义写死:中文小说用 U+201C/U+201D,而源码里的弯引号
# 极易在复制/工具链里被规范化成直引号,导致"对话占比恒为 0"这种静默错误。
_QUOTE_OPEN = "\u201c\u300c\u300e"   # “ 「 『
_QUOTE_CLOSE = "\u201d\u300d\u300f"  # ” 」 』
_DIALOGUE_CHARS = _QUOTE_OPEN + _QUOTE_CLOSE + '"' + "'"
_METAPHOR = re.compile(r"(像|仿佛|宛如|如同|好似|似的|一般|犹如)")
_PSYCHOLOGY = re.compile(r"(心里|想着|觉得|知道|记得|明白|意识到|琢磨|盘算|恍惚|怔)")
_COLLOQUIAL = re.compile(r"[啊呀吧呢嘛诶嘿唉哦哟咯啦嘞]")
_ONO = re.compile(r"(轰|哗|啪|咚|哐|砰|咕|滴答|簌簌|嗡)")
_PARALLEL_START = re.compile(r"^(.{1,4})")
# AI/套话元词:用来生成 anti-ai 规则(命中越多,越要提醒模型避开)
_AI_METAWORDS = (
    "璀璨", "油然而生", "不禁", "莫名的", "这一刻", "仿佛整个世界", "空气仿佛凝固",
    "心中五味杂陈", "不由得", "顿时", "瞬间", "深深的", "微微的", "缓缓地",
    "嘴角勾起", "意味深长", "一丝不苟", "不置可否", "若有所思",
)


def _pct(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = min(len(ordered) - 1, max(0, int(len(ordered) * q)))
    return float(ordered[pos])


def _std(values: list[int], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return var ** 0.5


def compute_dosage(text: str) -> dict:
    """样本文本 → 剂量画像(确定性统计)。"""
    stripped = re.sub(r"\s", "", text)
    total_chars = len(stripped)
    kchars = max(total_chars / 1000.0, 1e-9)

    sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    sent_lens = [len(re.sub(r"\s", "", s)) for s in sentences]
    sent_mean = sum(sent_lens) / len(sent_lens) if sent_lens else 0.0

    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    para_lens = [len(re.sub(r"\s", "", p)) for p in paragraphs]
    para_mean = sum(para_lens) / len(para_lens) if para_lens else 0.0

    dialogue_sents = [s for s in sentences if any(ch in s for ch in _DIALOGUE_CHARS)]
    dialogue_chars = sum(len(re.sub(r"\s", "", s)) for s in dialogue_sents)
    turn_lengths = [len(re.sub(r"\s", "", s)) for s in dialogue_sents]
    dialogues: list[str] = []
    for para in paragraphs:
        dialogues.extend(re.findall(
            "[" + re.escape(_QUOTE_OPEN) + r"]([^" + re.escape(_DIALOGUE_CHARS) + r"]{1,120})", para))

    def per_k(pattern: re.Pattern[str]) -> float:
        return round(len(pattern.findall(text)) / kchars, 3)

    parallel_starts: dict[str, int] = {}
    window: list[str] = []
    for sentence in sentences:
        match = _PARALLEL_START.match(sentence.strip())
        head = match.group(1) if match else ""
        window.append(head)
        if len(window) > 3:
            window.pop(0)
        if len(window) == 3 and len(set(window)) == 1 and head:
            parallel_starts[head] = parallel_starts.get(head, 0) + 1

    metawords = {word: text.count(word) for word in _AI_METAWORDS if text.count(word) > 0}

    return {
        "sentence": {"count": len(sentences), "mean_chars": round(sent_mean, 2),
                     "std_chars": round(_std(sent_lens, sent_mean), 2),
                     "p90_chars": _pct(sent_lens, 0.9)},
        "paragraph": {"count": len(paragraphs), "mean_chars": round(para_mean, 2),
                      "std_chars": round(_std(para_lens, para_mean), 2)},
        "dialogue": {"ratio": round(dialogue_chars / max(total_chars, 1), 4),
                     "per_kchar": round(len(dialogue_sents) / kchars, 3),
                     "turn_count": len(dialogues),
                     "turn_chars_mean": round(sum(turn_lengths) / len(turn_lengths), 2) if turn_lengths else 0.0},
        "metaphor": {"total_per_kchar": per_k(_METAPHOR), "sentence_end_per_kchar": 0.0,
                     "patterns_per_kchar": {}},
        "psychology": {"pattern_total_per_kchar": per_k(_PSYCHOLOGY), "neutral_words_per_kchar": 0.0,
                       "pattern_per_chapter_7k": 0.0, "patterns_per_kchar": {}},
        "parallelism": {"XYX_per_kchar": round(sum(parallel_starts.values()) / kchars, 3),
                        "ABAB_per_kchar": 0.0},
        "colloquial": {"particles_per_kchar": per_k(_COLLOQUIAL), "onomatopoeia_per_kchar": per_k(_ONO)},
        "total_chars": total_chars,
        "total_kchars": round(kchars, 3),
        "ai_metawords_count": metawords,
    }


def _patterns(text: str, regex: re.Pattern[str], limit: int = 8) -> dict:
    counts: dict[str, int] = {}
    for hit in regex.findall(text):
        counts[hit] = counts.get(hit, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    kchars = max(len(re.sub(r"\s", "", text)) / 1000.0, 1e-9)
    return {word: round(count / kchars, 3) for word, count in top}


def enrich(dosage: dict, text: str) -> dict:
    """补上按"具体词"统计的分布(规则里会用到)。"""
    dosage["metaphor"]["patterns_per_kchar"] = _patterns(text, _METAPHOR)
    dosage["psychology"]["patterns_per_kchar"] = _patterns(text, _PSYCHOLOGY)
    return dosage


async def apply_to_profile(db, profile_ref: str, dosage: dict, rules: list[dict],
                           anti_ai: list[dict], *, dry_run: bool) -> dict:
    from sqlalchemy import select
    from uuid import UUID

    from app.models.project import StyleProfile

    profile = None
    try:
        profile = await db.get(StyleProfile, str(UUID(profile_ref)))
    except Exception:  # noqa: BLE001 - 不是 UUID 就按名称查
        profile = None
    if profile is None:
        profile = (await db.execute(
            select(StyleProfile).where(StyleProfile.name == profile_ref))).scalars().first()
    if profile is None:
        raise SystemExit(f"找不到风格卡:{profile_ref}")

    summary = {"profile": profile.name, "profile_id": str(profile.id),
               "rules": len(rules), "anti_ai": len(anti_ai),
               "dry_run": dry_run}
    if dry_run:
        summary["sample_rules"] = [r["rule"] for r in rules[:6]]
        return summary

    config = dict(profile.config_json or {})
    config["dosage_profile"] = dosage
    profile.config_json = config
    profile.rules_json = rules
    if anti_ai:
        merged = list(profile.anti_ai_rules or [])
        known = {item.get("pattern") for item in merged if isinstance(item, dict)}
        merged.extend(item for item in anti_ai if item.get("pattern") not in known)
        profile.anti_ai_rules = merged
    if not (profile.tone_keywords or []):
        profile.tone_keywords = ["短句推进", "对话驱动", "冷峻"]
    await db.commit()
    summary["sample_rules"] = [r["rule"] for r in rules[:6]]
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="参考书样本文本")
    parser.add_argument("--profile", required=True, help="风格卡 id 或名称")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的规则,不改库")
    parser.add_argument("--out", default=None, help="把剂量画像写到这个 JSON(可选)")
    args = parser.parse_args(argv)

    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    from scripts.quality_eval.continuity_probe import _prepare_env
    if args.database_url:
        _prepare_env(args.database_url)

    dosage = enrich(compute_dosage(text), text)
    if args.out:
        Path(args.out).write_text(json.dumps(dosage, ensure_ascii=False, indent=2), encoding="utf-8")

    from app.services.dosage_to_rules import derive_rules_from_dosage
    rules, anti_ai = derive_rules_from_dosage(dosage, profile_version="ref-sample-v1")
    print(json.dumps({"dosage_summary": {
        "total_chars": dosage["total_chars"],
        "sentence_mean": dosage["sentence"]["mean_chars"],
        "paragraph_mean": dosage["paragraph"]["mean_chars"],
        "dialogue_ratio": dosage["dialogue"]["ratio"],
        "metaphor_per_kchar": dosage["metaphor"]["total_per_kchar"],
        "colloquial_per_kchar": dosage["colloquial"]["particles_per_kchar"],
        "ai_metawords": len(dosage["ai_metawords_count"]),
    }, "rules": len(rules), "anti_ai": len(anti_ai)}, ensure_ascii=False))

    if args.database_url:
        import asyncio
        from app.db.session import async_session_factory

        async def run():
            async with async_session_factory() as db:
                return await apply_to_profile(db, args.profile, dosage, rules, anti_ai,
                                              dry_run=args.dry_run)
        print(json.dumps(asyncio.run(run()), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
