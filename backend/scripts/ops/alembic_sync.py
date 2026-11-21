"""数据库迁移记账与 schema 的核对/同步(把"要不要 stamp"变成一条命令)。

## 为什么需要它

本仓库的迁移链**不自带建表**:`alembic upgrade` 假定 schema 已由
`Base.metadata.create_all` 建好。于是在"表已经建好、但 alembic 从没记过账"的库上:

- `alembic upgrade` → `DuplicateTable` / `DuplicateColumnError`;
- 正确动作是 `alembic stamp <head>`(只写记账,不跑 DDL)。

但 `stamp` 用错方向很危险(把没应用的版本标成已应用 → 以后 `upgrade` 跳过那段 DDL,
表结构与记账长期不一致)。所以这里**先核对 head 迁移留下的痕迹,再决定**。

## 用法

    # 只体检,不改任何东西(默认)
    DATABASE_URL=postgresql+asyncpg://... python -m scripts.ops.alembic_sync

    # 判定为"应 stamp"时才真正执行
    DATABASE_URL=... python -m scripts.ops.alembic_sync --apply

判定只有五种,任何一种都会逐字打印理由:

| 判定 | 含义 | 动作 |
|---|---|---|
| `noop` | 记账已是 head 且痕迹齐全 | 什么都不做 |
| `stamp` | 痕迹齐全但记账落后/缺失 | `--apply` 时执行 `alembic stamp head` |
| `upgrade` | 痕迹缺失(真的没应用) | 打印应执行的 `upgrade` 命令,**不代替你跑 DDL** |
| `fix_heads` | 迁移图有多个 head | 拒绝,提示先修分支 |
| `manual` | head 迁移没有登记核对痕迹 | 拒绝自动判断,给人工步骤 |

**本工具永不执行 DDL**(只可能执行 `stamp`);也绝不猜测:没有核对痕迹的迁移一律交给人。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

# head 迁移 → 它留下的可核对痕迹。加新迁移时**必须**在这里补一行,
# 否则新 head 会落到 `manual`(拒绝自动判断),这是有意的安全默认。
SCHEMA_PROBES: dict[str, tuple[tuple[str, str, str], ...]] = {
    # (kind, table, name);kind: column | unique
    # a1001961 只加一列:usage_estimated(该行 token 是字符数估算,不是供应商计量)。
    "a1001961": (("column", "llm_call_logs", "usage_estimated"),),
    "a1001959": (
        ("column", "character_states", "status_digest"),
        ("unique", "character_states", "uq_character_states_source"),
    ),
    # a1001958 建的是 arc 相关表;它之前的 head 也登记一条,便于逐级核对。
    # a1001960 只加一列:cached_input_tokens(缓存命中 token)。
    "a1001960": (("column", "llm_call_logs", "cached_input_tokens"),),
    # a1001958 建的是 arc 相关表;它之前的 head 也登记一条,便于逐级核对。
    "a1001958": (("table", "arc_planning_requests", ""),),
}


@dataclass
class Verdict:
    action: str            # noop | stamp | upgrade | fix_heads | manual
    reason: str
    detail: dict = field(default_factory=dict)

    def command(self, head: str | None) -> str:
        if self.action == "stamp":
            return f"alembic stamp {head}"
        if self.action == "upgrade":
            return f"alembic upgrade {head}"
        return ""


def decide(*, heads: list[str], current: str | None, probes_ok: bool | None) -> Verdict:
    """纯判断:给定迁移图与现状,给出该做什么(不碰数据库)。"""
    if not heads:
        return Verdict("fix_heads", "迁移图里没有任何 head")
    if len(heads) != 1:
        return Verdict("fix_heads", f"迁移图有 {len(heads)} 个 head:{sorted(heads)};先合并分支")
    head = heads[0]
    if probes_ok is None:
        return Verdict("manual", f"head `{head}` 没有登记核对痕迹(SCHEMA_PROBES),拒绝自动判断",
                       {"head": head})
    if current == head and probes_ok:
        return Verdict("noop", f"记账已是 `{head}` 且痕迹齐全", {"head": head})
    if probes_ok:
        return Verdict("stamp", f"schema 已是 `{head}` 的形态,但记账是 `{current or '(无)'}`"
                                 f" → 只补记账,不跑 DDL", {"head": head, "current": current})
    return Verdict("upgrade", f"schema 缺 `{head}` 的痕迹 → 这个迁移**真的没应用**,需要执行 DDL",
                   {"head": head, "current": current})


def _script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config("alembic.ini"))


def read_current(dsn: str) -> str | None:
    """读 alembic_version;表不存在(从未用过 alembic)返回 None。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run() -> str | None:
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as conn:
                exists = (await conn.execute(text(
                    "SELECT to_regclass('alembic_version')"))).scalar()
                if exists is None:
                    return None
                rows = (await conn.execute(text("SELECT version_num FROM alembic_version"))).all()
                return rows[0][0] if rows else None
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def check_probes(dsn: str, revision: str) -> bool | None:
    """核对某迁移的痕迹;该迁移没登记痕迹时返回 None(拒绝判断)。"""
    probes = SCHEMA_PROBES.get(revision)
    if probes is None:
        return None
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run() -> bool:
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as conn:
                for kind, table, name in probes:
                    if kind == "column":
                        found = (await conn.execute(text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = :t AND column_name = :n"), {"t": table, "n": name})).scalar()
                    elif kind == "unique":
                        found = (await conn.execute(text(
                            "SELECT 1 FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) "
                            "AND contype = 'u' AND conname = :n"), {"t": table, "n": name})).scalar()
                    else:  # table
                        found = (await conn.execute(text(
                            "SELECT to_regclass(:t)"), {"t": table})).scalar()
                    if not found:
                        return False
                return True
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _apply_stamp(revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    command.stamp(Config("alembic.ini"), revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None, help="默认读 settings.DATABASE_URL")
    parser.add_argument("--apply", action="store_true", help="判定为 stamp 时真的执行(默认只体检)")
    args = parser.parse_args(argv)

    dsn = args.database_url
    if not dsn:
        from app.config import settings

        dsn = settings.DATABASE_URL
    heads = _script_directory().get_heads()
    head = heads[0] if len(heads) == 1 else None
    current = read_current(dsn)
    probes_ok = check_probes(dsn, head) if head else None
    verdict = decide(heads=heads, current=current, probes_ok=probes_ok)

    print(f"heads={heads} current={current or '(无 alembic_version)'} "
          f"probes_ok={probes_ok}")
    print(f"判定: {verdict.action} —— {verdict.reason}")
    if verdict.action == "stamp" and args.apply:
        _apply_stamp(head)
        print(f"已执行: alembic stamp {head}")
        print(f"复核: current={read_current(dsn)}")
    elif verdict.command(head):
        print(f"建议执行: {verdict.command(head)}")
    return 0 if verdict.action in ("noop", "stamp") else 1


if __name__ == "__main__":
    sys.exit(main())