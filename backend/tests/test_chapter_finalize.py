"""章后收尾的**唯一实现**(2026-11-21)。

背景:此前 SSE / 队列 / 评估探针三个入口各写一套章后收尾(摘要 + 实体派发 +
卷桥接 + 伏笔 + 事实检查),互相抄、字段名与成败处理不一致。对照
AI-Novel-Writing-Assistant 的"唯一执行链"原则(三入口必须落到同一个 runner,
任何入口不得再维护第二套实现),收敛到 app/services/chapter_finalize.py。

这组用例盯的是接线与语义边界(离线可跑)。
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.services import chapter_finalize as cf


def test_finalize_covers_all_five_steps():
    src = inspect.getsource(cf)
    for marker in ("summarize_and_save_chapter", "dispatch_for_chapter",
                   "backfill_prev_volume_summary", "check_chapter_foreshadow_setup",
                   "check_chapter_against_facts"):
        assert marker in src, f"统一收尾缺少步骤:{marker}"


def test_finalize_supports_dispatch_and_inline_modes():
    """生产走派发(Celery)、探针走同步(inline)—— 逻辑唯一,只有执行方式不同。"""
    sig = inspect.signature(cf.finalize_chapter)
    assert "mode" in sig.parameters
    assert sig.parameters["mode"].default == "dispatch"
    src = inspect.getsource(cf)
    assert "_extract_inline" in src and "dispatch_for_chapter" in src


@pytest.mark.asyncio
async def test_every_step_failure_is_swallowed_and_reported():
    """任何一步失败都只记 warning —— 章后收尾绝不能让正文交付失败。"""
    class Boom:
        def __getattr__(self, item):
            raise RuntimeError("boom")

    # 不触发卷桥接(它连数据库);这里只验证"失败被吞并记录"
    result = await cf.finalize_chapter(
        db=Boom(), chapter=object(), project_id="p", chapter_id="c",
        volume_id=None, full_text="正文" * 30, caller="test", mode="inline",
        run_volume_bridge=False)
    assert result["caller"] == "test"
    assert result["warnings"], "失败必须被记录成 warning 而不是抛出去"
    assert all(isinstance(w, str) and w.strip() for w in result["warnings"])


@pytest.mark.asyncio
async def test_missing_ids_skip_steps_instead_of_raising():
    result = await cf.finalize_chapter(
        db=object(), chapter=None, project_id=None, chapter_id=None,
        volume_id=None, full_text="", caller="test")
    assert result["steps"] == {}
    assert result["warnings"] == []


def test_all_three_entries_call_the_same_finalizer():
    """三入口(SSE / 队列 / 评估探针)都必须引用统一收尾 —— 唯一执行链的守门人。

    2026-11-21 收敛前,三处各写一套章后逻辑;现在都调
    `app/services/chapter_finalize.finalize_chapter`,且不允许绕过它自己再调
    实体派发 / 卷桥接。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    entries = {
        "SSE": "app/api/generate.py",
        "队列": "app/tasks/generation_tasks.py",
        "探针": "scripts/quality_eval/continuity_probe.py",
    }
    for label, rel in entries.items():
        text = (root / rel).read_text(encoding="utf-8")
        assert "chapter_finalize" in text, f"{label} 入口({rel})没有接入统一收尾"
    for label, rel in entries.items():
        text = (root / rel).read_text(encoding="utf-8")
        assert "await _dispatch_chapter_entities(" not in text, f"{label} 入口绕过统一收尾做实体派发"
        assert "await _backfill_volume_summary_safe(" not in text, f"{label} 入口绕过统一收尾做卷桥接"
    probe = (root / entries["探针"]).read_text(encoding="utf-8")
    assert 'mode="inline"' in probe or "mode='inline'" in probe, "探针应走 inline 模式"


def test_finalize_is_the_single_implementation_of_post_chapter_work():
    """唯一实现里不允许再出现第二套收尾逻辑的分叉。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    src = (root / "app/services/chapter_finalize.py").read_text(encoding="utf-8")
    # 五个步骤各只应出现一次调用
    assert src.count("summarize_and_save_chapter(chapter_id=") == 1
    assert src.count("dispatch_for_chapter(") == 1
    assert src.count("backfill_prev_volume_summary(") == 1
