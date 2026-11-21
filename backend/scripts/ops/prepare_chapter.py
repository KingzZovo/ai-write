#!/usr/bin/env python3
"""验证"第 N 章开写前的最小资产是否齐",缺什么补什么(可复跑)。

最小资产(借鉴 AI-Novel-Writing-Assistant 的首章关键路径最小化):
    story_base / world_slice / core_characters / chapter_route / chapter_contract

本脚本调用 ``app.services.progressive_planning.ensure_minimal_plan_for_chapter``,
它只补到"够写第 N 章":书纲缺才生成书纲,卷纲缺才生成卷纲(且只入库前
``--max-route-chapters`` 章路线),本章合同缺才生成本章合同。远期卷规划不是前置条件。

用法::

    python -m scripts.ops.prepare_chapter \
        --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \
        --project-id <project-uuid> --chapter 3

退出码:0 = ready(五项齐备);1 = 仍有缺口(见打印的 missing / errors)。
"""

from __future__ import annotations

import argparse
import json


async def main_async(args) -> int:
    # 必须在 import app.* 之前把应用配置指向目标库。
    from scripts.quality_eval.continuity_probe import _prepare_env

    _prepare_env(args.database_url)

    from app.db.session import async_session_factory
    from app.services.progressive_planning import ensure_minimal_plan_for_chapter

    async with async_session_factory() as db:
        status = await ensure_minimal_plan_for_chapter(
            db,
            args.project_id,
            args.chapter,
            max_route_chapters=args.max_route_chapters,
        )

    print(json.dumps(status.as_dict(), ensure_ascii=False, indent=2))
    if status.missing:
        print("缺:", "、".join(status.missing))
    else:
        print(f"最小资产齐备:第 {args.chapter} 章可以开写")
    for err in status.errors:
        print("错误:", err)
    return 0 if status.ready else 1


def main(argv=None) -> int:
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--max-route-chapters", type=int, default=5)
    args = ap.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
