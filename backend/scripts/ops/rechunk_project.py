"""重建一个项目的章级切片(chapter_chunks)。

什么时候必须跑:

- **切块算法变更**(本仓 2026-11-17 从"按字数凑段落"改为结构感知:对白不切断、
  场景分隔优先)。边界变了,旧 point 的 ``chunk_seq`` / ``text`` 与新切法对不上。
  虽然 ``vector_provenance._chunk_vector_matches`` 已经能容忍"正文未改、只是边界
  重排"的旧点(靠哈希 + 原文子串),重建仍然值得做 —— 旧点会把两场戏粘在一个
  片段里,重建后才拿到干净的叙事单位。
- `CHUNK_TARGET_CHARS` / `CHUNK_MAX_CHARS` 被调过。
- 项目历史上关过 `CHAPTER_CHUNKING_ENABLED`,现在要补齐。

只读章节正文、只写向量库;幂等(point id 由 `{volume_id}_{chapter_idx}_{seq}` 决定,
重跑覆盖同 id、多余尾部按过滤删除)。

用法:

    DATABASE_URL=postgresql+asyncpg://... \
    python -m scripts.ops.rechunk_project --project-id <uuid> [--dry-run]

`--dry-run` 只统计会处理多少章,不连向量库。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rechunk_project",
        description="重建项目的 chapter_chunks 切片(切块算法变更后必跑)",
    )
    parser.add_argument("--project-id", required=True, help="要重建的项目 UUID")
    parser.add_argument("--dry-run", action="store_true", help="只统计章节数,不写向量库")
    parser.add_argument("--database-url", default=None, help="默认读 settings.DATABASE_URL")
    return parser.parse_args(argv)


async def run(project_id: str, *, dry_run: bool = False) -> dict:
    from app.services.chapter_chunker import backfill_project_chunks

    if dry_run:
        from sqlalchemy import func, select

        from app.db.session import async_session_factory
        from app.models.project import Chapter, Volume

        async with async_session_factory() as db:
            total = (
                await db.execute(
                    select(func.count(Chapter.id))
                    .join(Volume, Chapter.volume_id == Volume.id)
                    .where(Volume.project_id == str(project_id))
                )
            ).scalar_one()
        return {"status": "dry_run", "project_id": str(project_id), "chapters": int(total or 0)}

    return await backfill_project_chunks(project_id)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    try:
        result = asyncio.run(run(args.project_id, dry_run=args.dry_run))
    except Exception as exc:  # noqa: BLE001 - 运维命令:报错要可读
        print(f"失败:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())