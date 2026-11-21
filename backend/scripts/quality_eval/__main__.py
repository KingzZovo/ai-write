"""单一命令入口。

    cd backend
    python -m scripts.quality_eval run \
        --cases scripts/quality_eval/cases/sample_set_v1.json \
        --out ../docs/evaluation/quality-eval-YYYYMMDD-<label> \
        --model gemini-3.1-pro \
        --variants scene_writer,chapter_single_shot,bare

    python -m scripts.quality_eval snapshot --rev HEAD --cases <样本集> --out <快照.json>
    python -m scripts.quality_eval judge   --dir <产出目录> --cases <样本集> \
        --name <后缀> --model deepseek-chat          # 独立评审"结果是否交付"
    python -m scripts.quality_eval detect  --dir <产出目录>
    python -m scripts.quality_eval rescore --dir <产出目录> --name <后缀> [--cases 新样本集]
    python -m scripts.quality_eval compare --before <目录A> --after <目录B>

连续章探针(真实写作链,独立入口):
    python -m scripts.quality_eval.continuity_probe --database-url <PG> --out <目录> --chapters 10

退出码:0 表示本次运行本身成功(调用与检测都完成);稿件是否合格由 REVIEW.md 判断,
不由退出码代替。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.quality_eval import judge as judge_mod
from scripts.quality_eval import runner
from scripts.quality_eval import snapshot as snapshot_mod


def _cmd_run(args: argparse.Namespace) -> int:
    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
    summary = runner.run(
        cases_path=args.cases,
        out_dir=args.out,
        variants=variants,
        model=args.model,
        endpoint=args.endpoint,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        api_key=args.api_key,
        repeats=args.repeats,
        retries=args.retries,
        only_case=args.only_case,
        snapshot=snapshot_mod.load(args.snapshot) if args.snapshot else None,
    )
    print(json.dumps({k: summary[k] for k in
                      ("cases", "calls", "returned", "failed", "chars", "counts", "tokens")},
                     ensure_ascii=False, indent=2))
    print(f"artifacts: {Path(args.out).resolve()}")
    return 0


def _cmd_judge(args: argparse.Namespace) -> int:
    result = judge_mod.judge_dir(
        run_dir=args.dir, cases_path=args.cases, name=args.name, model=args.model,
        endpoint=args.endpoint, api_key=args.api_key, only_case=args.only_case)
    summary = judge_mod.summarize(args.dir, args.name)
    print(json.dumps({**result, **summary}, ensure_ascii=False, indent=2))
    return 0


def _cmd_detect(args: argparse.Namespace) -> int:
    result = runner.detect_only(args.dir)


def _cmd_snapshot(args: argparse.Namespace) -> int:
    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
    result = snapshot_mod.capture(args.rev, args.cases, variants, args.out)
    print(json.dumps({"out": args.out, "rev": result["rev"],
                      "sources": result["sources"],
                      "variants": sorted(result["blocks"])}, ensure_ascii=False, indent=2))
    return 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["consistent_with_previous"] is not False else 1


def _cmd_rescore(args: argparse.Namespace) -> int:
    result = runner.rescore(args.dir, args.name, cases_path=args.cases)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _load_summary(directory: str) -> dict:
    path = Path(directory) / "summary.json"
    if not path.exists():
        raise SystemExit(f"not a quality_eval output directory: {directory}")
    return json.loads(path.read_text(encoding="utf-8"))


def _cmd_compare(args: argparse.Namespace) -> int:
    before = _load_summary(args.before)
    after = _load_summary(args.after)

    lines = [
        "# 前后对照(只列数字变化,判断见 REVIEW.md)",
        "",
        f"- before: `{args.before}`",
        f"- after:  `{args.after}`",
        f"- before 契约: {json.dumps(before.get('contract', {}), ensure_ascii=False)}",
        f"- after  契约: {json.dumps(after.get('contract', {}), ensure_ascii=False)}",
        "",
        "| 类别 | before | after | 变化 |",
        "|---|---|---|---|",
    ]
    regressions: list[str] = []
    for name in CATEGORIES:
        b = before["counts"].get(name, 0)
        a = after["counts"].get(name, 0)
        delta = a - b
        lines.append(f"| {name} | {b} | {a} | {delta:+d} |")
        if delta > 0:
            regressions.append(name)

    lines += ["", "| 未兑现合同条目 | before | after | 变化 |", "|---|---|---|---|"]
    for key in sorted(set(before["unmet"]) | set(after["unmet"])):
        b = before["unmet"].get(key, 0)
        a = after["unmet"].get(key, 0)
        lines.append(f"| {key} | {b} | {a} | {a - b:+d} |")
        if a - b > 0:
            regressions.append(key)

    lines += [
        "",
        f"- 字数:before {before['chars']} → after {after['chars']}",
        f"- 失败调用:before {before['failed']} → after {after['failed']}",
        f"- 退步项:{', '.join(regressions) if regressions else '无'}",
        "",
    ]
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    # arm-judge 自带 argparse(参数与这里的子命令风格不同),只做转发,不重复定义。
    passthrough = list(sys.argv[1:] if argv is None else argv)
    if passthrough[:1] == ["arm-judge"]:
        from scripts.quality_eval import arm_judge

        return arm_judge.main(passthrough[1:])
    parser = argparse.ArgumentParser(prog="scripts.quality_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="固定样本集 -> 真实模型 -> 确定性检测")
    run_p.add_argument("--cases", required=True)
    run_p.add_argument("--out", required=True)
    run_p.add_argument("--model", required=True)
    run_p.add_argument("--variants", default="scene_writer")
    run_p.add_argument("--endpoint", default=runner.DEFAULT_ENDPOINT)
    run_p.add_argument("--temperature", type=float, default=0.3)
    run_p.add_argument("--max-tokens", type=int, default=4096)
    run_p.add_argument("--timeout", type=float, default=300.0)
    run_p.add_argument("--api-key", default=None)
    run_p.add_argument("--repeats", type=int, default=1)
    run_p.add_argument("--snapshot", default=None,
                       help="契约快照 JSON;给出时按块替换当前契约,用于改动前后同条件对照")

    snap_p = sub.add_parser("snapshot", help="从某个 git 提交里冻住写作契约文本")
    snap_p.add_argument("--rev", default="HEAD")
    snap_p.add_argument("--cases", required=True)
    snap_p.add_argument("--variants", default="scene_writer,chapter_single_shot")
    snap_p.add_argument("--out", required=True)
    snap_p.set_defaults(func=_cmd_snapshot)
    run_p.add_argument("--retries", type=int, default=2,
                       help="单次调用返回拒答/截断/网关报错时的重试次数")
    run_p.add_argument("--only-case", default=None)
    run_p.set_defaults(func=_cmd_run)

    detect_p = sub.add_parser("detect", help="对已有产出目录重跑检测")
    detect_p.add_argument("--dir", required=True)
    detect_p.set_defaults(func=_cmd_detect)

    judge_p = sub.add_parser("judge", help="用独立模型评审结果是否交付(要求逐字引用)")
    judge_p.add_argument("--dir", required=True)
    judge_p.add_argument("--cases", required=True)
    judge_p.add_argument("--name", required=True, help="产物后缀:judged.<name>.json")
    judge_p.add_argument("--model", default="deepseek-v4.1-flash")
    judge_p.add_argument("--endpoint", default=runner.DEFAULT_ENDPOINT)
    judge_p.add_argument("--api-key", default=None)
    judge_p.add_argument("--only-case", default=None)
    judge_p.set_defaults(func=_cmd_judge)

    rescore_p = sub.add_parser("rescore", help="用当前检测器重新给已有正文打分(不重跑模型)")
    rescore_p.add_argument("--dir", required=True)
    rescore_p.add_argument("--name", required=True,
                           help="产物后缀:detections.<name>.json / summary.<name>.json")
    rescore_p.add_argument("--cases", default=None,
                           help="覆盖样本集路径(默认用产出目录内的 cases.json)")
    rescore_p.set_defaults(func=_cmd_rescore)

    compare_p = sub.add_parser("compare", help="两个产出目录的前后数字对照")
    compare_p.add_argument("--before", required=True)
    compare_p.add_argument("--after", required=True)
    compare_p.add_argument("--out", default=None)
    compare_p.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except runner.QualityEvalError as exc:
        print(f"quality_eval failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())