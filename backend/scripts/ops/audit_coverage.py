"""核对审计覆盖率:`docs/audit/<date>/pN.md` 里声明的"已读文件"是否覆盖 `backend/app` 全部 .py。

约定(写档案时必须遵守):每份 `pN.md` 里有一个二级标题 `## 已读文件`,
紧跟其后是一个围栏代码块,一行一个路径(相对仓库根),例如:

    ## 已读文件
    ```
    backend/app/api/generate.py
    backend/app/services/chapter_quality_gate.py
    ```

用法:
    cd backend && python -m scripts.ops.audit_coverage --dir ../docs/audit/2026-11-17

退出码 0 表示覆盖 100%;否则打印缺失文件清单并返回 1。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_ROOT = os.path.join(REPO_ROOT, "backend", "app")
HEADING = re.compile(r"^##\s*已读文件\s*$", re.MULTILINE)
FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)


def all_app_files() -> set[str]:
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                out.add(os.path.relpath(os.path.join(dirpath, name), REPO_ROOT))
    return out


def declared_files(markdown: str) -> list[str]:
    match = HEADING.search(markdown)
    if not match:
        return []
    fence = FENCE.search(markdown, match.end())
    if not fence:
        return []
    files = []
    for line in fence.group(1).splitlines():
        value = line.strip().strip("`").strip()
        if value and not value.startswith("#"):
            files.append(value)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit_coverage")
    parser.add_argument("--dir", required=True, help="审计档案目录,如 ../docs/audit/2026-11-17")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON")
    args = parser.parse_args(argv)

    audit_dir = os.path.abspath(args.dir)
    if not os.path.isdir(audit_dir):
        raise SystemExit(f"目录不存在:{audit_dir}")

    expected = all_app_files()
    per_partition: dict[str, set[str]] = {}
    claimed: set[str] = set()
    unknown: set[str] = set()
    for name in sorted(os.listdir(audit_dir)):
        if not re.fullmatch(r"p\d+\.md", name):
            continue
        with open(os.path.join(audit_dir, name), encoding="utf-8") as handle:
            files = declared_files(handle.read())
        per_partition[name] = set(files)
        claimed |= set(files)
        unknown |= {f for f in files if f not in expected}

    if not per_partition:
        raise SystemExit(f"{audit_dir} 下没有 pN.md 档案")

    covered = claimed & expected
    missing = sorted(expected - claimed)
    coverage = len(covered) / len(expected) * 100 if expected else 0.0

    summary = {
        "partitions": len(per_partition),
        "expected_files": len(expected),
        "declared_files": len(claimed),
        "covered_files": len(covered),
        "coverage_percent": round(coverage, 2),
        "missing": missing,
        "unknown_paths": sorted(unknown),
        "per_partition": {k: len(v) for k, v in per_partition.items()},
    }
    print(f"分区 {summary['partitions']} 个 · 覆盖 {summary['covered_files']}/{summary['expected_files']} "
          f"= {summary['coverage_percent']}%")
    for name, files in summary["per_partition"].items():
        print(f"  {name}: {files} 个")
    if missing:
        print(f"未覆盖 {len(missing)} 个:")
        for path in missing[:40]:
            print("  -", path)
        if len(missing) > 40:
            print(f"  ... 还有 {len(missing) - 40} 个")
    if unknown:
        print(f"声明里出现 {len(unknown)} 个不存在的路径(疑似笔误):{unknown[:5]}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if not missing else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())