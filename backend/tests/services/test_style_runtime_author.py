"""Injection-chain tests for author dossiers (app/services/style_runtime.py).

Preference order for the production style injection:
  book dossier style_block > author dossier style_block > compiled profile.

Also covers:
  - get_dossier_block accepting AuthorDossier rows (dossier_json) while the
    ReferenceBook path (metadata_json['dossier']) keeps working unchanged
  - resolve_style_context with an author-only binding (no profile resolves)
  - resolve_author_structure_block (author structure injection accessor)
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.style_runtime import (
    get_dossier_block,
    production_style_text_for_profile,
    resolve_author_structure_block,
    resolve_style_context,
)


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


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _make_profile(**overrides):
    base = dict(
        name="测试风格",
        source_book="参考书甲",
        source_book_id=None,
        rules_json=[{"rule": "短句为主，多留白", "weight": 0.9, "category": "rhythm"}],
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


def _author_row(style_block="作者惯用：短句冷峻。", structure_block="作者结构：三卷式。"):
    return SimpleNamespace(dossier_json={
        "style_block": style_block,
        "structure_block": structure_block,
        "world_block": "作者世界观：分层体系。",
    })


# ---------------------------------------------------------------------------
# get_dossier_block: same accessor for both granularities
# ---------------------------------------------------------------------------


def test_get_dossier_block_accepts_author_dossier_rows():
    row = _author_row()
    assert get_dossier_block(row, "style_block") == "作者惯用：短句冷峻。"
    assert get_dossier_block(row, "structure_block") == "作者结构：三卷式。"
    assert get_dossier_block(SimpleNamespace(dossier_json={}), "style_block") == ""
    assert get_dossier_block(SimpleNamespace(dossier_json=None), "style_block") == ""
    # ReferenceBook path unchanged (same helper signature keeps working)
    book = SimpleNamespace(metadata_json={"dossier": {"style_block": "书级风格"}})
    assert get_dossier_block(book, "style_block") == "书级风格"


# ---------------------------------------------------------------------------
# production_style_text_for_profile preference chain
# ---------------------------------------------------------------------------

_AUTHOR_SETTINGS = {"style_reference": {"author_name": "江南测试"}}


@pytest.mark.asyncio
async def test_book_dossier_wins_over_author_dossier():
    book_id = str(uuid.uuid4())
    profile = _make_profile(bind_level="book", bind_target_id=book_id)
    book = SimpleNamespace(
        id=book_id,
        metadata_json={"dossier": {"style_block": "书级：冷峻白描。"}},
    )
    # author lookup would also hit, but the book dossier must win first
    db = _FakeDB(
        objects={("ReferenceBook", book_id): book},
        execute_results=[_scalar_result(_author_row())],
    )
    text, source, ref_id = await production_style_text_for_profile(
        db, profile, settings_json=_AUTHOR_SETTINGS
    )
    assert source == "dossier"
    assert text == "书级：冷峻白描。"
    assert ref_id == book_id


@pytest.mark.asyncio
async def test_author_dossier_used_when_book_dossier_missing():
    book_id = str(uuid.uuid4())
    profile = _make_profile(bind_level="book", bind_target_id=book_id)
    book = SimpleNamespace(id=book_id, metadata_json={})  # no book dossier
    db = _FakeDB(
        objects={("ReferenceBook", book_id): book},
        execute_results=[_scalar_result(_author_row())],
    )
    text, source, ref_id = await production_style_text_for_profile(
        db, profile, settings_json=_AUTHOR_SETTINGS
    )
    assert source == "author_dossier"
    assert text == "作者惯用：短句冷峻。"
    assert ref_id == book_id


@pytest.mark.asyncio
async def test_compiled_fallback_when_no_dossier_at_either_tier():
    profile = _make_profile()
    db = _FakeDB(execute_results=[_scalar_result(None)])  # author row missing
    text, source, ref_id = await production_style_text_for_profile(
        db, profile, settings_json=_AUTHOR_SETTINGS
    )
    assert source == "compiled"
    assert "测试风格" in text


@pytest.mark.asyncio
async def test_no_author_lookup_without_settings_binding():
    """Backward compat: without settings_json the pre-author behavior holds."""
    profile = _make_profile()
    db = _FakeDB()
    text, source, ref_id = await production_style_text_for_profile(db, profile)
    assert source == "compiled"


# ---------------------------------------------------------------------------
# resolve_style_context: author-only binding (no profile resolves at all)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_style_context_author_only_binding():
    project = SimpleNamespace(settings_json=_AUTHOR_SETTINGS)
    db = _FakeDB(
        objects={("Project", "proj-1"): project},
        execute_results=[
            _scalar_result(None),           # project-level bind miss
            _scalar_result(None),           # global bind miss
            _scalar_result(_author_row()),  # author dossier hit
        ],
    )
    ctx = await resolve_style_context(db, "proj-1")
    assert ctx.profile is None
    assert ctx.source == "author_dossier"
    assert ctx.style_text == "作者惯用：短句冷峻。"


@pytest.mark.asyncio
async def test_resolve_style_context_empty_without_any_binding():
    project = SimpleNamespace(settings_json={})
    db = _FakeDB(objects={("Project", "proj-1"): project})
    ctx = await resolve_style_context(db, "proj-1")
    assert ctx.style_text == "" and ctx.source == ""


# ---------------------------------------------------------------------------
# resolve_author_structure_block (structure injection accessor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_author_structure_block():
    db = _FakeDB(execute_results=[_scalar_result(_author_row())])
    block = await resolve_author_structure_block(db, _AUTHOR_SETTINGS)
    assert block == "作者结构：三卷式。"

    assert await resolve_author_structure_block(_FakeDB(), {}) == ""
    assert await resolve_author_structure_block(_FakeDB(), None) == ""

    # author bound but no dossier row yet
    db_missing = _FakeDB(execute_results=[_scalar_result(None)])
    assert await resolve_author_structure_block(db_missing, _AUTHOR_SETTINGS) == ""
