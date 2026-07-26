"""Tests for author-level dossier consolidation (book_dossier.consolidate_author).

Covers:
  - deterministic cross-book comparison helpers (compare_style_stats /
    compare_plot_stats)
  - render_author_block 单书特例 rendering + caps
  - consolidate_author merges from existing book dossiers WITHOUT rebuilding
    them (pyramid principle), 3 LLM merge calls, contract shape + caps +
    proper-noun scrub (titles/author), book_labels map, persistence
  - missing book dossiers are built first via build_dossier (faked)
  - status marker transitions (running observed mid-merge, done/error final)
  - unknown author / all-merges-failed error paths
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select, text

from app.db.session import async_session_factory
from app.models.author_dossier import AuthorDossier
from app.models.project import ReferenceBook
from app.services import book_dossier as bd


# DDL mirrors alembic a1001920 exactly (idempotent), so the orchestrator's
# later `alembic upgrade` no-ops cleanly on a dev DB where tests ran first.
_AUTHOR_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS author_dossiers (
  id uuid PRIMARY KEY,
  author varchar(200) NOT NULL UNIQUE,
  status_json jsonb,
  dossier_json jsonb,
  source_book_ids_json jsonb,
  created_at timestamptz,
  updated_at timestamptz
);
"""


async def _ensure_author_table() -> None:
    async with async_session_factory() as db:
        await db.execute(text(_AUTHOR_TABLE_DDL))
        await db.commit()


# =========================================================================
# Deterministic cross-book comparison helpers
# =========================================================================

def test_compare_style_stats_consistent_vs_divergent() -> None:
    stats_a = {
        "pov": [{"value": "第三人称", "count": 5}],
        "pacing": [{"value": "快", "count": 4}, {"value": "慢", "count": 1}],
        "vocab_tone": [{"value": "古风", "count": 3}],
    }
    stats_b = {
        "pov": [{"value": "第三人称", "count": 7}],
        "pacing": [{"value": "慢", "count": 6}],
        "vocab_tone": [{"value": "古风", "count": 2}],
    }
    cmp = bd.compare_style_stats([("书1", stats_a), ("书2", stats_b)])
    assert cmp["consistent"]["pov"] == "第三人称"
    assert cmp["consistent"]["vocab_tone"] == "古风"
    assert cmp["divergent"]["pacing"] == {"书1": "快", "书2": "慢"}
    # merged frequency tables sum counts across books
    assert {"value": "第三人称", "count": 12} in cmp["merged_top"]["pov"]
    assert cmp["merged_top"]["pacing"][0] == {"value": "慢", "count": 7}


def test_compare_style_stats_single_book_is_consistent() -> None:
    cmp = bd.compare_style_stats([("书1", {"pov": [{"value": "第一人称", "count": 2}]})])
    assert cmp["consistent"]["pov"] == "第一人称"
    assert cmp["divergent"] == {}


def test_compare_plot_stats_ranges() -> None:
    stats_a = {
        "climax": {"avg_gap": 3.0},
        "foreshadow": {"avg_distance": 2.0},
        "scene_type_distribution": [{"value": "冲突", "count": 9}],
    }
    stats_b = {
        "climax": {"avg_gap": 6.0},
        "foreshadow": {"avg_distance": 2.5},
        "scene_type_distribution": [{"value": "冲突", "count": 4}],
    }
    cmp = bd.compare_plot_stats([("书1", stats_a), ("书2", stats_b)])
    assert cmp["per_book"]["书1"]["avg_climax_gap"] == 3.0
    assert cmp["per_book"]["书2"]["top_scene_types"] == ["冲突"]
    assert cmp["summary"]["climax_gap_range"] == [3.0, 6.0]
    assert cmp["summary"]["climax_gap_consistent"] is False
    assert cmp["summary"]["foreshadow_distance_consistent"] is True


def test_render_author_block_appends_book_specific_and_caps() -> None:
    huge = "长" * 500
    data = {
        "profile": {
            **{k: huge for k, _ in bd._STYLE_FIELDS},
            "signature_moves": [huge],
            "book_specific": [
                {"book": "书1", "note": "偏第一人称插叙" * 30},
                {"book": "书2", "note": "章末诗句收尾"},
                "裸字符串特例",
            ],
        },
    }
    block = bd.render_author_block(bd.render_style_block, data, bd.AUTHOR_STYLE_BLOCK_CAP)
    assert 0 < len(block) <= bd.AUTHOR_STYLE_BLOCK_CAP
    assert "单书特例（书1）" in block
    # empty data renders empty
    assert bd.render_author_block(bd.render_style_block, {}, 1500) == ""


# =========================================================================
# consolidate_author (real DB, faked LLM / faked build_dossier)
# =========================================================================

_STYLE_PROFILE = {
    "narrative_pov_rules": "第三人称有限视角",
    "syntax_rhythm": "短句为主",
    "dialogue_style": "口语化",
    "signature_moves": ["以天气写心"],
    "forbidden": ["璀璨"],
    "evidence_quotes": ["刀光一闪"],
}

_PLOT_PROFILE = {
    "macro_structure": "三卷式",
    "chapter_beat_template": "冲突→升级→钩子",
    "conflict_escalation": "层层递进",
    "foreshadow_strategy": "3章内回收",
    "payoff_rhythm": "两章一小爽",
}

_WORLD_PROFILE = {
    "power_system": "九层线性体系",
    "rules_constraints": "越阶反噬",
    "organizations": "中央垄断结构",
    "geography": "边缘→中心",
    "conflict_sources": "资源稀缺",
    "design_patterns": ["力量体系分层制造爬升目标"],
}


def _book_dossier_payload(pov: str = "第三人称") -> dict:
    return {
        "style_block": "【风格档案】\n视角：第三人称",
        "structure_block": "【剧情架构】\n宏观结构：三卷式",
        "world_block": "【世界观架构】\n力量体系：九层",
        "style_data": {
            "profile": dict(_STYLE_PROFILE),
            "stats": {"pov": [{"value": pov, "count": 5}],
                      "pacing": [{"value": "快", "count": 4}]},
        },
        "plot_data": {
            "profile": dict(_PLOT_PROFILE),
            "stats": {"climax": {"avg_gap": 3.0},
                      "foreshadow": {"avg_distance": 2.0},
                      "scene_type_distribution": [{"value": "冲突", "count": 9}]},
        },
        "world_data": {"profile": dict(_WORLD_PROFILE), "chunks_sampled": 10},
        "consolidated_at": "2026-07-26T00:00:00+00:00",
        "source_counts": {"style_cards": 6, "beat_cards": 6, "chunks_sampled": 10},
    }


def _author_merge_llm(author: str, titles: list[str],
                      fail_tasks: set[str] | None = None,
                      calls: list[str] | None = None,
                      on_call=None):
    """Fake merge LLM; outputs deliberately leak the author name and a real
    book title so the scrub discipline is exercised."""
    fail_tasks = fail_tasks or set()
    leak = f"{author}在《{titles[0]}》中" if titles else ""

    async def fake(task_type, user_content, db, *args, **kwargs):
        if calls is not None:
            calls.append(task_type)
        if on_call is not None:
            await on_call(task_type, user_content)
        if task_type in fail_tasks:
            raise RuntimeError(f"boom:{task_type}")
        if task_type == "author_style_merge":
            return {
                **_STYLE_PROFILE,
                "sensory_rhetoric": f"偏听觉触觉，{leak}尤为明显",
                "book_specific": [{"book": "书1", "note": f"{leak}偏第一人称插叙"}],
            }
        if task_type == "author_plot_merge":
            return {
                **_PLOT_PROFILE,
                "book_specific": [{"book": "书2", "note": "该书为单卷短篇结构"}],
            }
        if task_type == "author_world_merge":
            return {
                **_WORLD_PROFILE,
                "book_specific": [{"book": "书1", "note": "该书无修炼体系"}],
            }
        raise AssertionError(f"unexpected task_type {task_type}")

    return fake


async def _make_author_books(author: str, titles: list[str],
                             with_dossier: list[bool] | None = None) -> list[str]:
    await _ensure_author_table()
    with_dossier = with_dossier or [True] * len(titles)
    ids: list[str] = []
    async with async_session_factory() as db:
        for i, title in enumerate(titles):
            meta = {}
            if with_dossier[i]:
                meta = {
                    "dossier": _book_dossier_payload(),
                    "dossier_status": {"state": "done", "llm_calls": 5},
                }
            book = ReferenceBook(
                title=title, author=author, source="upload_txt",
                status="ready", metadata_json=meta,
            )
            db.add(book)
            await db.flush()
            ids.append(str(book.id))
        await db.commit()
    return ids


async def _cleanup_author(author: str) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(ReferenceBook).where(ReferenceBook.author == author))
        await db.execute(delete(AuthorDossier).where(AuthorDossier.author == author))
        await db.commit()


async def _load_row(author: str) -> AuthorDossier | None:
    async with async_session_factory() as db:
        result = await db.execute(
            select(AuthorDossier).where(AuthorDossier.author == author)
        )
        return result.scalar_one_or_none()


_CONTRACT_KEYS = {
    "style_block", "structure_block", "world_block",
    "style_data", "plot_data", "world_data",
    "consolidated_at", "source_counts",
}


@pytest.mark.asyncio
async def test_consolidate_author_merges_without_rebuilding(monkeypatch) -> None:
    author = "作者甲-合并测试"
    titles = ["甲书一号", "甲书二号"]
    calls: list[str] = []
    seen_running: list[str] = []

    async def on_call(task_type, user_content):
        # status marker must be "running" while merges execute
        row = await _load_row(author)
        seen_running.append((row.status_json or {}).get("state"))
        # pyramid inputs: labeled 《书N》 payloads, never raw cards
        assert "书1" in user_content and "书2" in user_content

    async def forbidden_build(book_id, db=None):
        raise AssertionError("build_dossier must not run when dossiers exist")

    monkeypatch.setattr(bd, "build_dossier", forbidden_build)
    monkeypatch.setattr(
        bd, "run_structured_prompt",
        _author_merge_llm(author, titles, calls=calls, on_call=on_call),
    )

    book_ids = await _make_author_books(author, titles)
    try:
        result = await bd.consolidate_author(author)
        assert result["status"] == "done", result
        assert result["built_books"] == []
        # cost discipline: exactly 3 merge calls when book dossiers exist
        assert result["llm_calls"] == 3
        assert calls == [
            "author_style_merge", "author_plot_merge", "author_world_merge",
        ]
        assert seen_running == ["running"] * 3

        dossier = result["dossier"]
        # exact contract shape (mirrors the book dossier contract)
        assert set(dossier.keys()) == _CONTRACT_KEYS
        assert dossier["source_counts"] == {
            "books": 2, "style_cards": 12, "beat_cards": 12,
        }
        assert isinstance(dossier["consolidated_at"], str)

        # author-tier caps
        assert 0 < len(dossier["style_block"]) <= bd.AUTHOR_STYLE_BLOCK_CAP
        assert 0 < len(dossier["structure_block"]) <= bd.AUTHOR_STRUCTURE_BLOCK_CAP
        assert 0 < len(dossier["world_block"]) <= bd.AUTHOR_WORLD_BLOCK_CAP

        # 作者惯用 body + 单书特例 lines with neutral 《书N》 labels
        assert "单书特例（书1）" in dossier["style_block"]
        assert "单书特例（书2）" in dossier["structure_block"]

        # proper-noun discipline: author name + all book titles scrubbed
        for block in (dossier["style_block"], dossier["structure_block"],
                      dossier["world_block"]):
            assert author not in block
            for title in titles:
                assert title not in block

        # book_labels map real titles back to the 《书N》 placeholders
        labels = dossier["style_data"]["book_labels"]
        assert {v["title"] for v in labels.values()} == set(titles)
        assert set(labels) == {"书1", "书2"}
        assert dossier["plot_data"]["book_labels"] == labels

        # persistence: fresh session sees dossier + done marker + sources
        row = await _load_row(author)
        assert set(row.dossier_json.keys()) == _CONTRACT_KEYS
        assert row.status_json["state"] == "done"
        assert row.status_json["llm_calls"] == 3
        assert sorted(row.source_book_ids_json) == sorted(book_ids)
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_consolidate_author_builds_missing_book_dossiers_first(monkeypatch) -> None:
    author = "作者乙-补建测试"
    titles = ["乙书一号", "乙书二号"]
    built: list[str] = []

    async def fake_build(book_id, db=None):
        built.append(str(book_id))
        assert db is not None  # shares the consolidation session
        book = await db.get(ReferenceBook, str(book_id))
        bd._write_meta(
            book,
            dossier=_book_dossier_payload(),
            dossier_status={"state": "done", "llm_calls": 5},
        )
        await db.commit()
        return {"status": "done", "llm_calls": 5}

    monkeypatch.setattr(bd, "build_dossier", fake_build)
    monkeypatch.setattr(
        bd, "run_structured_prompt", _author_merge_llm(author, titles),
    )

    book_ids = await _make_author_books(author, titles, with_dossier=[True, False])
    try:
        result = await bd.consolidate_author(author)
        assert result["status"] == "done", result
        # only the dossier-less book was built; the ready one was reused
        assert built == [book_ids[1]]
        assert result["built_books"] == [book_ids[1]]
        assert result["dossier"]["source_counts"]["books"] == 2
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_consolidate_author_unknown_author_errors() -> None:
    await _ensure_author_table()
    result = await bd.consolidate_author("不存在的作者-测试")
    assert result["status"] == "error"
    assert "no reference books" in result["error"]
    result_blank = await bd.consolidate_author("  ")
    assert result_blank["status"] == "error"


@pytest.mark.asyncio
async def test_consolidate_author_all_merges_failed_marks_error(monkeypatch) -> None:
    author = "作者丙-失败测试"
    titles = ["丙书一号"]
    monkeypatch.setattr(
        bd, "run_structured_prompt",
        _author_merge_llm(author, titles, fail_tasks={
            "author_style_merge", "author_plot_merge", "author_world_merge",
        }),
    )
    await _make_author_books(author, titles)
    try:
        result = await bd.consolidate_author(author)
        assert result["status"] == "error"
        assert all(
            "error" in result["dossier"][k]
            for k in ("style_data", "plot_data", "world_data")
        )
        row = await _load_row(author)
        assert row.status_json["state"] == "error"
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_consolidate_author_tolerates_partial_section_failure(monkeypatch) -> None:
    author = "作者丁-局部失败"
    titles = ["丁书一号", "丁书二号"]
    monkeypatch.setattr(
        bd, "run_structured_prompt",
        _author_merge_llm(author, titles, fail_tasks={"author_style_merge"}),
    )
    await _make_author_books(author, titles)
    try:
        result = await bd.consolidate_author(author)
        assert result["status"] == "done"  # other sections proceeded
        dossier = result["dossier"]
        assert "error" in dossier["style_data"]
        assert dossier["style_block"] == ""
        assert dossier["structure_block"] and dossier["world_block"]
    finally:
        await _cleanup_author(author)
