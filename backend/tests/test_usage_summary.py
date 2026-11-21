"""用量可见性(a1001960):聚合口径、分组、预算边界与"不读大字段"。

这一批的契约是前端照抄的:字段名、`total_tokens`/`cache_hit_rate` 的口径、
四种分组、`exceeded` 的边界都不能改。两条最容易写错的地方各有用例钉住:

* `cached_input_tokens` 是 `input_tokens` 的**子集**,总 token 不能再加一遍;
* 聚合下推到 SQL,**绝不** select `messages_json` / `response_text`
  (一张月表几百万行,读进来会把进程打爆)。

真跑:聚合语句在 SQLite 内存库上执行(离线、无 socket);
纯函数部分用构造行核对标签与边界。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api import usage as usage_api
from app.models.call_log import LLMCallLog
from app.services.usage_summary import (
    UNASSIGNED_KEY,
    UNASSIGNED_LABEL,
    build_budget_from_rows,
    build_summary_from_rows,
    cache_hit_rate,
    chapter_budget_statement,
    clamp_days,
    fetch_project_usage_summary,
    normalize_group_by,
    project_summary_statement,
)

PROJECT = uuid.UUID("11111111-1111-4111-8111-111111111111")
CH_A = uuid.UUID("22222222-2222-4222-8222-222222222222")
CH_B = uuid.UUID("33333333-3333-4333-8333-333333333333")

DAY1 = dt.datetime(2024, 5, 1, 9, 0, tzinfo=dt.timezone.utc)
DAY2 = dt.datetime(2024, 5, 2, 9, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# 夹具:SQLite 内存库(聚合语句真跑,不是手算替身)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    LLMCallLog.__table__.create(engine)
    with Session(engine) as db:
        yield db
        db.rollback()
    engine.dispose()


def _log(*, chapter_id, task_type, model, input_tokens, cached_input_tokens,
         output_tokens, created_at=DAY1, project_id=PROJECT) -> LLMCallLog:
    return LLMCallLog(
        id=uuid.uuid4(),
        task_type=task_type,
        project_id=project_id,
        chapter_id=chapter_id,
        messages_json=[{"role": "user", "content": "巨大的prompt"}],
        response_text="巨大的正文",
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        model=model,
        status="ok",
        created_at=created_at,
    )


@pytest.fixture()
def seeded(session: Session) -> Session:
    session.add_all([
        # 第 A 章:写作 + 评审,输入 1200(含缓存 400),输出 600
        _log(chapter_id=CH_A, task_type="scene_writer", model="gpt-4o",
             input_tokens=1000, cached_input_tokens=400, output_tokens=500),
        _log(chapter_id=CH_A, task_type="evaluation", model="gpt-4o",
             input_tokens=200, cached_input_tokens=0, output_tokens=100),
        # 第 B 章:输入 800(全部命中缓存),输出 200
        _log(chapter_id=CH_B, task_type="scene_writer", model="claude-3",
             input_tokens=800, cached_input_tokens=800, output_tokens=200,
             created_at=DAY2),
        # 未归章:规划类调用
        _log(chapter_id=None, task_type="foreshadow", model="gpt-4o",
             input_tokens=50, cached_input_tokens=0, output_tokens=50,
             created_at=DAY2),
    ])
    session.commit()
    return session


def _summary(session: Session, *, group_by: str, chapter_id=None, since=None) -> dict:
    stmt = project_summary_statement(
        project_id=PROJECT, group_by=group_by, since=since, chapter_id=chapter_id,
        dialect="sqlite",
    )
    rows = session.execute(stmt).all()
    return build_summary_from_rows(project_id=PROJECT, group_by=group_by, rows=rows)


def _groups(summary: dict) -> dict[str, dict]:
    return {group["key"]: group for group in summary["groups"]}


# ---------------------------------------------------------------------------
# 1. 聚合正确性 + 口径(缓存是输入的子集)
# ---------------------------------------------------------------------------


def test_totals_treat_cached_tokens_as_part_of_input(seeded: Session):
    summary = _summary(seeded, group_by="chapter")
    totals = summary["totals"]

    assert totals["calls"] == 4
    assert totals["input_tokens"] == 2050
    assert totals["cached_input_tokens"] == 1200
    assert totals["output_tokens"] == 850
    # 输入里已含命中部分 → 总 token 不得再把缓存加一遍(否则是 3250)
    assert totals["total_tokens"] == 2900
    assert totals["cache_hit_rate"] == pytest.approx(1200 / 2050, abs=1e-6)
    assert summary["project_id"] == str(PROJECT)
    assert summary["group_by"] == "chapter"


def test_groups_are_sorted_by_total_tokens_desc(seeded: Session):
    summary = _summary(seeded, group_by="chapter")
    assert [g["total_tokens"] for g in summary["groups"]] == [1800, 1000, 100]
    assert summary["groups"][0]["key"] == str(CH_A)
    assert summary["groups"][-1]["key"] == UNASSIGNED_KEY
    assert summary["groups"][-1]["label"] == UNASSIGNED_LABEL


def test_cached_hit_rate_is_zero_without_input():
    assert cache_hit_rate(0, 0) == 0.0
    assert cache_hit_rate(500, 0) == 0.0
    assert cache_hit_rate(None, None) == 0.0
    assert cache_hit_rate(250, 1000) == 0.25
    # 四舍五入到 6 位,前端直接显示百分比
    assert cache_hit_rate(1, 3) == 0.333333


# ---------------------------------------------------------------------------
# 2. 四种分组
# ---------------------------------------------------------------------------


def test_group_by_task_type(seeded: Session):
    groups = _groups(_summary(seeded, group_by="task_type"))
    assert groups["scene_writer"]["calls"] == 2
    assert groups["scene_writer"]["total_tokens"] == 2500
    assert groups["evaluation"]["total_tokens"] == 300
    assert groups["foreshadow"]["total_tokens"] == 100
    # task_type / model / day 的 label 就是 key
    assert groups["evaluation"]["label"] == "evaluation"


def test_group_by_model(seeded: Session):
    groups = _groups(_summary(seeded, group_by="model"))
    assert groups["gpt-4o"]["calls"] == 3
    assert groups["gpt-4o"]["total_tokens"] == 1900
    assert groups["claude-3"]["total_tokens"] == 1000
    assert groups["claude-3"]["cached_input_tokens"] == 800


def test_group_by_day_is_utc_yyyymmdd(seeded: Session):
    summary = _summary(seeded, group_by="day")
    groups = _groups(summary)
    assert sorted(groups) == ["2024-05-01", "2024-05-02"]
    assert groups["2024-05-01"]["total_tokens"] == 1800
    assert groups["2024-05-02"]["total_tokens"] == 1100
    assert groups["2024-05-02"]["label"] == "2024-05-02"

# ---------------------------------------------------------------------------
# 3. 过滤:chapter_id 与 days 窗口
# ---------------------------------------------------------------------------


def test_chapter_filter_only_counts_that_chapter(seeded: Session):
    summary = _summary(seeded, group_by="task_type", chapter_id=CH_A)
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["total_tokens"] == 1800
    assert {g["key"] for g in summary["groups"]} == {"scene_writer", "evaluation"}


def test_days_window_excludes_older_calls(seeded: Session):
    summary = _summary(seeded, group_by="day", since=dt.datetime(
        2024, 5, 2, 0, 0, tzinfo=dt.timezone.utc))
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["total_tokens"] == 1100


def test_days_window_is_clamped_to_1_365():
    assert clamp_days(None) == 30
    assert clamp_days(0) == 1
    assert clamp_days(-7) == 1
    assert clamp_days(365) == 365
    assert clamp_days(10_000) == 365
    assert clamp_days("15") == 15


def test_unknown_group_by_is_rejected():
    with pytest.raises(ValueError):
        normalize_group_by("project")
    # 空值退回默认(前端省略参数时不该报错)
    assert normalize_group_by(None) == "chapter"
    assert normalize_group_by("") == "chapter"


# ---------------------------------------------------------------------------
# 4. 章级预算
# ---------------------------------------------------------------------------


def _chapter_rows(session: Session, chapter_id=CH_A):
    stmt = chapter_budget_statement(chapter_id=chapter_id)
    return session.execute(stmt).all()


def test_chapter_budget_totals(seeded: Session):
    payload = build_budget_from_rows(
        chapter_id=CH_A, rows=_chapter_rows(seeded), limit_tokens=100_000)
    assert payload["chapter_id"] == str(CH_A)
    assert payload["used_tokens"] == 1800
    assert payload["input_tokens"] == 1200
    assert payload["cached_input_tokens"] == 400
    assert payload["output_tokens"] == 600
    assert payload["calls"] == 2
    assert payload["limit_tokens"] == 100_000
    assert payload["exceeded"] is False
    assert payload["by_step"] == [
        {"task_type": "scene_writer", "calls": 1, "total_tokens": 1500},
        {"task_type": "evaluation", "calls": 1, "total_tokens": 300},
    ]


def test_exceeded_is_true_exactly_at_the_limit(seeded: Session):
    rows = _chapter_rows(seeded)
    just_at = build_budget_from_rows(
        chapter_id=CH_A, rows=rows, limit_tokens=1800)
    below = build_budget_from_rows(
        chapter_id=CH_A, rows=rows, limit_tokens=1801)
    assert just_at["used_tokens"] == 1800
    assert just_at["exceeded"] is True, "用满预算就算超,不是 >"
    assert below["exceeded"] is False


def test_zero_or_negative_limit_never_exceeds(seeded: Session):
    rows = _chapter_rows(seeded)
    for limit in (0, -1):
        payload = build_budget_from_rows(
            chapter_id=CH_A, rows=rows, limit_tokens=limit)
        assert payload["exceeded"] is False
        assert payload["limit_tokens"] == 0


def test_empty_chapter_budget_reports_zeros():
    payload = build_budget_from_rows(chapter_id=CH_B, rows=[], limit_tokens=100)
    assert payload["used_tokens"] == 0
    assert payload["calls"] == 0
    assert payload["by_step"] == []
    assert payload["exceeded"] is False


# ---------------------------------------------------------------------------
# 5. 聚合不得读取大字段(性能/内存的硬约束)
# ---------------------------------------------------------------------------


def _compiled(stmt) -> str:
    return str(stmt.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_aggregate_sql_never_selects_prompt_or_response_bodies():
    stmt = project_summary_statement(project_id=PROJECT, group_by="chapter")
    sql = _compiled(stmt)
    assert "response_text" not in sql
    assert "messages_json" not in sql
    assert "rag_hits_json" not in sql
    # 只选 token 列与分组键
    assert {col for col in stmt.selected_columns.keys()} == {
        "group_key", "calls", "input_tokens", "cached_input_tokens", "output_tokens",
    }
    assert "sum(llm_call_logs.input_tokens)" in sql
    assert "sum(llm_call_logs.cached_input_tokens)" in sql
    assert "group by" in sql.lower()


def test_budget_sql_groups_by_task_type_without_bodies():
    stmt = chapter_budget_statement(chapter_id=CH_A)
    sql = _compiled(stmt)
    assert "response_text" not in sql and "messages_json" not in sql
    assert "group by llm_call_logs.task_type" in sql.lower()


def test_day_grouping_is_utc_on_postgres():
    sql = _compiled(project_summary_statement(project_id=PROJECT, group_by="day"))
    assert "timezone('UTC'" in sql
    assert "date_trunc('day'" in sql


# ---------------------------------------------------------------------------
# 6. 异步路径 + 章号标签 + 非法分组的 400
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    """只实现聚合路径用到的东西:execute 返回构造行。"""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self.rows)


def test_async_summary_labels_chapters_with_their_numbers():
    rows = [
        (CH_A, 2, 1200, 400, 600),
        (CH_B, 1, 800, 800, 200),
        (None, 1, 50, 0, 50),
    ]

    async def lookup(ids):
        assert sorted(ids) == sorted([str(CH_A), str(CH_B)])
        return {str(CH_A): 1, str(CH_B): 12}

    db = _FakeDB(rows)
    summary = asyncio.run(fetch_project_usage_summary(
        db, project_id=PROJECT, group_by="chapter", days=7,
        chapter_number_lookup=lookup))

    labels = {group["key"]: group["label"] for group in summary["groups"]}
    assert labels[str(CH_A)] == "第1章"
    assert labels[str(CH_B)] == "第12章"
    assert labels[UNASSIGNED_KEY] == UNASSIGNED_LABEL
    assert "response_text" not in str(db.statements[0].compile(
        dialect=postgresql.dialect()))


def test_async_summary_skips_chapter_lookup_for_other_groupings():
    async def lookup(ids):  # 不该被调用
        raise AssertionError("task_type 分组不需要查章号")

    db = _FakeDB([("scene_writer", 1, 10, 0, 5)])
    summary = asyncio.run(fetch_project_usage_summary(
        db, project_id=PROJECT, group_by="task_type", chapter_number_lookup=lookup))
    assert summary["groups"] == [{
        "key": "scene_writer", "label": "scene_writer", "calls": 1,
        "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5,
        "total_tokens": 15,
    }]


def test_unknown_number_falls_back_to_the_chapter_id():
    summary = build_summary_from_rows(
        project_id=PROJECT, group_by="chapter",
        rows=[(CH_A, 1, 10, 0, 5)], chapter_numbers={})
    assert summary["groups"][0]["label"] == str(CH_A)


def test_invalid_group_by_returns_400_from_the_endpoint():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(usage_api.project_usage_summary(
            project_id=PROJECT, group_by="chapter_typo", days=30,
            chapter_id=None, db=None))
    assert excinfo.value.status_code == 400
    assert "group_by" in str(excinfo.value.detail)


def test_routes_are_registered_under_api_usage():
    paths = {route.path for route in usage_api.router.routes}
    assert "/api/usage/projects/{project_id}/summary" in paths
    assert "/api/usage/chapters/{chapter_id}/budget" in paths