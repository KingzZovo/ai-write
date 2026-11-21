"""把多个连续章运行目录摊成一张对照表,用于 A/B 比较。

用法::

    python -m scripts.quality_eval.compare_arms <label>=<dir> [<label>=<dir> ...] [--amounts]

默认只读产物、不调用模型。加 ``--amounts`` 时用模型抽取金额,输出每臂的**漂移条数**:
本章写出的总额与后一章对不上的条数(按 (名目, 数额) 去重)。正则做不到这一步 ——
单价与总额同属一个名目。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from scripts.quality_eval.continuity_probe import check

_MONEY = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十百千万]{2,})\s*(元|块钱|块)")


def _chapters(run_dir: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for path in sorted(run_dir.glob("ch*.txt")):
        match = re.fullmatch(r"ch(\d+)\.txt", path.name)
        if match and path.stat().st_size:
            out[int(match.group(1))] = path.read_text(encoding="utf-8")
    return out


def _scene_counts(run_dir: Path) -> dict[int, int]:
    path = run_dir / "chapters.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {int(row["index"]): len((row.get("plan") or {}).get("scenes") or []) for row in rows}


def summarize(label: str, run_dir: Path, target: int) -> dict:
    texts = _chapters(run_dir)
    lengths = {i: len(t) for i, t in texts.items()}
    ratios = sorted(v / target for v in lengths.values())
    middle = ((ratios[len(ratios) // 2] if len(ratios) % 2 else
               (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2)
              if ratios else 0.0)
    report = check(run_dir)
    counts: dict[str, int] = {}
    for item in report["findings"]:
        counts[item["check"]] = counts.get(item["check"], 0) + 1
    return {
        "label": label,
        "dir": str(run_dir),
        "chapters": len(texts),
        "scenes": _scene_counts(run_dir),
        "lengths": lengths,
        "median_ratio": round(middle, 3),
        "ge80": sum(1 for r in ratios if r >= 0.8),
        "ge85": sum(1 for r in ratios if r >= 0.85),
        "findings": counts,
        "texts": texts,
    }


def _money_lines(text: str, limit: int = 8) -> list[str]:
    return [m.group(0) for m in list(_MONEY.finditer(text))[:limit]]


def _report_drift(rows: list[dict], chapters: list[int], args) -> None:
    """对每一臂:抽取指定章金额,报"本章总额与后一章对不上"的条数。"""
    from openai import OpenAI

    from app.services.numeric_ledger import drift_against, looks_like_total
    from scripts.quality_eval.amounts import extract_amounts

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("CPA_API_KEY")
    client = OpenAI(base_url=args.endpoint, api_key=api_key or "unset")
    pairs = list(zip(chapters, chapters[1:])) or [(chapters[0], chapters[0] + 1)]
    print(f"\n--- 金额漂移(--amounts,模型 {args.model};对比 {pairs})---")
    for row in rows:
        drift = 0
        for first, later in pairs:
            head = row["texts"].get(first, "")
            tail = row["texts"].get(later, "")
            if not head or not tail:
                print(f"[{row['label']}] ch{first:02d}/ch{later:02d}: (缺正文,未测)")
                continue
            rows_a, raw_a = extract_amounts(client, model=args.model, text=head,
                                            max_tokens=args.max_tokens, retries=args.retries)
            rows_b, raw_b = extract_amounts(client, model=args.model, text=tail,
                                            max_tokens=args.max_tokens, retries=args.retries)
            if rows_a is None or rows_b is None:
                print(f"[{row['label']}] ch{first:02d}/ch{later:02d}: 抽取失败,未测"
                      f" (前={raw_a[:40]!r} 后={raw_b[:40]!r})")
                continue
            found = drift_against(rows_a, rows_b)
            # 按 (名目, 数额) 去重:同一数额在同章出现多次只算一条漂移,
            # 否则"漂移条数"会被重复出现的次数灌水(实测 baseline 同一 840 出现 3 次)。
            distinct = sorted({(r["item"], r["written"]) for r in found})
            drift += len(distinct)
            totals_a = [r["amount"] for r in rows_a if looks_like_total(r["item"])]
            totals_b = [r["amount"] for r in rows_b if looks_like_total(r["item"])]
            detail = ", ".join("%s=%s" % pair for pair in distinct)
            print(f"[{row['label']}] ch{first:02d} 总额={totals_a} "
                  f"ch{later:02d} 总额={totals_b} 冲突={len(distinct)} {detail}")
        print(f"[{row['label']}] 漂移条数={drift}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arms", nargs="+", help="label=dir")
    parser.add_argument("--target", type=int, default=1200)
    parser.add_argument("--money-chapters", default="7,8")
    parser.add_argument("--amounts", action="store_true",
                        help="用模型抽取金额并输出每臂漂移条数(默认关闭:会调用模型)")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8317/v1")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    rows = []
    for spec in args.arms:
        label, _, path = spec.partition("=")
        if not path:
            raise SystemExit(f"bad arm spec: {spec}")
        rows.append(summarize(label, Path(path), args.target))

    print(f"{'臂':<10}{'章':>4}{'场/章':>8}{'中位比':>8}{'≥80%':>7}{'≥85%':>7}  检查项")
    for row in rows:
        scenes = row["scenes"]
        scene_shape = (f"{min(scenes.values())}-{max(scenes.values())}"
                       if scenes else "?")
        print(f"{row['label']:<10}{row['chapters']:>4}{scene_shape:>8}"
              f"{row['median_ratio']:>8.2f}{row['ge80']:>7}{row['ge85']:>7}  "
              f"{json.dumps(row['findings'], ensure_ascii=False)}")

    wanted = [int(x) for x in args.money_chapters.split(",") if x.strip()]
    for chapter in wanted:
        print(f"\n--- 第 {chapter} 章的金额(人工核验总额是否一致)---")
        for row in rows:
            text = row["texts"].get(chapter, "")
            if not text:
                print(f"[{row['label']}] (缺)")
                continue
            print(f"[{row['label']}] {len(text)} 字: {' | '.join(_money_lines(text))}")

    if args.amounts:
        _report_drift(rows, wanted, args)


if __name__ == "__main__":
    sys.exit(main())