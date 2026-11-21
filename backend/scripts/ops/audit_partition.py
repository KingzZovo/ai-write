"""把 `backend/app` 切成 N 个审计分区(默认 8),保证**互不重叠且并集=全部**。

用法:
    cd backend && python -m scripts.ops.audit_partition --out ../docs/audit/2026-11-17

产出:
    <out>/partitions/p1.txt ... pN.txt   每份是相对仓库根的路径清单(一行一个)
    <out>/partitions/index.json          分区 -> 文件数/字节数,以及总数

分区规则(确定性,与执行顺序无关):
    1) 先按目录聚合:`api` / `services` / `models` / `tasks` / 其余基础设施;
    2) `services` 太大(近 280 个文件),再按文件名前缀归到若干"主题"组;
    3) 各主题组按文件数从大到小、轮流塞进当前文件数最少的分区(贪心均衡)。
这样同一个文件永远落在同一个分区,跑两次结果一致。
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_ROOT = os.path.join(REPO_ROOT, "backend", "app")

# services 下按前缀归主题;顺序即匹配优先级(先长前缀优先,避免 "character_" 吃掉 "character_state_")
SERVICE_TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("write", ("chapter_generator", "chapter_quality_gate", "chapter_acceptance",
               "chapter_persist", "chapter_outline", "chapter_chunker", "chapter_summarizer",
               "scene_", "prose_", "chinese_prose", "writing_guides", "style_", "humanizer",
               "polish", "auto_revise", "revision")),
    ("cognition", ("character_cognition", "character_context", "character_state", "cognition_",
                   "entity_", "relationship_", "fact_contract", "narrative_contract",
                   "narrative_compass", "world_rule", "knowledge_")),
    ("retrieval", ("qdrant_", "vector_", "embedding", "memory", "context_pack", "rag_",
                   "retrieval", "lexical_", "semantic_", "chunk", "recall")),
    ("runtime", ("model_router", "llm_", "prompt_", "provider_", "budget", "cost", "usage_",
                 "generation_", "run_", "task_", "celery", "queue", "rate_limit")),
    ("domain", ("foreshadow_", "numeric_", "money_", "outline_", "volume_", "arc_", "planning_",
                "plot_", "compass_", "project_", "chapter_order", "incremental_sync",
                "export_", "import_", "decompile", "reference_", "evaluation_")),
]


def _top_level_group(rel: str) -> str:
    parts = rel.split(os.sep)
    if len(parts) == 1:
        return "root"                      # app/*.py(config/main/__init__)
    return parts[0]                        # api / services / models / tasks / db / ...


def _service_topic(name: str) -> str:
    for topic, prefixes in SERVICE_TOPICS:
        if any(name.startswith(prefix) for prefix in prefixes):
            return topic
    return "misc"


def collect_files(app_root: str = APP_ROOT) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(app_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, REPO_ROOT))
    return sorted(out)


def group_files(rels: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for rel in rels:
        app_rel = os.path.relpath(os.path.join(REPO_ROOT, rel), APP_ROOT)
        top = _top_level_group(app_rel)
        if top == "services":
            groups[f"services:{_service_topic(os.path.basename(app_rel))}"].append(rel)
        else:
            groups[top].append(rel)
    return dict(groups)


def _split_group(files: list[str], max_files: int) -> list[list[str]]:
    """把过大的组按路径顺序切成若干片,保证单片不超过 max_files。"""
    files = sorted(files)
    return [files[i:i + max_files] for i in range(0, len(files), max_files)] or [[]]


def partition(rels: list[str], count: int, *, max_per_partition: int = 48) -> list[list[str]]:
    groups = group_files(rels)
    units: list[list[str]] = []
    for name in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        units.extend(_split_group(groups[name], max_per_partition))
    buckets: list[list[str]] = [[] for _ in range(count)]
    # 大片先放,每次丢进当前文件数最少的桶 —— 贪心均衡,结果确定
    for unit in sorted(units, key=lambda u: (-len(u), u[0] if u else "")):
        target = min(range(count), key=lambda i: (len(buckets[i]), i))
        buckets[target].extend(unit)
    return [sorted(b) for b in buckets]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit_partition")
    parser.add_argument("--out", required=True, help="审计档案目录,如 ../docs/audit/2026-11-17")
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args(argv)

    rels = collect_files()
    if not rels:
        raise SystemExit("app 目录下没有 .py 文件")
    buckets = partition(rels, max(1, args.count))

    out_dir = os.path.abspath(args.out)
    pdir = os.path.join(out_dir, "partitions")
    os.makedirs(pdir, exist_ok=True)
    index = {"total_files": len(rels), "partitions": []}
    for i, bucket in enumerate(buckets, start=1):
        path = os.path.join(pdir, f"p{i}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(bucket) + ("\n" if bucket else ""))
        size = sum(os.path.getsize(os.path.join(REPO_ROOT, rel)) for rel in bucket)
        index["partitions"].append({"id": f"p{i}", "files": len(bucket), "bytes": size,
                                    "list": os.path.relpath(path, REPO_ROOT)})
    with open(os.path.join(pdir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, indent=2)

    covered = sum(p["files"] for p in index["partitions"])
    print(json.dumps({"total_files": covered, "partitions": index["partitions"]},
                     ensure_ascii=False, indent=2))
    assert covered == len(rels), "分区必须无遗漏"
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())