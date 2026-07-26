"""Layered style injection (基调层 + 修正层).

When BOTH a dossier style_block (book dossier > author dossier) AND a bound
style profile's own compiled rules exist, the injected [风格要求] content
becomes two labeled layers:

    【基调层·来自书籍/作者档案】<dossier style_block>
    【修正层·作者本人规则（优先级高于基调层）】<compiled profile rules>

Budget ~1800 chars: the base (dossier) layer gets at most 1200 and is the
only layer ever truncated; the override keeps at least 400 of the budget and
is never cut. Single layer -> unchanged pre-layering behavior.

Covers: stack/split assembly and caps, build_style_injection_block passing
layered text uncut, evaluator prompt consuming both layers, and prompt
capture proving the SSE and async generation paths both inject the stacked
block.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.style_runtime import (
    STYLE_BASE_LAYER_LABEL,
    STYLE_INJECTION_MAX_CHARS,
    STYLE_OVERRIDE_LAYER_LABEL,
    STYLE_STACK_BASE_MAX_CHARS,
    STYLE_STACK_MAX_CHARS,
    build_style_injection_block,
    resolve_style_context,
    split_style_layers,
    stack_style_layers,
)

BASE_TEXT = "冷峻短句，白描推进，对白极简，情绪靠动作外化。"
RULE_TEXT = "每章以动作或对话切入，禁止环境铺陈开头"


# ---------------------------------------------------------------------------
# Fakes (same shape as test_style_signal_flow.py)
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal AsyncSession stand-in: get() by (model-name, pk), scripted execute()."""

    def __init__(self, objects: dict | None = None, execute_results: list | None = None):
        self._objects = objects or {}
        self._execute_results = list(execute_results or [])

    async def get(self, model, pk):
        return self._objects.get((model.__name__, str(pk)))

    async def execute(self, stmt):
        if self._execute_results:
            return self._execute_results.pop(0)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    async def rollback(self):
        pass


def _make_profile(**overrides):
    base = dict(
        name="测试风格",
        source_book="参考书甲",
        source_book_id=None,
        rules_json=[{"rule": RULE_TEXT, "weight": 0.9, "category": "structure"}],
        anti_ai_rules=[],
        tone_keywords=[],
        sample_passages=[],
        config_json={},
        bind_level="global",
        bind_target_id=None,
        is_active=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. stack_style_layers: assembly, order, caps
# ---------------------------------------------------------------------------


def test_stack_both_layers_labels_and_order():
    stacked = stack_style_layers(BASE_TEXT, "规则甲；规则乙。")
    assert stacked == (
        f"{STYLE_BASE_LAYER_LABEL}{BASE_TEXT}\n"
        f"{STYLE_OVERRIDE_LAYER_LABEL}规则甲；规则乙。"
    )
    # base layer strictly before the override layer
    assert stacked.index(STYLE_BASE_LAYER_LABEL) < stacked.index(STYLE_OVERRIDE_LAYER_LABEL)


def test_stack_base_capped_at_base_max():
    long_base = "基" * 3000
    stacked = stack_style_layers(long_base, "短规则。")
    base, override = split_style_layers(stacked)
    assert len(base) == STYLE_STACK_BASE_MAX_CHARS
    assert override == "短规则。"


def test_stack_long_override_squeezes_base_and_is_never_truncated():
    long_base = "基" * 3000
    long_override = "规" * 1500
    stacked = stack_style_layers(long_base, long_override)
    base, override = split_style_layers(stacked)
    assert override == long_override  # override survives intact
    assert len(base) == STYLE_STACK_MAX_CHARS - 1500  # 300: remainder goes to base
    assert len(base) + len(override) <= STYLE_STACK_MAX_CHARS


def test_stack_override_beyond_budget_degrades_to_single_layer():
    """An override that alone exhausts the budget is still never cut; the
    base layer is squeezed out entirely."""
    huge_override = "规" * (STYLE_STACK_MAX_CHARS + 200)
    stacked = stack_style_layers("基" * 500, huge_override)
    assert stacked == huge_override
    assert STYLE_BASE_LAYER_LABEL not in stacked


def test_stack_single_layer_fallbacks():
    assert stack_style_layers(BASE_TEXT, "") == BASE_TEXT
    assert stack_style_layers("", "规则。") == "规则。"
    assert stack_style_layers("", "") == ""
    # single-layer text carries no labels (pre-layering behavior unchanged)
    assert STYLE_BASE_LAYER_LABEL not in stack_style_layers(BASE_TEXT, "")


def test_split_style_layers_round_trip_and_plain():
    stacked = stack_style_layers(BASE_TEXT, "规则甲。")
    assert split_style_layers(stacked) == (BASE_TEXT, "规则甲。")
    assert split_style_layers("普通单层风格文本") == ("普通单层风格文本", "")
    assert split_style_layers("") == ("", "")


# ---------------------------------------------------------------------------
# 2. build_style_injection_block: layered text is never sliced
# ---------------------------------------------------------------------------


def test_injection_block_passes_layered_text_uncut():
    stacked = stack_style_layers("基" * 1300, "规" * 500)
    assert len(stacked) > STYLE_INJECTION_MAX_CHARS
    block = build_style_injection_block(stacked)
    assert block == "[风格要求] " + stacked  # no [:1200] slice
    assert block.endswith("规" * 500)  # override tail intact


def test_injection_block_still_caps_plain_text():
    capped = build_style_injection_block("风" * 5000)
    assert len(capped) == len("[风格要求] ") + STYLE_INJECTION_MAX_CHARS


# ---------------------------------------------------------------------------
# 3. resolve_style_context returns the stacked text (settings-bound profile
#    + reference-book dossier)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_style_context_returns_layered_text():
    book_id = str(uuid.uuid4())
    profile = _make_profile(source_book_id=book_id)
    book = SimpleNamespace(
        id=book_id, metadata_json={"dossier": {"style_block": BASE_TEXT}}
    )
    project = SimpleNamespace(settings_json={"style_profile_id": "prof-1"})
    db = _FakeDB(objects={
        ("Project", "proj-1"): project,
        ("StyleProfile", "prof-1"): profile,
        ("ReferenceBook", book_id): book,
    })
    ctx = await resolve_style_context(db, "proj-1")
    assert ctx.source == "layered:dossier"
    base, override = split_style_layers(ctx.style_text)
    assert base == BASE_TEXT
    assert RULE_TEXT in override


# ---------------------------------------------------------------------------
# 4. Evaluator consumes both layers
# ---------------------------------------------------------------------------


def test_evaluator_prompt_splits_stacked_layers():
    from app.services.chapter_evaluator import _build_user_prompt

    stacked = stack_style_layers(BASE_TEXT, f"- {RULE_TEXT}")
    prompt = _build_user_prompt(
        chapter_text="正文" * 20,
        chapter_outline={},
        previous_summary="",
        style_profile=stacked,
        active_foreshadows=None,
    )
    assert "## 目标风格·基调层" in prompt
    assert "像不像" in prompt  # base = 像不像这个基调
    assert BASE_TEXT in prompt
    assert "## 目标风格·修正层（优先级高于基调层）" in prompt
    assert "是否被遵守" in prompt  # override = 规则是否被遵守
    assert RULE_TEXT in prompt
    assert "## 目标风格描述" not in prompt  # replaced by the two-layer sections


def test_evaluator_prompt_plain_profile_text_unchanged():
    from app.services.chapter_evaluator import _build_user_prompt

    prompt = _build_user_prompt(
        chapter_text="正文" * 20,
        chapter_outline={},
        previous_summary="",
        style_profile="普通单层风格描述",
        active_foreshadows=None,
    )
    assert "## 目标风格描述" in prompt
    assert "普通单层风格描述" in prompt
    assert "## 目标风格·基调层" not in prompt
    assert "## 目标风格·修正层" not in prompt


_EVAL_JSON = (
    '{"plot_coherence": {"score": 8, "issues": []},'
    ' "character_consistency": {"score": 8, "issues": []},'
    ' "style_adherence": {"score": 8, "issues": []},'
    ' "narrative_pacing": {"score": 8, "issues": []},'
    ' "foreshadow_handling": {"score": 8, "issues": []}}'
)


@pytest.mark.asyncio
async def test_evaluator_llm_call_receives_stacked_text():
    from app.services.chapter_evaluator import ChapterEvaluator

    captured: dict = {}

    class _FakeRouter:
        async def generate_with_tier_fallback(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text=_EVAL_JSON, usage=SimpleNamespace(total_tokens=5)
            )

    stacked = stack_style_layers(BASE_TEXT, f"- {RULE_TEXT}")
    with patch(
        "app.services.chapter_evaluator.get_model_router_async",
        AsyncMock(return_value=_FakeRouter()),
    ):
        result = await ChapterEvaluator().evaluate(
            chapter_text="正文" * 50,
            chapter_outline={},
            style_profile=stacked,
        )
    assert result.overall == 8.0
    user_msg = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    assert BASE_TEXT in user_msg
    assert RULE_TEXT in user_msg
    assert "## 目标风格·修正层（优先级高于基调层）" in user_msg


# ---------------------------------------------------------------------------
# 5. Prompt capture: SSE and async generation paths both inject the stacked
#    block (real DB rows; writer patched to capture its instruction)
# ---------------------------------------------------------------------------


async def _insert_style_rows(project_id: str) -> tuple[str, str]:
    """Insert ReferenceBook (with dossier) + StyleProfile rows and bind the
    profile via project settings. Returns (book_id, profile_id)."""
    from app.db.session import async_session_factory, reset_engine
    from app.models.project import Project, ReferenceBook, StyleProfile

    # Earlier suite tests that run _run_async_safe spin their own event loop
    # and leave the cached engine pool holding stale-loop connections
    # ("attached to a different loop"). Drop the cached pool so this test's
    # sessions bind to the current (session-scoped) loop.
    reset_engine()

    async with async_session_factory() as db:
        book = ReferenceBook(
            title="层叠测试参考书",
            source="upload_txt",
            metadata_json={"dossier": {"style_block": BASE_TEXT}},
        )
        db.add(book)
        await db.flush()
        profile = StyleProfile(
            name="层叠测试风格",
            rules_json=[{"rule": RULE_TEXT, "weight": 0.9, "category": "structure"}],
            source_book_id=book.id,
            bind_level="global",
            is_active=1,
        )
        db.add(profile)
        await db.flush()
        project = await db.get(Project, project_id)
        project.settings_json = {
            **(project.settings_json or {}),
            "style_profile_id": str(profile.id),
        }
        await db.commit()
        return str(book.id), str(profile.id)


async def _delete_style_rows(book_id: str, profile_id: str) -> None:
    from sqlalchemy import delete

    from app.db.session import async_session_factory
    from app.models.project import ReferenceBook, StyleProfile

    async with async_session_factory() as db:
        await db.execute(delete(StyleProfile).where(StyleProfile.id == profile_id))
        await db.execute(delete(ReferenceBook).where(ReferenceBook.id == book_id))
        await db.commit()


def _assert_layered_block_in(instruction: str) -> None:
    assert "[风格要求] " + STYLE_BASE_LAYER_LABEL in instruction
    assert STYLE_OVERRIDE_LAYER_LABEL in instruction
    assert BASE_TEXT in instruction  # dossier base layer content
    assert RULE_TEXT in instruction  # profile override layer content


@pytest.mark.asyncio
async def test_sse_writer_prompt_receives_layered_block(auth_client, monkeypatch):
    """SSE path: /api/generate/chapter resolves the layered style text and
    injects it into ChapterGenerator's user_instruction."""
    import app.api.generate as gen_mod
    from app.services.chapter_generator import ChapterGenerator

    resp = await auth_client.post(
        "/api/projects", json={"title": "层叠注入SSE测试", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    book_id, profile_id = await _insert_style_rows(project_id)

    try:
        class _Ready:
            ready = True

        async def _fake_readiness(*args, **kwargs):
            return _Ready()

        monkeypatch.setattr(gen_mod, "build_outline_readiness_report", _fake_readiness)

        captured: dict = {}

        async def _capture_generate(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("capture-only (test)")

        monkeypatch.setattr(ChapterGenerator, "generate", _capture_generate)

        resp = await auth_client.post(
            "/api/generate/chapter",
            json={
                "project_id": project_id,
                "volume_id": str(uuid.uuid4()),
                "chapter_idx": 1,
                "use_scene_mode": False,
                "auto_revise": False,
            },
        )
        assert resp.status_code == 200, resp.text
        assert "user_instruction" in captured, "ChapterGenerator.generate never ran"
        _assert_layered_block_in(captured["user_instruction"])
    finally:
        await _delete_style_rows(book_id, profile_id)
        await auth_client.delete(f"/api/projects/{project_id}")


@pytest.mark.asyncio
async def test_async_writer_prompt_receives_layered_block(auth_client, monkeypatch):
    """Async path: _run_async_generation_impl resolves the same layered text
    via production_style_text_for_profile and appends the [风格要求] block."""
    from app.db.session import async_session_factory
    from app.models.generation_task import GenerationTask
    from app.models.project import Chapter, Volume
    from app.services.chapter_generator import ChapterGenerator
    from app.tasks.generation_tasks import _run_async_generation_impl

    resp = await auth_client.post(
        "/api/projects", json={"title": "层叠注入异步测试", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    book_id, profile_id = await _insert_style_rows(project_id)

    async with async_session_factory() as db:
        volume = Volume(project_id=project_id, title="第一卷", volume_idx=1)
        db.add(volume)
        await db.flush()
        chapter = Chapter(volume_id=volume.id, title="第一章", chapter_idx=1)
        db.add(chapter)
        await db.flush()
        task = GenerationTask(
            project_id=project_id,
            task_type="chapter",
            status="pending",
            params_json={
                "chapter_id": str(chapter.id),
                "user_input": "写第1章",
                # capture-only run: skip the scene planner so the patched
                # single-shot generator is invoked directly, then aborted.
                "force_direct_chapter": True,
                "auto_revise": False,
            },
        )
        db.add(task)
        await db.commit()
        task_id, chapter_id, volume_id = str(task.id), str(chapter.id), str(volume.id)

    try:
        captured: dict = {}

        async def _capture_generate(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("capture-only (test)")

        monkeypatch.setattr(ChapterGenerator, "generate", _capture_generate)
        monkeypatch.setattr(
            "app.services.model_router.get_model_router_async",
            AsyncMock(return_value=MagicMock()),
        )

        await _run_async_generation_impl(task_id)

        assert "user_instruction" in captured, "ChapterGenerator.generate never ran"
        _assert_layered_block_in(captured["user_instruction"])
    finally:
        from sqlalchemy import delete

        async with async_session_factory() as db:
            await db.execute(
                delete(GenerationTask).where(GenerationTask.id == task_id)
            )
            await db.execute(delete(Chapter).where(Chapter.id == chapter_id))
            await db.execute(delete(Volume).where(Volume.id == volume_id))
            await db.commit()
        await _delete_style_rows(book_id, profile_id)
        await auth_client.delete(f"/api/projects/{project_id}")
