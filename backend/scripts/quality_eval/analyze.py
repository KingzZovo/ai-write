"""把一次连续章运行的产物摊平,便于两臂逐章对比与人工核验。

用法::

    python -m scripts.quality_eval.analyze ../docs/evaluation/<run_dir>

只读产物,不调用模型,也不改任何文件。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十百千万]{2,})\s*(元|块钱|块)")
_CHAPTER_FILE = re.compile(r"^ch(\d+)\.txt$")


def _load_chapters(run_dir: Path) -> dict[int, str]:
    chapters: dict[int, str] = {}
    for path in sorted(run_dir.glob("ch*.txt")):
        match = _CHAPTER_FILE.match(path.name)
        if match and path.stat().st_size:
            chapters[int(match.group(1))] = path.read_text(encoding="utf-8")
    return chapters


def analyze(run_dir: Path) -> dict:
    chapters = _load_chapters(run_dir)
    continuity_path = run_dir / "continuity.json"
    continuity = (json.loads(continuity_path.read_text(encoding="utf-8"))
                  if continuity_path.exists() else {})
    counts = continuity.get("word_counts") or {}
    ratios = []
    for index, text in chapters.items():
        entry = counts.get(f"ch{index:02d}.txt") or {}
        ratios.append(entry.get("ratio") or round(len(text) / 1200, 3))
    ratios.sort()
    middle = (ratios[len(ratios) // 2] if len(ratios) % 2 else
              (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2) if ratios else 0.0
    findings = continuity.get("findings") or []
    by_check: dict[str, list] = {}
    for item in findings:
        by_check.setdefault(item["check"], []).append(item)
    return {
        "chapters": len(chapters),
        "median_ratio": round(middle, 3),
        "at_least_80_percent": sum(1 for r in ratios if r >= 0.8),
        "findings_total": len(findings),
        "findings_by_check": {k: [i.get("chapter") for i in v] for k, v in sorted(by_check.items())},
        "texts": chapters,
    }


def _money_lines(text: str) -> list[str]:
    hits = []
    for match in _AMOUNT_RE.finditer(text):
        hits.append(text[max(0, match.start() - 12): match.end()].replace("\n", ""))
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--money", action="store_true", help="逐章列出金额出现处")
    args = parser.parse_args()
    report = analyze(args.run_dir)
    texts = report.pop("texts")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.money:
        print("--- money mentions ---")
        for index in sorted(texts):
            print(f"ch{index:02d}: " + " | ".join(_money_lines(texts[index])))


if __name__ == "__main__":
    main()
