"""项目级 / 章级 LLM 用量聚合(a1001960)。

作者要看见"这一章花了多少 token、缓存命中多少",所以用量必须能从
`llm_call_logs` 直接算出来,而不是只有 Prometheus 计数器。

两条硬约束:

1. **聚合下推到 SQL**:一律 `select(func.count(), func.sum(...)).group_by(...)`,
   只选 token 列和分组键。一张月的 `llm_call_logs` 就有几百万行、正文与
   `messages_json` 都在表里 —— 把行读进内存再 `sum()` 会在生产上直接打爆内存,
   也因此本模块绝不 select `response_text` / `messages_json`。
2. **口径**:`cached_input_tokens` 是 `input_tokens` 的**子集**(命中缓存的
   输入 token,计价更低)。总 token = `input_tokens + output_tokens`,
   **不再加**一遍缓存数;`cache_hit_rate = cached_input_tokens / input_tokens`,
   分母为 0 时返回 0.0。

本模块同时提供纯函数(`build_summary_from_rows` / `build_budget_from_rows`),
离线用例用它们核对聚合口径与标签,不必起真实 PG。

a1001961:供应商**完全没报** usage 的行由 model_router 按字符数估算并置
`usage_estimated`。估算值写的就是 `input_tokens` / `output_tokens` 本身,所以
总量口径不变(`total = input + output`,不重复加)。为了不让估算冒充真实计量,
汇总另外报两个数:`estimated_calls`(估算行条数)与 `estimated_tokens`
(估算行贡献的 input+output)。这两个数是**同一批行的另一种切分**,不是额外
桶 —— 把它们加进 `total_tokens` 就是重复计数。
"""

from sqlalchemy import Select, case, func, select

from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select

from app.models.call_log import LLMCallLog

#: 合法分组维度。`chapter` 是默认值(作者最常看的"每章花多少")。
GROUP_BY_CHOICES: tuple[str, ...] = ("chapter", "task_type", "model", "day")

DEFAULT_GROUP_BY = "chapter"
DEFAULT_DAYS = 30
MIN_DAYS = 1
MAX_DAYS = 365

#: 没有章身份的调用(规划、评审、抽取等)归到这一组,而不是丢掉。
UNASSIGNED_KEY = "unassigned"
UNASSIGNED_LABEL = "未归章"

DAY_FORMAT = "%Y-%m-%d"

#: 章节 id(字符串)→ 展示用章号。由调用方注入,便于离线用例替身。
ChapterNumberLookup = Callable[[Sequence[str]], Awaitable[dict[str, int]]]


def normalize_group_by(group_by: str | None) -> str:
    """校验分组维度;非法值抛 ``ValueError``(API 层翻成 400)。"""
    if group_by is None or group_by == "":
        return DEFAULT_GROUP_BY
    if group_by not in GROUP_BY_CHOICES:
        raise ValueError(
            f"unsupported group_by {group_by!r}; expected one of {list(GROUP_BY_CHOICES)}"
        )
    return group_by


def clamp_days(days: int | None) -> int:
    """把窗口天数夹进 1..365(默认 30)。越界不报错,只夹住。"""
    if days is None:
        return DEFAULT_DAYS
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, value))


def cache_hit_rate(cached_input_tokens: int | None, input_tokens: int | None) -> float:
    """缓存命中率 = 命中数 / 输入数。输入为 0 时是 0.0,不是 NaN。"""
    try:
        numerator = int(cached_input_tokens or 0)
        denominator = int(input_tokens or 0)
    except (TypeError, ValueError):
        return 0.0
    if denominator <= 0:
        return 0.0
    # 2026-11-17 审计:中转有时把 cached_tokens 报得比 prompt_tokens 还大,
    # 不夹住就会在 /usage 上显示 >100% 的命中率。
    return round(min(max(numerator, 0), denominator) / denominator, 6)


# ---------------------------------------------------------------------------
# SQL 语句构造:只选 token 列 + 分组键
# ---------------------------------------------------------------------------


def _group_key_expression(group_by: str, dialect: str = "postgresql"):
    """分组键表达式。

    `day` 必须按 **UTC** 切日:`created_at` 是 timestamptz,直接 CAST 成 date
    会跟会话 TimeZone 走,换个连接就可能跨日错位。PostgreSQL 用
    `date_trunc('day', timezone('UTC', created_at))` 显式换算;SQLite(sync
    用例替身,时间本就是 UTC)用 `strftime`。
    """
    if group_by == "chapter":
        return LLMCallLog.chapter_id
    if group_by == "task_type":
        return LLMCallLog.task_type
    if group_by == "model":
        return LLMCallLog.model
    if group_by == "day":
        if dialect == "sqlite":
            return func.strftime(DAY_FORMAT, LLMCallLog.created_at)
        return func.date_trunc("day", func.timezone("UTC", LLMCallLog.created_at))
    raise ValueError(f"unsupported group_by {group_by!r}")


def _token_columns():
    """聚合列:行数 + 三个 token 求和。

    `coalesce` 让空组返回 0 而不是 NULL;`func.count()` 用 ``*``(行数),
    与 `status` 无关 —— 失败的调用同样产生了 token 消耗,照样计数。
    """
    return (
        func.count().label("calls"),
        func.coalesce(func.sum(LLMCallLog.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(LLMCallLog.cached_input_tokens), 0).label("cached_input_tokens"),
        func.coalesce(func.sum(LLMCallLog.output_tokens), 0).label("output_tokens"),
    )


def _estimated_columns():
    """估算列:估算行条数 + 估算行贡献的 input+output。

    `usage_estimated` 是布尔;这里用 `case` 而不是 `FILTER (WHERE ...)`,因为
    SQLite(离线用例的替身)老版本不支持 FILTER。
    """
    estimated = LLMCallLog.usage_estimated.is_(True)
    row_total = (
        func.coalesce(LLMCallLog.input_tokens, 0)
        + func.coalesce(LLMCallLog.output_tokens, 0)
    )
    return (
        func.coalesce(func.sum(case((estimated, 1), else_=0)), 0).label("estimated_calls"),
        func.coalesce(func.sum(case((estimated, row_total), else_=0)), 0).label(
            "estimated_tokens"
        ),
    )


def estimated_summary_statement(
    *,
    project_id: Any,
    group_by: str = DEFAULT_GROUP_BY,
    since: datetime | None = None,
    chapter_id: Any = None,
    dialect: str = "postgresql",
) -> Select:
    """构造"哪些分组里有估算行"的聚合语句(与 `project_summary_statement` 同维度)。

    刻意与主语句**分开**:主语句的选列形状(calls/input/cached/output)是既有
    契约,前端与冻结用例都按它核对;估算统计只是同一批行的另一视角,用一条
    带同样 where/group by 的轻查询补上,依旧不读正文与消息。
    """
    group_by = normalize_group_by(group_by)
    key = _group_key_expression(group_by, dialect)
    stmt = (
        select(key.label("group_key"), *_estimated_columns())
        .where(LLMCallLog.project_id == project_id)
        .group_by(key)
    )
    if since is not None:
        stmt = stmt.where(LLMCallLog.created_at >= since)
    if chapter_id is not None:
        stmt = stmt.where(LLMCallLog.chapter_id == chapter_id)
    return stmt


def project_summary_statement(
    *,
    project_id: Any,
    group_by: str = DEFAULT_GROUP_BY,
    since: datetime | None = None,
    chapter_id: Any = None,
    dialect: str = "postgresql",
) -> Select:
    """构造项目用量聚合语句(尚未执行)。

    选中的列只有 ``分组键 + calls/input/cached/output``;没有任何大字段。
    """
    group_by = normalize_group_by(group_by)
    key = _group_key_expression(group_by, dialect)
    stmt = (
        select(key.label("group_key"), *_token_columns())
        .where(LLMCallLog.project_id == project_id)
        .group_by(key)
    )
    if since is not None:
        stmt = stmt.where(LLMCallLog.created_at >= since)
    if chapter_id is not None:
        stmt = stmt.where(LLMCallLog.chapter_id == chapter_id)
    return stmt


def chapter_budget_statement(*, chapter_id: Any, since: datetime | None = None) -> Select:
    """构造单章用量语句:按 task_type 分组,同样只选 token 列。"""
    stmt = (
        select(LLMCallLog.task_type.label("group_key"), *_token_columns())
        .where(LLMCallLog.chapter_id == chapter_id)
        .group_by(LLMCallLog.task_type)
    )
    if since is not None:
        stmt = stmt.where(LLMCallLog.created_at >= since)
    return stmt


# ---------------------------------------------------------------------------
# 行 → 载荷(纯函数,离线可测)
# ---------------------------------------------------------------------------


def _row_values(row: Any) -> tuple[Any, int, int, int, int]:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        values = (
            mapping["group_key"],
            mapping["calls"],
            mapping["input_tokens"],
            mapping["cached_input_tokens"],
            mapping["output_tokens"],
        )
    else:  # 普通元组(测试里手写的行)
        values = (row[0], row[1], row[2], row[3], row[4])
    key, calls, inp, cached, out = values
    return (
        key,
        int(calls or 0),
        int(inp or 0),
        int(cached or 0),
        int(out or 0),
    )



def _estimated_row_values(row: Any) -> tuple[Any, int, int] | None:
    """估算聚合行 → `(group_key, estimated_calls, estimated_tokens)`。

    形状不符(例如旧替身只有 5 个 token 列)时返回 ``None``:没有估算数据,
    绝不从别的列瞎猜。
    """
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return (
                mapping["group_key"],
                int(mapping["estimated_calls"] or 0),
                int(mapping["estimated_tokens"] or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None
    try:
        if len(row) != 3:
            return None
        return (row[0], int(row[1] or 0), int(row[2] or 0))
    except (TypeError, ValueError):
        return None

def _day_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime(DAY_FORMAT) if value.tzinfo else value.strftime(DAY_FORMAT)
    return str(value)


def _normalize_raw_key(group_by: str, raw_key: Any) -> str:
    """分组键的字符串形态(主语句与估算语句必须落到同一个 key 上)。"""
    if group_by == "chapter":
        return UNASSIGNED_KEY if raw_key is None else str(raw_key)
    if group_by == "day":
        return _day_key(raw_key)
    return "" if raw_key is None else str(raw_key)


def _group_key_and_label(
    group_by: str, raw_key: Any, chapter_numbers: dict[str, int]
) -> tuple[str, str]:
    key = _normalize_raw_key(group_by, raw_key)
    if group_by == "chapter":
        if raw_key is None:
            return UNASSIGNED_KEY, UNASSIGNED_LABEL
        number = chapter_numbers.get(key)
        # 章号查不到(章已删除/未编号)时宁可显示 id,也不瞎编一个"第N章"。
        return key, (f"第{number}章" if number is not None else key)
    return key, key


def _estimated_by_key(
    group_by: str, estimated_rows: Iterable[Any] | None
) -> dict[str, tuple[int, int]]:
    """估算聚合行 → `{分组键: (估算条数, 估算 token 数)}`。"""
    merged: dict[str, tuple[int, int]] = {}
    if estimated_rows is None:
        return merged
    for row in estimated_rows:
        parsed = _estimated_row_values(row)
        if parsed is None:
            continue
        raw_key, calls, tokens = parsed
        key = _normalize_raw_key(group_by, raw_key)
        prev_calls, prev_tokens = merged.get(key, (0, 0))
        merged[key] = (prev_calls + calls, prev_tokens + tokens)
    return merged


def build_summary_from_rows(
    *,
    project_id: Any,
    group_by: str,
    rows: Iterable[Any],
    chapter_numbers: dict[str, int] | None = None,
    estimated_rows: Iterable[Any] | None = None,
) -> dict:
    """把聚合行整理成 API 载荷:`totals` + 按 total_tokens 降序的 `groups`。

    a1001961:``estimated_rows`` 是 `estimated_summary_statement` 的结果(按同一
    分组维度的"估算行条数 / 估算 token 数")。给了它,`groups[]` 与 `totals`
    就各自带上 `estimated_calls` / `estimated_tokens`;不给(纯函数老调用者、
    或该项目根本没有估算行)就不出现在 payload 里,保持既有形状。
    `total_tokens` 仍是 `input + output` —— 估算行的数已经在 input/output 里,
    再加一遍 `estimated_tokens` 就是重复计数。
    """
    group_by = normalize_group_by(group_by)
    numbers = dict(chapter_numbers or {})
    estimates = _estimated_by_key(group_by, estimated_rows)
    with_estimates = estimated_rows is not None
    totals = {
        "calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_hit_rate": 0.0,
        "estimated_calls": 0,
        "estimated_tokens": 0,
    }
    groups: list[dict] = []
    for row in rows:
        raw_key, calls, inp, cached, out = _row_values(row)
        key, label = _group_key_and_label(group_by, raw_key, numbers)
        total = inp + out  # 缓存命中已在 input 里,不再重复相加
        est_calls, est_tokens = estimates.get(key, (0, 0))
        group = {
            "key": key,
            "label": label,
            "calls": calls,
            "input_tokens": inp,
            "cached_input_tokens": cached,
            "output_tokens": out,
            "total_tokens": total,
        }
        if with_estimates:
            group["estimated_calls"] = est_calls
            group["estimated_tokens"] = est_tokens
        groups.append(group)
        totals["calls"] += calls
        totals["input_tokens"] += inp
        totals["cached_input_tokens"] += cached
        totals["output_tokens"] += out
        totals["total_tokens"] += total
        totals["estimated_calls"] += est_calls
        totals["estimated_tokens"] += est_tokens
    groups.sort(key=lambda item: (-item["total_tokens"], str(item["key"])))
    totals["cache_hit_rate"] = cache_hit_rate(
        totals["cached_input_tokens"], totals["input_tokens"]
    )
    return {
        "project_id": str(project_id),
        "group_by": group_by,
        "totals": totals,
        "groups": groups,
    }


def build_budget_from_rows(
    *,
    chapter_id: Any,
    rows: Iterable[Any],
    limit_tokens: int = 0,
) -> dict:
    """把单章聚合行整理成预算载荷。

    ``exceeded`` 用 ``>=``:刚好用满预算就算超(作者要的是"还能不能继续写"),
    且 `limit_tokens <= 0` 表示"没设预算",永不算超。
    """
    used = {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    by_step: list[dict] = []
    for row in rows:
        raw_key, calls, inp, cached, out = _row_values(row)
        total = inp + out
        by_step.append({
            "task_type": "" if raw_key is None else str(raw_key),
            "calls": calls,
            "total_tokens": total,
        })
        used["calls"] += calls
        used["input_tokens"] += inp
        used["cached_input_tokens"] += cached
        used["output_tokens"] += out
    by_step.sort(key=lambda item: (-item["total_tokens"], item["task_type"]))
    used_tokens = used["input_tokens"] + used["output_tokens"]
    limit = max(int(limit_tokens or 0), 0)
    return {
        "chapter_id": str(chapter_id),
        "used_tokens": used_tokens,
        "input_tokens": used["input_tokens"],
        "cached_input_tokens": used["cached_input_tokens"],
        "output_tokens": used["output_tokens"],
        "calls": used["calls"],
        "limit_tokens": limit,
        "exceeded": bool(limit > 0 and used_tokens >= limit),
        "by_step": by_step,
    }


# ---------------------------------------------------------------------------
# 异步读取(真实 PG 路径)
# ---------------------------------------------------------------------------


def _session_dialect(db: Any) -> str:
    try:
        bind = db.get_bind()
        return getattr(bind.dialect, "name", "postgresql")
    except Exception:
        return "postgresql"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _load_chapter_numbers(db: Any, chapter_ids: Sequence[str]) -> dict[str, int]:
    """批量取章号 → `{chapter_id: 章号}`(只读两列,不读正文)。"""
    ids: list[UUID] = []
    for raw in chapter_ids:
        try:
            ids.append(UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not ids:
        return {}
    from app.models.project import Chapter

    result = await db.execute(
        select(Chapter.id, Chapter.global_idx, Chapter.chapter_idx).where(Chapter.id.in_(ids))
    )
    numbers: dict[str, int] = {}
    for row in result.all():
        chapter_id, global_idx, chapter_idx = row[0], row[1], row[2]
        number = global_idx if global_idx is not None else chapter_idx
        if number is None:
            continue
        numbers[str(chapter_id)] = int(number)
    return numbers


async def fetch_project_usage_summary(
    db: Any,
    *,
    project_id: Any,
    group_by: str = DEFAULT_GROUP_BY,
    days: int | None = DEFAULT_DAYS,
    chapter_id: Any = None,
    chapter_number_lookup: ChapterNumberLookup | None = None,
    now: datetime | None = None,
) -> dict:
    """项目用量汇总。`group_by` 非法时抛 ``ValueError``(API 翻成 400)。"""
    group_by = normalize_group_by(group_by)
    window = clamp_days(days)
    since = (now or _utcnow()) - timedelta(days=window)
    dialect = _session_dialect(db)
    stmt = project_summary_statement(
        project_id=project_id,
        group_by=group_by,
        since=since,
        chapter_id=chapter_id,
        dialect=dialect,
    )
    rows = (await db.execute(stmt)).all()
    # a1001961: 同一窗口、同一分组维度上再问一句"哪些分组里有估算行"。两条
    # 语句都只选分组键 + token 列,不读正文/消息;合并在纯函数里做。
    est_stmt = estimated_summary_statement(
        project_id=project_id,
        group_by=group_by,
        since=since,
        chapter_id=chapter_id,
        dialect=dialect,
    )
    estimated_rows = [
        parsed
        for parsed in (
            _estimated_row_values(row) for row in (await db.execute(est_stmt)).all()
        )
        if parsed is not None
    ]
    numbers: dict[str, int] = {}
    if group_by == "chapter":
        keys = sorted({str(row[0]) for row in rows if row[0] is not None})
        if keys:
            if chapter_number_lookup is not None:
                numbers = dict(await chapter_number_lookup(keys))
            else:
                numbers = await _load_chapter_numbers(db, keys)
    return build_summary_from_rows(
        project_id=project_id,
        group_by=group_by,
        rows=rows,
        chapter_numbers=numbers,
        # 没有估算行就传 None:payload 不带估算字段,与改造前形状一致。
        estimated_rows=estimated_rows or None,
    )


async def fetch_chapter_usage_budget(
    db: Any,
    *,
    chapter_id: Any,
    limit_tokens: int = 100_000,
) -> dict:
    """单章已用 token(全书累计,不受 days 窗口限制)与是否超出预算。"""
    stmt = chapter_budget_statement(chapter_id=chapter_id)
    rows = (await db.execute(stmt)).all()
    return build_budget_from_rows(
        chapter_id=chapter_id, rows=rows, limit_tokens=limit_tokens
    )