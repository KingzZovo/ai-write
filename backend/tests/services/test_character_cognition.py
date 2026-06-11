"""Tests for the character cognition ledger (Task 17 / Q3).

Pure-logic coverage for apply_changes / serialize_for_prompt plus real-DB
integration coverage for load_ledger / save_ledger upsert semantics.
"""
from __future__ import annotations

import uuid

import pytest

from app.services.character_cognition import (
    READER,
    apply_changes,
    load_ledger,
    save_ledger,
    serialize_for_prompt,
)


# ---------------------------------------------------------------------------
# apply_changes (pure)
# ---------------------------------------------------------------------------


def test_apply_changes_moves_known_items():
    ledger = {
        "林冲": {"knows": [], "does_not_know": ["高俅设局"]},
        "__reader__": {"knows": ["高俅设局"], "does_not_know": []},
    }
    out = apply_changes(ledger, [{"character": "林冲", "learns": "高俅设局"}])
    assert "高俅设局" in out["林冲"]["knows"]
    assert "高俅设局" not in out["林冲"]["does_not_know"]
    # Input ledger must not be mutated.
    assert ledger["林冲"]["knows"] == []
    assert ledger["林冲"]["does_not_know"] == ["高俅设局"]


def test_apply_changes_new_character_and_dedup():
    ledger = {"林冲": {"knows": ["陆谦是卧底"], "does_not_know": []}}
    out = apply_changes(
        ledger,
        [
            # New character appears for the first time.
            {"character": "鲁智深", "learns": "林冲被发配沧州"},
            # Duplicate learns must not produce duplicate entries.
            {"character": "林冲", "learns": "陆谦是卧底"},
            {"character": "鲁智深", "learns": "林冲被发配沧州"},
        ],
    )
    assert out["鲁智深"]["knows"] == ["林冲被发配沧州"]
    assert out["林冲"]["knows"] == ["陆谦是卧底"]


def test_still_unknown_does_not_demote_known():
    ledger = {"林冲": {"knows": ["高俅设局"], "does_not_know": []}}
    out = apply_changes(
        ledger,
        [
            {"character": "林冲", "still_unknown": "高俅设局"},
            {"character": "林冲", "still_unknown": "妻子已被害"},
        ],
    )
    # Already-known info is never demoted back to does_not_know.
    assert "高俅设局" in out["林冲"]["knows"]
    assert "高俅设局" not in out["林冲"]["does_not_know"]
    # Genuinely unknown info is recorded.
    assert "妻子已被害" in out["林冲"]["does_not_know"]


def test_apply_changes_reader_learns_and_skips_garbage():
    ledger: dict = {}
    out = apply_changes(
        ledger,
        [
            {"character": READER, "learns": "陆谦带着刀来了"},
            # Reader row only uses `knows`; still_unknown for reader is ignored.
            {"character": READER, "still_unknown": "应被忽略"},
            # Malformed entries are skipped, never raise.
            {"character": "", "learns": "无名"},
            {"learns": "没有角色字段"},
            "not-a-dict",
            {"character": "林冲"},
        ],
    )
    assert out[READER]["knows"] == ["陆谦带着刀来了"]
    assert out[READER]["does_not_know"] == []
    assert set(out.keys()) == {READER}


# ---------------------------------------------------------------------------
# serialize_for_prompt (pure)
# ---------------------------------------------------------------------------


def test_serialize_empty_returns_empty():
    assert serialize_for_prompt({}) == ""
    assert serialize_for_prompt({"林冲": {"knows": [], "does_not_know": []}}) == ""


def test_serialize_contains_all_parts():
    ledger = {
        "林冲": {"knows": ["陆谦是卧底"], "does_not_know": ["妻子已被害"]},
        READER: {"knows": ["高俅设局"], "does_not_know": []},
    }
    text = serialize_for_prompt(ledger)
    assert "林冲知道：陆谦是卧底" in text
    assert "林冲不知道：妻子已被害" in text
    assert "读者已知：高俅设局" in text
    # Reader pseudo-name must never leak verbatim.
    assert READER not in text


def test_serialize_budget_capped():
    # Many characters; entry-rich characters must survive truncation.
    ledger = {
        f"角色{i}": {
            "knows": [f"事实{i}-{j}号情报内容较长用于撑预算" for j in range(4)],
            "does_not_know": [f"未知{i}-{j}" for j in range(2)],
        }
        for i in range(10)
    }
    ledger["主角"] = {
        "knows": [f"核心事实{j}" for j in range(12)],
        "does_not_know": ["最大悬念"],
    }
    text = serialize_for_prompt(ledger, max_chars=600)
    assert text
    assert len(text) <= 600
    # Entry-count importance: the richest character is kept first.
    assert "主角知道" in text
    # No dangling half-line: every line is a complete 知道/不知道/读者已知 statement.
    for line in text.splitlines():
        assert line.startswith(("主角", "角色", "读者已知")), line
        assert not line.endswith(("；", "，")), line


# ---------------------------------------------------------------------------
# load/save upsert round-trip against the real DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_save_upsert_roundtrip():
    from app.db.session import async_session_factory
    from app.models.project import Project

    async with async_session_factory() as db:
        project = Project(id=uuid.uuid4(), title="cognition-test")
        db.add(project)
        await db.commit()
        pid = project.id
        try:
            assert await load_ledger(db, pid) == {}

            ledger = {
                "林冲": {"knows": ["陆谦是卧底"], "does_not_know": ["妻子已被害"]},
                READER: {"knows": ["高俅设局"], "does_not_know": []},
            }
            await save_ledger(db, pid, ledger)
            assert await load_ledger(db, pid) == ledger

            # Upsert: existing rows are updated in place, not duplicated.
            ledger["林冲"]["knows"].append("高俅设局")
            await save_ledger(db, pid, ledger)
            loaded = await load_ledger(db, pid)
            assert loaded["林冲"]["knows"] == ["陆谦是卧底", "高俅设局"]
            assert len(loaded) == 2
        finally:
            await db.delete(project)
            await db.commit()
