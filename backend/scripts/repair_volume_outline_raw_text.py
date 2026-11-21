"""PR-OUTLINE-DEEPDIVE Phase 3 · 历史分卷大纲 raw_text 修复脚本。

背景
----
用户 2026-05-04 反馈某些分卷大纲的 ``content_json.raw_text`` 出现了
结构被携平的误序列化例：

  { "volume_idx": 1, "title": "外滩怀表", …,
    "title": "雨夜外滩", "summary": …, "key_events": [...] }

（顶层 title 与 chapter_summaries[0].title 被同时平铺。）

本脚本扫描所有 outlines.level='volume'，按以下规则诊断并修复：

  - 如 ``content_json.raw_text`` 为空，跳过。
  - 尝试 json.loads(raw_text)。成功且与 ``content_json`` 顶层几个
    关键字段一致则跳过（干净）。
  - 如果解析失败 · 或 顶层同名键出现多次，表示 JSON 被携平。
  - 修复策略：打 ``\"corrupted_raw_text\": true`` 标记，
    并从 content_json (PG 存储层) 重新生成一份干净 raw_text。
  - 未以 ``--apply`` 运行时仅 dry-run 报告。

使用
----
  python3 backend/scripts/repair_volume_outline_raw_text.py            # dry-run
  python3 backend/scripts/repair_volume_outline_raw_text.py --apply    # 实际写入
  python3 backend/scripts/repair_volume_outline_raw_text.py --project=<uuid>  # 限定项目

需在 backend 容器内运行。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

from sqlalchemy import select

sys.path.insert(0, "/app")

from app.db.session import async_session_factory  # noqa: E402
from app.models.project import Outline  # noqa: E402


DUP_KEY_PATTERN = re.compile(r'"title"\s*:[^,}]+,(?:[^\n]*"title"\s*:)', re.M)


def _is_corrupted(raw_text: str) -> tuple[bool, str]:
    """Return (corrupted, reason)."""
    if not raw_text:
        return (False, "empty")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        # Sometimes raw_text is not strict JSON but a python-repr-like
        # string. Treat as corrupted.
        return (True, f"json_decode_error: {exc}")
    if not isinstance(parsed, dict):
        return (True, f"not_dict: type={type(parsed).__name__}")
    # Heuristic: top-level should have volume_idx / chapter_summaries
    has_vidx = "volume_idx" in parsed
    cs = parsed.get("chapter_summaries")
    has_cs = isinstance(cs, list)
    if has_vidx and has_cs:
        return (False, "clean")
    # Detect flattened: appears two `"title":` at top level but not nested
    if DUP_KEY_PATTERN.search(raw_text):
        return (True, "duplicate_top_level_title")
    return (True, f"missing_keys vidx={has_vidx} cs={has_cs}")


def _rebuild_raw_text(content_json: dict[str, Any]) -> str:
    """Re-render raw_text from content_json minus raw_text itself."""
    body = {k: v for k, v in (content_json or {}).items() if k != "raw_text"}
    return json.dumps(body, ensure_ascii=False, indent=2)


async def main(*, apply: bool, project_filter: str | None) -> int:
    fixed = 0
    skipped_clean = 0
    skipped_no_cj = 0
    flagged_unfixable = 0
    async with async_session_factory() as db:
        q = select(Outline).where(Outline.level == "volume")
        if project_filter:
            from uuid import UUID
            q = q.where(Outline.project_id == UUID(project_filter))
        rows = (await db.execute(q)).scalars().all()
        print(f"扫描 {len(rows)} 个卷级 outline")
        for o in rows:
            cj = o.content_json or {}
            if not isinstance(cj, dict) or not cj:
                skipped_no_cj += 1
                continue
            raw = cj.get("raw_text") or ""
            corrupted, reason = _is_corrupted(raw if isinstance(raw, str) else "")
            if not corrupted:
                skipped_clean += 1
                continue
            print(f"  · outline {o.id} project={o.project_id} · {reason}")
            # 如 content_json 除 raw_text 外仍有可用结构，才能重建
            has_structure = (
                "volume_idx" in cj
                and isinstance(cj.get("chapter_summaries"), list)
                and len(cj.get("chapter_summaries") or []) > 0
            )
            if not has_structure:
                flagged_unfixable += 1
                print("     · 不可修复：inner content_json 仍不含可用结构")
                continue
            new_raw = _rebuild_raw_text(cj)
            if apply:
                cj_new = dict(cj)
                cj_new["raw_text"] = new_raw
                cj_new["_repaired"] = True
                cj_new["_repair_reason"] = reason
                o.content_json = cj_new
                fixed += 1
            else:
                fixed += 1
        if apply:
            await db.commit()
    print(
        f"总结：fixed={fixed} clean={skipped_clean} no_cj={skipped_no_cj} "
        f"unfixable={flagged_unfixable} apply={apply}"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写入 PG")
    parser.add_argument("--project", default=None, help="仅扫某项目")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, project_filter=args.project)))
