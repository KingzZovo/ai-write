"""长跑曲线:把 100 章的逐章产出摊成一条可看的曲线,回答"越写越飘吗"。

只看三件事,不掺任何主观判断:

1. **篇幅比**:每章字符数 / 章节目标。看的是**趋势**而不是单章好坏 ——
   若后三分之一明显低于前三分之一,就是长期衰减;
2. **状态**:`ok` / `resumed` / `error` 的分布,以及出错章节的编号;
3. **终态提取**:每章实体投影与认知提取的 `status`,用来区分"章写成了"和"章写成但台账没跟上"。

用法::

    python -m scripts.quality_eval.longrun --dir <run_dir> [--target 1200]
"""
from __future__ import annotations


def detect_target(run_dir: str | Path) -> int | None:
    """推断本章目标字数,避免默认 1200 把 2000 档的跑批算成"超额 1.59 倍"。

    优先用 ``continuity.json`` 里 word_counts 记录的 target(探针自己写的,权威);
    其次从目录名里的 "-2000-" 这类片段推断;都没有就返回 None 由调用方决定。
    """
    path = Path(run_dir) / "continuity.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        counts = data.get("word_counts") if isinstance(data, dict) else None
        if isinstance(counts, dict):
            targets = {
                int(item["target"]) for item in counts.values()
                if isinstance(item, dict) and item.get("target")
            }
            if len(targets) == 1:
                return targets.pop()
    match = re.search(r"-(\d{3,5})-", Path(run_dir).name)
    return int(match.group(1)) if match else None

import argparse
import json
import json
import re
import sys
from pathlib import Path


def load(run_dir: str | Path) -> list[dict]:
    path = Path(run_dir) / "chapters.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in data if isinstance(row, dict)]


def _chars(row: dict) -> int:
    try:
        return int(row.get("chars") or 0)
    except (TypeError, ValueError):
        return 0


def _idx(row: dict) -> int:
    try:
        return int(row.get("index") or 0)
    except (TypeError, ValueError):
        return 0


def _terminal(row: dict) -> dict:
    """从 extraction 字段里取实体/认知的终态状态。字段可能是 dict 或 repr 字符串。"""
    raw = row.get("extraction")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw.replace("'", '"'))
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ("entity", "cognition"):
        part = raw.get(key)
        if isinstance(part, dict) and part.get("status"):
            out[key] = str(part["status"])
    return out


def curve(rows: list[dict], target: int) -> list[dict]:
    return [{"index": _idx(row), "status": str(row.get("status") or ""),
             "chars": _chars(row),
             "ratio": round(_chars(row) / target, 3) if target else None,
             "terminal": _terminal(row)}
            for row in sorted(rows, key=_idx)]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return (ordered[middle] if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2)


def summarize(rows: list[dict], target: int) -> dict:
    points = curve(rows, target)
    ratios = [p["ratio"] for p in points if p["ratio"] is not None]
    third = max(1, len(ratios) // 3)
    statuses: dict[str, int] = {}
    for point in points:
        statuses[point["status"]] = statuses.get(point["status"], 0) + 1
    terminals: dict[str, int] = {}
    for point in points:
        for key, value in point["terminal"].items():
            terminals[f"{key}:{value}"] = terminals.get(f"{key}:{value}", 0) + 1
    return {
        "chapters": len(points),
        "target": target,
        "median_ratio": round(_median(ratios), 3) if ratios else None,
        "first_third_median": round(_median(ratios[:third]), 3) if ratios else None,
        "last_third_median": round(_median(ratios[-third:]), 3) if ratios else None,
        "below_70pct": [p["index"] for p in points if p["ratio"] is not None and p["ratio"] < 0.7],
        "statuses": statuses,
        "terminals": terminals,
        "failed": [p["index"] for p in points if p["status"] not in ("ok", "resumed", "resumed_extract")],
    }


def _spark(values: list[float]) -> str:
    """把篇幅比画成一行符号:低于 0.7 用 '.',0.7–0.85 用 '-',0.85–1.0 用 '=',高于 1.0 用 '+'。"""
    out = []
    for value in values:
        out.append("." if value < 0.7 else "-" if value < 0.85 else "=" if value <= 1.0 else "+")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    parser.add_argument(
        "--target", type=int, default=None,
        help="本章目标字数。缺省时从 continuity.json 的 word_counts(权威)或目录名推断,"
             "都不行才落到 1200。",
    )
    parser.add_argument("--out", default=None, help="汇总 JSON 输出路径")
    args = parser.parse_args(argv)

    rows = load(args.dir)
    if not rows:
        print("没有 chapters.json 或内容为空")
        return 1
    target = args.target or detect_target(args.dir)
    if not target:
        print("⚠ 未能推断本章目标字数,按 1200 计算;请用 --target 显式指定。")
        target = 1200
    summary = summarize(rows, target)
    points = curve(rows, target)
    print(f"章节 {summary['chapters']} / 目标 {summary['target']} 字")
    print(f"中位比 {summary['median_ratio']} | 前 1/3 {summary['first_third_median']} | "
          f"后 1/3 {summary['last_third_median']}")
    print(f"状态 {json.dumps(summary['statuses'], ensure_ascii=False)}")
    print(f"终态 {json.dumps(summary['terminals'], ensure_ascii=False)}")
    if summary["failed"]:
        print(f"非 ok 章节: {summary['failed']}")
    if summary["below_70pct"]:
        print(f"低于目标 70% 的章节: {summary['below_70pct']}")
    print("--- 篇幅比曲线(每字符=一章)---")
    print(_spark([p["ratio"] or 0 for p in points]))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "curve": points},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())