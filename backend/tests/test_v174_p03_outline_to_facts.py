"""v1.7.4 P0-3: tests for outline_to_facts ETL.

Covers:
- etl_characters: deterministic insert from volume.new_characters
- etl_foreshadows: deterministic insert from volume.foreshadows.planted
- etl_world_rules JSON parsing: fenced JSON / bare JSON / regex fallback
- Idempotency: re-running an ETL skips existing rows

world_rules LLM extraction is mocked (no real LLM call) so the test runs offline.
"""
from __future__ import annotations

import json
import types
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.outline_to_facts import (
    etl_characters,
    etl_foreshadows,
    etl_world_rules,
)


PID = "f14712d6-6dc6-4cfb-b05f-e107fa02b63d"


class _FakeRow:
    def __init__(self, val):
        self._v = val

    def __getitem__(self, idx):
        return self._v[idx] if isinstance(self._v, tuple) else self._v


class _FakeResult:
    """Mock SQLAlchemy result that supports .all(), .first(), .scalars().all()."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self


class _FakeDB:
    """Minimal AsyncSession stub: records add() / commit() / execute()."""

    def __init__(self, execute_responses):
        # execute_responses: list of _FakeResult to return in order.
        self._responses = list(execute_responses)
        self.added: list = []
        self.commits = 0

    async def execute(self, *_args, **_kwargs):
        if not self._responses:
            return _FakeResult([])
        return self._responses.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


# ---------------------------------------------------------------------------
# etl_characters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_etl_characters_inserts_all_new_chars():
    # First execute: existing names lookup -> empty.
    # Second execute: volume outlines lookup -> one volume with 2 chars.
    vol_outline = {
        "volume_idx": 1,
        "new_characters": [
            {"name": "裴归尘", "role": "导师", "identity": "馆长"},
            {"name": "罗弥", "role": "商人", "identity": "烟契者"},
        ],
    }
    db = _FakeDB([
        _FakeResult([]),  # existing names empty
        _FakeResult([(vol_outline,)]),  # volume outline
    ])
    inserted, skipped = await etl_characters(db, PID)
    assert inserted == 2
    assert skipped == 0
    assert db.commits == 1
    assert len(db.added) == 2
    assert {c.name for c in db.added} == {"裴归尘", "罗弥"}


@pytest.mark.asyncio
async def test_etl_characters_skips_existing():
    vol_outline = {
        "volume_idx": 1,
        "new_characters": [
            {"name": "裴归尘", "role": "导师", "identity": "馆长"},
        ],
    }
    db = _FakeDB([
        _FakeResult([("裴归尘",)]),  # existing names
        _FakeResult([(vol_outline,)]),
    ])
    inserted, skipped = await etl_characters(db, PID)
    assert inserted == 0
    assert skipped == 1
    assert db.commits == 0  # nothing to commit


@pytest.mark.asyncio
async def test_etl_characters_handles_empty_outlines():
    db = _FakeDB([
        _FakeResult([]),
        _FakeResult([]),
    ])
    inserted, skipped = await etl_characters(db, PID)
    assert inserted == 0 and skipped == 0


# ---------------------------------------------------------------------------
# etl_foreshadows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_etl_foreshadows_inserts_planted_with_status():
    vol = {
        "volume_idx": 1,
        "foreshadows": {
            "planted": [
                {"description": "伏笔A", "resolve_conditions": ["条件1", "条件2"]},
                {"description": "伏笔B", "resolve_conditions": []},
            ]
        },
    }
    db = _FakeDB([
        _FakeResult([]),  # existing descs
        _FakeResult([(vol,)]),  # volume outlines
    ])
    inserted, skipped = await etl_foreshadows(db, PID)
    assert inserted == 2
    assert skipped == 0
    statuses = {f.status for f in db.added}
    assert statuses == {"planted"}
    proximities = {f.narrative_proximity for f in db.added}
    assert proximities == {0.5}
    chapters = {f.planted_chapter for f in db.added}
    assert chapters == {1}  # volume_idx=1


@pytest.mark.asyncio
async def test_etl_foreshadows_skips_dup_description():
    vol = {
        "volume_idx": 1,
        "foreshadows": {"planted": [{"description": "伏笔A", "resolve_conditions": []}]},
    }
    db = _FakeDB([
        _FakeResult([("伏笔A",)]),  # existing description
        _FakeResult([(vol,)]),
    ])
    inserted, skipped = await etl_foreshadows(db, PID)
    assert inserted == 0 and skipped == 1


@pytest.mark.asyncio
async def test_etl_foreshadows_handles_missing_planted_key():
    vol = {"volume_idx": 1, "foreshadows": {}}
    db = _FakeDB([_FakeResult([]), _FakeResult([(vol,)])])
    inserted, skipped = await etl_foreshadows(db, PID)
    assert inserted == 0 and skipped == 0


# ---------------------------------------------------------------------------
# etl_world_rules (LLM mocked)
# ---------------------------------------------------------------------------


@dataclass
class _StubGenResult:
    text: str


@pytest.mark.asyncio
async def test_etl_world_rules_parses_fenced_json():
    book_outline = {"raw_text": "这是一份超过200字的书级大纲。" * 20}
    db = _FakeDB([
        _FakeResult([(book_outline,)]),  # book outline raw_text
        _FakeResult([]),  # existing rules
    ])
    fake_text = (
        "```json\n"
        '[{"category": "重力", "rule_text": "万物下落"},'
        ' {"category": "时间", "rule_text": "时间单向流动"}]\n```'
    )
    with patch(
        "app.services.prompt_registry.run_text_prompt",
        new=AsyncMock(return_value=_StubGenResult(text=fake_text)),
    ):
        ins, sk = await etl_world_rules(db, PID)
    assert ins == 2 and sk == 0
    assert {r.category for r in db.added} == {"重力", "时间"}


@pytest.mark.asyncio
async def test_etl_world_rules_parses_bare_json():
    book_outline = {"raw_text": "x" * 400}
    db = _FakeDB([_FakeResult([(book_outline,)]), _FakeResult([])])
    fake_text = '[{"category": "A", "rule_text": "规则一二"}]'
    with patch(
        "app.services.prompt_registry.run_text_prompt",
        new=AsyncMock(return_value=_StubGenResult(text=fake_text)),
    ):
        ins, sk = await etl_world_rules(db, PID)
    assert ins == 1 and sk == 0


@pytest.mark.asyncio
async def test_etl_world_rules_parses_with_regex_fallback():
    book_outline = {"raw_text": "x" * 400}
    db = _FakeDB([_FakeResult([(book_outline,)]), _FakeResult([])])
    # LLM included surrounding prose around the JSON array.
    fake_text = (
        "以下是抽取结果：\n[{\"category\": \"X\", \"rule_text\": \"规则二三\"}]\n完毕。"
    )
    with patch(
        "app.services.prompt_registry.run_text_prompt",
        new=AsyncMock(return_value=_StubGenResult(text=fake_text)),
    ):
        ins, sk = await etl_world_rules(db, PID)
    assert ins == 1 and sk == 0


@pytest.mark.asyncio
async def test_etl_world_rules_returns_zero_for_short_outline():
    book_outline = {"raw_text": "太短"}
    db = _FakeDB([_FakeResult([(book_outline,)])])
    ins, sk = await etl_world_rules(db, PID)
    assert ins == 0 and sk == 0


@pytest.mark.asyncio
async def test_etl_world_rules_skips_existing():
    book_outline = {"raw_text": "x" * 400}
    db = _FakeDB([
        _FakeResult([(book_outline,)]),
        _FakeResult([("规则一二",)]),  # already exists
    ])
    fake_text = '[{"category": "A", "rule_text": "规则一二"}]'
    with patch(
        "app.services.prompt_registry.run_text_prompt",
        new=AsyncMock(return_value=_StubGenResult(text=fake_text)),
    ):
        ins, sk = await etl_world_rules(db, PID)
    assert ins == 0 and sk == 1


@pytest.mark.asyncio
async def test_etl_world_rules_handles_unparseable():
    book_outline = {"raw_text": "x" * 400}
    db = _FakeDB([_FakeResult([(book_outline,)]), _FakeResult([])])
    fake_text = "这是一段纯文本，没有 JSON 数组。"
    with patch(
        "app.services.prompt_registry.run_text_prompt",
        new=AsyncMock(return_value=_StubGenResult(text=fake_text)),
    ):
        ins, sk = await etl_world_rules(db, PID)
    assert ins == 0 and sk == 0
