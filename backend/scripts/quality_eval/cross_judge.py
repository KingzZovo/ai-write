"""两个评审模型的交叉盲评:同一批章、同一套"结果交付"标准,比对一致率与分歧点。

为什么需要它:目前的"文学/结果"证据来自**一个**评审模型(`deepseek-chat`)。单个评审模型的
结论无法区分"章节确实交付了结果"和"这个评审模型的偏好"。换成两个不同家族的模型各判一遍,
能得到两样东西:

1. **一致率**:两评审都判"交付"的比例 —— 一致性高,说明结论不是某一个模型的偏好;
2. **分歧清单**:逐条列出两评审判得不一样的地方,交给真人只审这些(把人工成本压到最小)。

用法::

    # 先各自判一遍(会调模型),再比对
    python -m scripts.quality_eval.cross_judge --dir <run_dir> --cases <cases.json> \
        --a-model deepseek-chat --b-model claude-sonnet-4-6

    # 已有 judged.*.json 时只比对
    python -m scripts.quality_eval.cross_judge --dir <run_dir> --compare-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.quality_eval.judge import judge_dir, summarize

DEFAULT_A_NAME = "deepseek-chat"
DEFAULT_B_NAME = "claude-sonnet-4-6"


def _records(run_dir: Path, name: str) -> list[dict]:
    path = run_dir / f"judged.{name}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def verdict_map(records: list[dict]) -> dict[tuple[str, str], dict]:
    """``(stem, item_id) -> verdict``,只取判成功的记录。"""
    out: dict[tuple[str, str], dict] = {}
    for record in records:
        if record.get("status") != "ok":
            continue
        for verdict in record.get("verdicts") or []:
            item_id = verdict.get("id")
            if item_id:
                out[(record["stem"], item_id)] = verdict
    return out


def compare(run_dir: str | Path, name_a: str, name_b: str) -> dict:
    """比对两个评审的逐条判定。只统计**两方都判过**的条目,避免把覆盖率差异当分歧。"""
    out = Path(run_dir)
    map_a = verdict_map(_records(out, name_a))
    map_b = verdict_map(_records(out, name_b))
    common = sorted(set(map_a) & set(map_b))
    agree = [key for key in common if bool(map_a[key]["delivered"]) == bool(map_b[key]["delivered"])]
    disagreements = []
    for key in common:
        if bool(map_a[key]["delivered"]) == bool(map_b[key]["delivered"]):
            continue
        disagreements.append({
            "stem": key[0], "item": key[1],
            f"{name_a}_delivered": bool(map_a[key]["delivered"]),
            f"{name_b}_delivered": bool(map_b[key]["delivered"]),
            f"{name_a}_quote": str(map_a[key].get("quote") or "")[:120],
            f"{name_b}_quote": str(map_b[key].get("quote") or "")[:120],
        })
    return {
        "run_dir": str(out),
        "judges": {name_a: summarize(out, name_a) if _records(out, name_a) else None,
                   name_b: summarize(out, name_b) if _records(out, name_b) else None},
        "compared_items": len(common),
        "agreed": len(agree),
        "disagreed": len(disagreements),
        "agreement_rate": round(len(agree) / len(common), 4) if common else None,
        "only_a": sorted({k[0] for k in set(map_a) - set(map_b)}),
        "only_b": sorted({k[0] for k in set(map_b) - set(map_a)}),
        "disagreements": disagreements,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="quality_eval / 探针运行目录")
    parser.add_argument("--cases", default=None, help="样本集 JSON(需要实跑评审时必填)")
    parser.add_argument("--a-name", default=DEFAULT_A_NAME)
    parser.add_argument("--a-model", default=DEFAULT_A_NAME)
    parser.add_argument("--b-name", default=DEFAULT_B_NAME)
    parser.add_argument("--b-model", default=DEFAULT_B_NAME)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8317/v1")
    parser.add_argument("--compare-only", action="store_true",
                        help="不调模型,只比对已有的 judged.*.json")
    parser.add_argument("--out", default=None, help="报告 JSON 输出路径")
    args = parser.parse_args(argv)

    run_dir = Path(args.dir)
    if not args.compare_only:
        if not args.cases:
            raise SystemExit("实跑评审需要 --cases")
        for name, model in ((args.a_name, args.a_model), (args.b_name, args.b_model)):
            # 不能按"文件存在"跳过:judge_dir 自己是**逐篇**续判的(已判成功的篇目跳过),
            # 按文件存在跳过会把"判到一半"的评审当成已完成(实测:评审 B 只判了 2/6 篇就被跳过)。
            print(f"[judge] {name} ({model}) ...", flush=True)
            print(f"[judge] {name} ({model}) ...", flush=True)
            judge_dir(run_dir=run_dir, cases_path=args.cases, name=name,
                      model=model, endpoint=args.endpoint)

    report = compare(run_dir, args.a_name, args.b_name)
    for name, stats in report["judges"].items():
        if stats is None:
            print(f"[{name}] 无判定记录")
            continue
        print(f"[{name}] 篇={stats['judged_documents']} 条目={stats['items']} "
              f"交付率={stats['delivered_rate']} 有据交付率={stats['verified_delivered_rate']}")
    print(f"共同比对条目={report['compared_items']} 一致={report['agreed']} "
          f"分歧={report['disagreed']} 一致率={report['agreement_rate']}")
    for row in report["disagreements"][:10]:
        print(f"  分歧 {row['stem']}#{row['item']}: "
              f"{args.a_name}={row[f'{args.a_name}_delivered']} "
              f"{args.b_name}={row[f'{args.b_name}_delivered']}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())