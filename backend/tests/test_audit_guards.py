"""审计修复的两条护栏(2026-11-17)。

1. `DISABLE_AUTH=1` 曾经只要留在 env 里就等于全站匿名可写,没有任何环境守卫;
2. 生成任务对**任何**异常都写 `failed`(终态),而重投/恢复只接管 `pending`
   —— 一次 DB 抖动就把任务永久判死。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from app import main as main_module
from app.tasks import generation_tasks


# --- DISABLE_AUTH ---------------------------------------------------------

@pytest.mark.parametrize("env,allowed", [
    ("development", True),
    ("", True),
    ("production", False),
    ("prod", False),
    ("PRODUCTION", False),
])
def test_disable_auth_only_outside_production(monkeypatch, env, allowed):
    monkeypatch.setattr(main_module, "_app_env", lambda: env.strip().lower())
    assert main_module._disable_auth_allowed() is allowed


# --- 瞬时错误分类 ---------------------------------------------------------

def test_transient_errors_include_infrastructure_kinds():
    kinds = generation_tasks._transient_errors()
    assert OperationalError in kinds
    assert asyncio.TimeoutError in kinds
    assert ConnectionError in kinds


def test_transient_error_is_recognised():
    kinds = generation_tasks._transient_errors()
    assert any(isinstance(OperationalError("stmt", {}, Exception("boom")), kind) for kind in kinds)


def test_business_error_is_not_transient():
    kinds = generation_tasks._transient_errors()
    assert not any(isinstance(ValueError("stale source"), kind) for kind in kinds)

# --- 大纲章数上限(2026-11-17) -------------------------------------------

def test_chapter_count_is_clamped_to_avoid_call_explosion():
    from app.services.outline_generator import _MAX_VOLUME_CHAPTERS, _clamp_chapter_count

    assert _clamp_chapter_count(24) == 24
    assert _clamp_chapter_count(_MAX_VOLUME_CHAPTERS + 1) == _MAX_VOLUME_CHAPTERS
    assert _clamp_chapter_count(5000) == _MAX_VOLUME_CHAPTERS


def test_chapter_count_falls_back_for_garbage():
    from app.services.outline_generator import _clamp_chapter_count

    assert _clamp_chapter_count(0) == 1
    assert _clamp_chapter_count(-3) == 1
    assert _clamp_chapter_count(None) == 1
    assert _clamp_chapter_count("abc") == 1


# --- 用量归属与成本(2026-11-17) ------------------------------------------

def test_usage_identity_falls_back_to_default_without_request(monkeypatch):
    from app.middlewares import request_identity as ident

    ident.set_current_user(None)
    assert ident.resolve_user_id() == "king"
    assert ident.resolve_user_id("explicit") == "explicit"


def test_usage_identity_uses_request_user():
    from app.middlewares import request_identity as ident

    ident.set_current_user("alice")
    try:
        assert ident.resolve_user_id() == "alice"
    finally:
        ident.set_current_user(None)


def test_cost_defaults_to_zero_and_scales_with_env(monkeypatch):
    from app.services.llm_call_logger import _estimate_cost_cents

    monkeypatch.delenv("USAGE_CENTS_PER_1K_TOKENS", raising=False)
    assert _estimate_cost_cents(10_000, 10_000) == 0

    monkeypatch.setenv("USAGE_CENTS_PER_1K_TOKENS", "2")
    assert _estimate_cost_cents(1000, 1000) == 4

    monkeypatch.setenv("USAGE_CENTS_PER_1K_TOKENS", "oops")
    assert _estimate_cost_cents(1000, 1000) == 0
    monkeypatch.setenv("USAGE_CENTS_PER_1K_TOKENS", "-1")
    assert _estimate_cost_cents(1000, 1000) == 0


# --- 伏笔检查必须真的被调用(2026-11-17) ---------------------------------

@pytest.mark.asyncio
async def test_foreshadow_setup_event_opens_its_own_session(monkeypatch):
    """回归:该检查曾引用事件流里不存在的 `save_db`,NameError 被吞 → 从未执行。"""
    from contextlib import asynccontextmanager

    from app.api import generate as gen
    from app.services import foreshadow_setup_check as check_mod

    seen = {}

    class _FakeSession:
        async def execute(self, *a, **k):  # pragma: no cover - 只验证会话被传入
            return None

    @asynccontextmanager
    async def fake_factory():
        seen["opened"] += 1
        yield _FakeSession()

    seen["opened"] = 0

    async def fake_check(db, *, project_id, chapter_id):
        seen["args"] = (db, project_id, chapter_id)

        report = type("R", (), {})()
        report.chapter_id = chapter_id
        report.planned = 2
        report.grounded = 1
        report.missing = [type("M", (), {"plan_key": "p", "title": "钥匙", "keywords": ("钥匙",)})()]
        return report

    monkeypatch.setattr("app.db.session.async_session_factory", fake_factory)
    monkeypatch.setattr(check_mod, "check_chapter_foreshadow_setup", fake_check)

    report, event = await gen._foreshadow_setup_event("proj", "chap")
    assert seen["opened"] == 1
    assert seen["args"][1:] == ("proj", "chap")
    assert report.planned == 2
    assert event and event["missing_count"] == 1


# --- 正文与版本同一事务(A4 结构性回归) ---------------------------------
# `_persist_chapter_now` 是端点内的闭包,无法直接单测;这里用源码结构锁死两条不变量:
# ① 正文赋值与版本插入之间不得再出现中间 commit(否则会留下"正文已更、版本仍旧");
# ② 版本写失败的分支必须 rollback,并把失败如实回报。

def test_content_and_version_share_one_transaction():
    import inspect
    import re

    from app.api import generate as gen

    src = inspect.getsource(gen)
    start = src.index("target_chapter.content_text = text_to_save")
    end = src.index("last_saved_quality_meta = quality_meta", start)
    window = src[start:end]

    commits = len(re.findall(r"await save_db\.commit\(\)", window))
    assert commits == 1, f"正文与版本之间应只有一次提交,实际 {commits} 次"
    assert "save_db.rollback()" in window
    assert "version write failed" in window.lower() or "version_write_failed" in window


# --- 上下文缓存失效必须晚于提交(2026-11-17) ------------------------------

@pytest.mark.asyncio
async def test_change_log_defers_cache_invalidation_until_commit(monkeypatch):
    from app.services import change_log as cl
    from app.services import ctxpack_cache

    calls: list[str] = []

    async def fake_invalidate(pid):
        calls.append(pid)

    monkeypatch.setattr(ctxpack_cache, "invalidate", fake_invalidate)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    real_session = sessionmaker(bind=create_engine("sqlite://"))()

    class _Db:
        sync_session = real_session

        def add(self, obj):  # pragma: no cover - 只为让 record_change 跑通
            pass

    await cl.record_change(_Db(), project_id="11111111-1111-4111-8111-111111111111",
                           actor_type="user", actor_id="a", target_type="character",
                           target_id=None, action="update", commit=False)
    # commit=False:不得立刻失效(否则提交前会有人用旧设定重建缓存)
    assert calls == []
    # 事件已挂上,事务对象上记着待处理的项目
    assert "11111111-1111-4111-8111-111111111111" in getattr(
        _Db.sync_session, "_ctxpack_pending_invalidations", set())


# --- 分块不得丢正文(2026-11-17) -----------------------------------------

def test_semantic_chunker_keeps_short_prose_before_a_chapter_heading():
    """回归:不足下限的短正文曾在下个章标题处被整体覆盖丢失。"""
    from app.services import semantic_chunker as chunker

    text = "第1章 起\n" + "短句。" * 10 + "\n第2章 承\n" + "正文" * 200
    slices = chunker.chunk(text)
    joined = "".join(item.raw_text for item in slices)
    assert "短句。" in joined, "章边界处的短正文不得丢失"


# --- 密钥解密失败必须显式(2026-11-17) ------------------------------------

def test_decrypt_failure_raises_instead_of_returning_empty(monkeypatch):
    from app.utils import crypto

    monkeypatch.setattr(crypto, "_fernet", type("F", (), {
        "decrypt": staticmethod(lambda _v: (_ for _ in ()).throw(crypto.InvalidToken())),
    })())
    try:
        crypto.decrypt_api_key("enc:garbage")
    except crypto.ApiKeyDecryptError:
        pass
    else:  # pragma: no cover
        raise AssertionError("解密失败必须抛 ApiKeyDecryptError,不能返回空串")


# --- 配额路径默认值(2026-11-17 第二轮审计) ------------------------------

def test_quota_prefixes_default_is_not_a_string(monkeypatch):
    """默认值是 tuple,旧实现直接对它调 ``.split(",")`` → 开开关即 500。

    上一轮把这条判成误报(以为逗号在引号内),复核实为独立 tuple 元素。
    """
    from app.middlewares import quota

    monkeypatch.delenv("QUOTA_PATHS", raising=False)
    assert isinstance(quota._DEFAULT_QUOTA_PATHS, tuple)
    prefixes = quota._quota_prefixes()
    assert isinstance(prefixes, tuple)
    assert "/api/generate" in prefixes
    assert len(prefixes) == 6
    assert not any(p.endswith(",") for p in prefixes)


def test_quota_prefixes_reads_env(monkeypatch):
    from app.middlewares import quota

    monkeypatch.setenv("QUOTA_PATHS", " /api/one , ,/api/two ")
    assert quota._quota_prefixes() == ("/api/one", "/api/two")


# --- 没有嵌入端点时必须显式失败(2026-11-17) ------------------------------

def test_embed_raises_instead_of_zero_vector():
    from app.services.model_router import EmbeddingUnavailableError, ModelRouter

    router = ModelRouter()
    router.embedding_provider = None
    router.providers = {}
    try:
        asyncio.run(router.embed("x"))
    except EmbeddingUnavailableError:
        pass
    else:  # pragma: no cover
        raise AssertionError("无嵌入端点必须抛 EmbeddingUnavailableError,不能返回零向量")


# --- 结束帧 delta=null 不得炸掉整章(2026-11-17) --------------------------

def test_stream_guard_tolerates_null_delta():
    from types import SimpleNamespace

    from app.services.provider_response_guard import guard_chat_stream

    def chunk(content, delta_present=True):
        delta = SimpleNamespace(content=content) if delta_present else None
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)],
                               usage=None)

    async def source():
        yield chunk("正文开头。")
        yield chunk(None, delta_present=False)  # 中转的结束/拒绝帧
        yield chunk("正文结尾。")

    async def collect():
        return [c async for c in guard_chat_stream(source())]

    chunks = asyncio.run(collect())
    assert len(chunks) == 3


# --- 第三批(2026-11-17) ------------------------------------------------

def test_redact_masks_query_secrets():
    """查询串里的 api_key/token 必须脱敏(日志与 Sentry 共用 redact)。"""
    from app.observability.logging import redact

    out = redact("GET /api/x?api_key=sk-live-1&token=abc123&q=1")
    assert "sk-live-1" not in out
    assert "abc123" not in out
    assert "q=1" in out


def test_cache_hit_rate_never_exceeds_one():
    """中转把 cached 报得比 input 大时,命中率不能 >1。"""
    from app.services.usage_summary import cache_hit_rate

    assert cache_hit_rate(500, 100) == 1.0
    assert cache_hit_rate(100, 0) == 0.0
    assert cache_hit_rate(50, 200) == 0.25


def test_quality_score_parse_failure_is_unavailable_not_low():
    """解析失败必须是"没评成",不能伪装成 0 分低质量。"""
    from app.services import quality_scorer as qs

    class _Router:
        async def generate(self, **_kw):
            return type("R", (), {"text": "这不是 JSON"})()

    scorer = qs.QualityScorer.__new__(qs.QualityScorer)
    scorer.router = _Router()
    scorer.threshold = 6.0
    score = asyncio.run(scorer.score(["样本"]))
    assert score.available is False
    assert score.verdict == "unknown"
    assert score.overall == 0.0


def test_plot_extraction_failure_raises():
    """提取失败必须抛,不能返回"正文前 100 字"充数。"""
    from app.services.feature_extractor import PlotExtractionError, PlotExtractor

    class _Router:
        async def generate(self, **_kw):
            return type("R", (), {"text": "not json"})()

    extractor = PlotExtractor.__new__(PlotExtractor)
    extractor.router = _Router()
    try:
        asyncio.run(extractor.extract("第一章正文。"))
    except PlotExtractionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("提取失败必须抛 PlotExtractionError")


# --- 后台任务路径必须与桌面路径接同样的章级检查(2026-11-20) ---------------

def test_background_generation_task_wires_budget_gate_and_foreshadow_check():
    """两条生成入口必须接同一套章级检查。

    实测过的漏接:每章 token 预算闸门与"伏笔缺铺垫"检查只接在
    `api/generate.py`(桌面 SSE),后台任务(`tasks/generation_tasks.py`)完全没有 ——
    同一本书换个入口生成,预算与伏笔告警就整个消失。这条用例直接盯源码里的接线。
    """
    import inspect

    from app.tasks import generation_tasks

    src = inspect.getsource(generation_tasks)
    assert "chapter_budget_verdict" in src, "后台任务没有接每章 token 预算闸门"
    assert "check_chapter_foreshadow_setup" in src, "后台任务没有接伏笔缺铺垫检查"
    # 顺序:预算闸门在任务转入 running 之后、真正生成之前。
    running_at = src.index('task.status = "running"')
    budget_at = src.index("chapter_budget_verdict")
    assert budget_at > running_at, "预算闸门必须在任务 running 之后、生成之前判定"


# --- 非叙事文本必须在**统一净化入口**里被剥离(2026-11-20) ------------------

def test_service_noise_stripping_lives_in_the_unified_sanitizer():
    """服务话术/中转诊断必须在 sanitize_prose 里剥离,而不是各入口自己判。

    根因:此前"拒答/诊断"只有**判定**(looks_like_refusal / looks_like_relay_error)
    没有**净化**,且只认"整章拒答/头部拒答";于是"正常正文 + 尾随服务话术"
    (实测 docs/evaluation/style-ref-10/ch01.txt)一路通过验收并落库。
    """
    import inspect

    from app.services.prose_sanitizer import sanitize_prose

    src = inspect.getsource(sanitize_prose)
    assert "strip_service_noise" in src, "sanitize_prose 没有接入服务话术剥离"


def test_service_noise_detector_is_position_independent():
    """尾部/中部/头部三种位置都要能剥离 —— 旧守卫只在头部 200 字符找。"""
    from app.services.prose_purity import strip_service_noise

    noise = "I seem to be encountering an error. Can I try something else for you?"
    body = "他把信封往桌角推了推,指尖在粗糙的陶釉上停了两秒才缩回袖口里。" * 40

    assert strip_service_noise(body + noise).removed, "尾部话术漏检"
    assert strip_service_noise(noise + body).removed, "头部粘连漏检"
    assert strip_service_noise(body + "\n\n" + noise + "\n\n" + body).removed, "中段话术漏检"


def test_both_generation_paths_sanitize_prose_before_persist():
    """桌面 SSE 与后台任务两条路径都必须过同一个净化器。"""
    import inspect

    from app.api import generate
    from app.tasks import generation_tasks

    assert "sanitize_prose" in inspect.getsource(generation_tasks)
    assert "sanitize_prose" in inspect.getsource(generate)
