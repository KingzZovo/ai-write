"""a1001961:供应商完全不报 usage 时的 token 估算(明确标记,不冒充真实计量)。

背景(2026-11-15 实测):经中转调 `gemini-3.1-pro` 时,即使请求带了
`stream_options={"include_usage": True}`,上游也一条 usage 都不返回(同一中转下
`deepseek-v4-flash` 正常),于是 100 章长跑里 2,664 条调用日志的
input/output token 全是 0 —— "AI 用量"页在主模型上全显示 0,比没有更糟。

这里钉住四条:

1. 只在**供应商一个数都没报**、且**输出非空**时估算;供应商只要报了任一数字
   就原样落库(不估算、不打标记)。
2. 估算行必须带 `usage_estimated=True`,真实行必须是 False —— 估算不能冒充计量。
3. `GenerationResult.usage` 保持真实语义(没报就是 0):截断判定
   (`output_tokens >= max_tokens * 0.95`)继续只看真实值,拿估算去喂它会假报。
4. 汇总里 `estimated_calls` / `estimated_tokens` 是估算行的**另一种切分**,
   `total_tokens` 仍是 `input + output`,不得重复计数。

全部离线:SQLite 内存库 + 假 provider 响应对象,不起真实 PG / 不打网络。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.call_log import LLMCallLog
from app.services import model_router as mr
from app.services.llm_call_logger import CallContext, log_llm_call
from app.services.model_router import (
    CHARS_PER_TOKEN,
    OpenAIProvider,
    TokenUsage,
    _estimate_from_texts,
    _prompt_texts,
    _resolve_usage_box,
    estimate_tokens,
)
from app.services.usage_summary import (
    build_summary_from_rows,
    estimated_summary_statement,
    project_summary_statement,
)

ENV = "TOKEN_ESTIMATE_CHARS_PER_TOKEN"


@pytest.fixture()
def clean_env(monkeypatch):
    """比例走默认值,且"非法 env 只告警一次"的模块级标志每个用例重置。"""
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(mr, "_chars_per_token_warned", False)
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. estimate_tokens 边界
# ---------------------------------------------------------------------------


def test_default_ratio_is_1_5():
    assert CHARS_PER_TOKEN == 1.5


def test_estimate_tokens_zero_and_negative_are_zero(clean_env):
    assert estimate_tokens(0) == 0
    assert estimate_tokens(-1) == 0
    assert estimate_tokens(-100_000) == 0
    # 类型兜底:不是数字就是"没有字符",不抛异常
    assert estimate_tokens(None) == 0
    assert estimate_tokens("abc") == 0


def test_estimate_tokens_single_char_is_at_least_one(clean_env):
    # 1 字按 1.5 字/token 算出来是 0.67 → round 到 1;有字就至少算 1 个 token
    assert estimate_tokens(1) == 1
    assert estimate_tokens(2) == 1
    assert estimate_tokens(3) == 2


def test_estimate_tokens_default_ratio(clean_env):
    assert estimate_tokens(1500) == 1000
    assert estimate_tokens(150) == 100


def test_estimate_tokens_env_override(clean_env):
    clean_env.setenv(ENV, "2")
    assert estimate_tokens(100) == 50
    assert estimate_tokens(1000) == 500


@pytest.mark.parametrize("bad", ["abc", "", "0", "-1.5", "nan", "inf", "  "])
def test_invalid_env_falls_back_to_default(clean_env, bad):
    """非法比例不能变成除零/负 token:一律回落 1.5。"""
    clean_env.setenv(ENV, bad)
    assert estimate_tokens(1500) == 1000
    assert mr._chars_per_token() == 1.5


def test_invalid_env_warns_only_once(clean_env, caplog):
    clean_env.setenv(ENV, "not-a-number")
    with caplog.at_level("WARNING", logger="app.services.model_router"):
        assert estimate_tokens(1500) == 1000
        assert estimate_tokens(1500) == 1000
        assert estimate_tokens(1500) == 1000
    warnings = [r for r in caplog.records if ENV in r.getMessage()]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# 2. _estimate_from_texts:输入与输出分别算
# ---------------------------------------------------------------------------


def test_estimate_from_texts_computes_both_sides_separately(clean_env):
    prompt = ["a" * 1500, "b" * 1500]
    assert _estimate_from_texts(prompt, "c" * 300) == (2000, 200)


def test_estimate_from_texts_empty_sides_are_zero(clean_env):
    assert _estimate_from_texts([], "x" * 150) == (0, 100)
    assert _estimate_from_texts(["x" * 150], "") == (100, 0)
    assert _estimate_from_texts([], "") == (0, 0)


def test_prompt_texts_collects_text_only():
    messages = [
        {"role": "system", "content": "abc"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "de"},
                {"type": "image_url", "image_url": {"url": "http://example/x.png"}},
            ],
        },
        "not-a-message",
    ]
    assert _prompt_texts(messages) == ["abc", "de"]


# ---------------------------------------------------------------------------
# 3. 触发条件:什么情况估、什么情况不估
# ---------------------------------------------------------------------------


def test_real_usage_is_copied_never_estimated(clean_env):
    box: dict = {}
    _resolve_usage_box(box, TokenUsage(1200, 800, 2000), ["p" * 1500], "o" * 300)
    assert box == {"input_tokens": 1200, "output_tokens": 800, "usage_estimated": False}


def test_provider_reports_only_one_side_blocks_estimation(clean_env):
    # 供应商报了 input、output 是 0(空内容):按规矩不估算,也不打估算标记
    box: dict = {}
    _resolve_usage_box(box, TokenUsage(50, 0, 50), ["p" * 1500], "o" * 1500)
    assert box["input_tokens"] == 50
    assert box["output_tokens"] == 0
    assert box["usage_estimated"] is False


def test_estimate_when_provider_reports_nothing(clean_env):
    box: dict = {}
    _resolve_usage_box(box, TokenUsage(), ["x" * 1500], "y" * 300)
    assert box == {"input_tokens": 1000, "output_tokens": 200, "usage_estimated": True}


def test_empty_output_is_never_estimated(clean_env):
    box: dict = {}
    _resolve_usage_box(box, TokenUsage(), ["x" * 1500], "")
    assert box == {}
    _resolve_usage_box(box, None, ["x" * 1500], "")
    assert box == {}


def test_numbers_already_in_box_win_over_estimation(clean_env):
    box = {"input_tokens": 11, "output_tokens": 7}
    _resolve_usage_box(box, None, ["x" * 1500], "y" * 300)
    assert box == {"input_tokens": 11, "output_tokens": 7}


# ---------------------------------------------------------------------------
# 4. 真实 provider 路径:无 usage 的流 / 有 usage 的流 / 非流式
# ---------------------------------------------------------------------------


def _chunk(content=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt: int, completion: int):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        ),
    )


class _FakeStream:
    def __init__(self, parts):
        self._parts = parts
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for part in self._parts:
            yield part

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_without_usage_is_estimated_and_flagged(clean_env):
    """中转吞掉 usage 的那条路径:以前记 0,现在记估算并标记。"""
    provider = OpenAIProvider(api_key="k")
    stream = _FakeStream([
        _chunk("你好" * 150),          # 300 字
        _chunk(None, finish_reason="stop"),
    ])
    provider._create_chat_stream = AsyncMock(return_value=stream)
    box: dict = {}
    out = [
        c
        async for c in provider.generate_stream(
            [{"role": "user", "content": "x" * 1500}],
            model="m",
            task_type="scene_writer",
            retry_attempts=1,
            usage_box=box,
        )
    ]
    assert "".join(out) == "你好" * 150
    assert box == {"input_tokens": 1000, "output_tokens": 200, "usage_estimated": True}
    assert stream.closed is True


@pytest.mark.asyncio
async def test_stream_with_real_usage_is_not_estimated(clean_env):
    """供应商报了 usage:box 原样镜像,且**不出现**估算标记。"""
    provider = OpenAIProvider(api_key="k")
    stream = _FakeStream([
        _chunk("正文" * 100),
        _chunk(None, finish_reason="stop"),
        _usage_chunk(30, 9),
    ])
    provider._create_chat_stream = AsyncMock(return_value=stream)
    box: dict = {}
    _ = [
        c
        async for c in provider.generate_stream(
            [{"role": "user", "content": "x" * 1500}],
            model="m",
            task_type="scene_writer",
            retry_attempts=1,
            usage_box=box,
        )
    ]
    assert box == {"input_tokens": 30, "output_tokens": 9}


@pytest.mark.asyncio
async def test_nonstream_estimate_does_not_touch_generation_result_usage(clean_env):
    """估算只进 usage_box / 日志,**不**改 GenerationResult.usage。

    `_raise_if_empty_thinking_burn` 用 `usage.output_tokens >= max_tokens*0.95`
    判"思考把预算烧完了"。这里 max_tokens=100,估算输出 200 远超阈值 —— 若估算
    污染了 usage,就会立刻抛假截断错误。调用必须正常返回,且 usage 仍是 0。
    """
    provider = OpenAIProvider(api_key="k")

    async def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="z" * 300),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    box: dict = {}
    result = await provider.generate(
        messages=[{"role": "user", "content": "x" * 1500}],
        model="m",
        temperature=0.7,
        max_tokens=100,
        task_type="scene_writer",
        stream=False,
        usage_box=box,
    )
    assert result.text == "z" * 300
    assert box == {"input_tokens": 1000, "output_tokens": 200, "usage_estimated": True}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (0, 0)


# ---------------------------------------------------------------------------
# 5. 日志落地:usage_estimated 必须写库
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self):
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def test_call_context_defaults_to_not_estimated():
    assert CallContext().usage_estimated is False


@pytest.mark.asyncio
async def test_logger_persists_estimated_flag(clean_env):
    from app.services import usage_service

    recorded: list[dict] = []

    async def fake_record_usage(*args, **kwargs):
        recorded.append(kwargs)

    clean_env.setattr(usage_service, "record_usage", fake_record_usage)
    db = _FakeDB()
    async with log_llm_call(
        db=db,
        task_type="scene_writer",
        prompt_id=None,
        project_id=None,
        chapter_id=None,
        messages=[{"role": "user", "content": "hi"}],
        rag_hits=None,
        model="gemini-3.1-pro",
        endpoint_id=None,
    ) as ctx:
        ctx.add_chunk("正文" * 100)
        ctx.set_usage(1000, 200, 0, usage_estimated=True)

    row = db.added[0]
    assert row.usage_estimated is True
    assert (row.input_tokens, row.output_tokens) == (1000, 200)
    # 配额同步照旧吃这批数(它们已经是本次调用"最接近真实"的用量)
    assert recorded and recorded[0]["prompt_tokens"] == 1000


@pytest.mark.asyncio
async def test_logger_defaults_to_real_usage(clean_env):
    from app.services import usage_service

    async def fake_record_usage(*args, **kwargs):
        return None

    clean_env.setattr(usage_service, "record_usage", fake_record_usage)
    db = _FakeDB()
    async with log_llm_call(
        db=db,
        task_type="scene_writer",
        prompt_id=None,
        project_id=None,
        chapter_id=None,
        messages=[{"role": "user", "content": "hi"}],
        rag_hits=None,
        model="m",
        endpoint_id=None,
    ) as ctx:
        ctx.add_chunk("正文")
        ctx.set_usage(11, 7)

    assert db.added[0].usage_estimated is False


# ---------------------------------------------------------------------------
# 6. 汇总:estimated_calls / estimated_tokens,total 不重复计数
# ---------------------------------------------------------------------------


def test_summary_counts_estimates_without_double_counting(clean_env):
    rows = [
        ("ch-a", 3, 3000, 0, 1500),
        ("ch-b", 1, 100, 0, 50),
    ]
    estimated_rows = [("ch-a", 2, 4200), ("ch-b", 0, 0)]
    summary = build_summary_from_rows(
        project_id=uuid.uuid4(),
        group_by="chapter",
        rows=rows,
        estimated_rows=estimated_rows,
    )
    totals = summary["totals"]
    assert totals["input_tokens"] == 3100
    assert totals["output_tokens"] == 1550
    # 估算行的数已经在 input/output 里 → 总 token 不能再加一遍 estimated_tokens
    assert totals["total_tokens"] == 4650
    assert totals["estimated_calls"] == 2
    assert totals["estimated_tokens"] == 4200

    groups = {group["key"]: group for group in summary["groups"]}
    assert groups["ch-a"]["estimated_calls"] == 2
    assert groups["ch-a"]["estimated_tokens"] == 4200
    assert groups["ch-a"]["total_tokens"] == 4500
    assert groups["ch-b"]["estimated_calls"] == 0
    assert groups["ch-b"]["estimated_tokens"] == 0


def test_summary_without_estimated_rows_keeps_the_old_shape(clean_env):
    rows = [("ch-a", 1, 10, 0, 5)]
    summary = build_summary_from_rows(
        project_id=uuid.uuid4(), group_by="chapter", rows=rows
    )
    assert "estimated_calls" not in summary["groups"][0]
    assert "estimated_tokens" not in summary["groups"][0]
    assert summary["totals"]["estimated_calls"] == 0
    assert summary["totals"]["estimated_tokens"] == 0
    assert summary["totals"]["total_tokens"] == 15


def test_summary_ignores_rows_of_an_unknown_shape(clean_env):
    """旧替身/旧形状的行不是估算数据 —— 当作"没有",不瞎猜。"""
    summary = build_summary_from_rows(
        project_id=uuid.uuid4(),
        group_by="chapter",
        rows=[("ch-a", 1, 10, 0, 5)],
        estimated_rows=[("ch-a", 1, 10, 0, 5)],
    )
    assert summary["totals"]["estimated_calls"] == 0
    assert summary["totals"]["estimated_tokens"] == 0


def test_estimated_statement_only_reads_counters():
    stmt = estimated_summary_statement(
        project_id=uuid.uuid4(), group_by="chapter"
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "response_text" not in sql
    assert "messages_json" not in sql
    assert set(stmt.selected_columns.keys()) == {
        "group_key", "estimated_calls", "estimated_tokens",
    }
    assert "usage_estimated" in sql


def test_estimated_statement_marks_only_flagged_rows_in_sqlite(clean_env):
    """真跑一遍聚合语句:估算行与非估算行必须分开计。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    LLMCallLog.__table__.create(engine)
    project = uuid.uuid4()
    with Session(engine) as db:
        db.add_all([
            LLMCallLog(
                task_type="scene_writer", project_id=project, messages_json=[],
                input_tokens=1000, cached_input_tokens=0, output_tokens=500,
                usage_estimated=True,
            ),
            LLMCallLog(
                task_type="scene_writer", project_id=project, messages_json=[],
                input_tokens=10, cached_input_tokens=0, output_tokens=5,
                usage_estimated=False,
            ),
        ])
        db.commit()
        main_rows = db.execute(project_summary_statement(
            project_id=project, group_by="task_type", dialect="sqlite")).all()
        est_rows = db.execute(estimated_summary_statement(
            project_id=project, group_by="task_type", dialect="sqlite")).all()
        summary = build_summary_from_rows(
            project_id=project, group_by="task_type", rows=main_rows,
            estimated_rows=est_rows,
        )
    engine.dispose()

    totals = summary["totals"]
    assert totals["calls"] == 2
    assert totals["input_tokens"] == 1010
    assert totals["output_tokens"] == 505
    assert totals["total_tokens"] == 1515          # 不是 1515 + 1500
    assert totals["estimated_calls"] == 1
    assert totals["estimated_tokens"] == 1500

    group = summary["groups"][0]
    assert group["key"] == "scene_writer"
    assert group["calls"] == 2
    assert group["estimated_calls"] == 1
    assert group["estimated_tokens"] == 1500
    assert group["total_tokens"] == 1515


# ---------------------------------------------------------------------------
# 7. 模型列存在(NOT NULL + server_default,旧行读 false)
# ---------------------------------------------------------------------------


def test_model_column_is_not_null_with_server_default():
    column = LLMCallLog.__table__.columns["usage_estimated"]
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "false"