#!/usr/bin/env python3
"""
PR-OL? 数据修补脚本 (dry-run)

动机：PR-OL6 在 prompt + entity_tasks bulk insert 加了 “与最近一条
   status_json 相同则 SKIP”。但旧项目中已经存在的 “承前重复” 记录
   还在 character_states 表里。该脚本扫描一个 project 的
   character_states 表，报告 “相邻两条 status_json byte-equal” 的 group。

默认 dry-run。如要真删，传入 --execute。
使用:
   docker exec -w /app ai-write-backend-1 \
     python scripts/cleanup_character_states_dup.py --project-id 0eaeff87-... [--execute]
“相邻” 的定义：同一 character，按 chapter_start ASC，两条中间没有其他不同 status。
"""
import asyncio
import argparse
import sys
from uuid import UUID
from sqlalchemy import select
from app.db.session import async_session_maker
from app.models.project import CharacterState, Character

async def main(project_id: str, execute: bool) -> int:
    async with async_session_maker() as db:
        # 拉取 project 的所有 character_states，按 character_id, chapter_start
        q = await db.execute(
            select(CharacterState).where(
                CharacterState.project_id == UUID(project_id),
            ).order_by(CharacterState.character_id, CharacterState.chapter_start)
        )
        rows = list(q.scalars().all())
        print(f"总 character_states: {len(rows)}")

        # 为报告拼个名字
        char_q = await db.execute(
            select(Character).where(Character.project_id == UUID(project_id))
        )
        char_by_id = {str(c.id): c.name for c in char_q.scalars().all()}

        # 按 character_id 分组，与上一条比较
        prev_status: dict[str, str] = {}
        prev_id: dict[str, str] = {}
        dups: list[tuple[str, str, str, str]] = []  # (char_id, prev_id, this_id, status)
        for r in rows:
            cid = str(r.character_id)
            cur = (r.status_json or "").strip() if isinstance(r.status_json, str) else str(r.status_json or "").strip()
            if cid in prev_status and prev_status[cid] == cur:
                dups.append((cid, prev_id[cid], str(r.id), cur[:80]))
            prev_status[cid] = cur
            prev_id[cid] = str(r.id)

        print(f"相邻 byte-equal 重复: {len(dups)} 条")
        for cid, prev_state_id, this_state_id, snippet in dups[:30]:
            print(f"  char={char_by_id.get(cid, cid)[:20]:20s} | prev={prev_state_id[:8]} this={this_state_id[:8]} | {snippet}")
        if len(dups) > 30:
            print(f"  ... 以及其他 {len(dups)-30} 条")

        if not execute:
            print("\n[dry-run] 未执行删除。加 --execute 真删。")
            return 0

        # 执行删除: 删 “this_state_id” (保留先出现的一条)
        from sqlalchemy import delete
        ids_to_del = [UUID(d[2]) for d in dups]
        if not ids_to_del:
            print("无需删除。")
            return 0
        result = await db.execute(
            delete(CharacterState).where(CharacterState.id.in_(ids_to_del))
        )
        await db.commit()
        print(f"已删除 {result.rowcount} 条。")
        return 0

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--project-id", required=True)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.project_id, args.execute)))
