"""Tests for the book-dossier consolidation layer (app/services/book_dossier.py).

Covers:
  - deterministic MAP aggregations (style + beat fixture cards -> expected stats)
  - stratified sampling spacing
  - proper-noun scrub
  - block rendering size caps
  - metadata_json write discipline (copy + flag_modified) on a fake book
  - full build_dossier against the real DB with a faked LLM: contract shape,
    size caps, persistence, partial-failure tolerance, proper-noun scrub
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import book_dossier as bd


# =========================================================================
# Pure MAP helpers
# =========================================================================

def test_select_stratified_even_spacing() -> None:
    items = list(range(101))
    picked = bd.select_stratified(items, 5)
    assert picked == [0, 25, 50, 75, 100]

    picked10 = bd.select_stratified(list(range(100)), 10)
    assert len(picked10) == 10
    assert picked10[0] == 0 and picked10[-1] == 99
    gaps = [b - a for a, b in zip(picked10, picked10[1:])]
    assert max(gaps) - min(gaps) <= 1  # even spacing


def test_select_stratified_edge_cases() -> None:
    assert bd.select_stratified([1, 2, 3], 30) == [1, 2, 3]
    assert bd.select_stratified([7, 8, 9], 1) == [7]
    assert bd.select_stratified([1, 2], 0) == []
    assert bd.select_stratified([], 5) == []


def test_aggregate_style_stats_counts_and_tolerance() -> None:
    cards = [
        {
            "pov": "第一人称", "tense": "past", "pacing": "快",
            "emotional_register": "冷峻",
            "vocab_tone": ["古风", "硬核"],
            "signature_moves": ["以天气写心"],
            "forbidden_tells": ["璀璨"],
        },
        {
            "pov": "第一人称",
            "vocab_tone": ["古风"],
            "signature_moves": [],
            "forbidden_tells": ["璀璨"],
        },
        {"pov": "第三人称", "vocab_tone": "市井"},  # string instead of list
        "not a dict",  # must be tolerated
    ]
    stats = bd.aggregate_style_stats(cards)
    assert stats["cards"] == 4
    assert stats["pov"][0] == {"value": "第一人称", "count": 2}
    assert {"value": "第三人称", "count": 1} in stats["pov"]
    assert stats["vocab_tone"][0] == {"value": "古风", "count": 2}
    assert {"value": "市井", "count": 1} in stats["vocab_tone"]
    assert stats["forbidden_tells"] == [{"value": "璀璨", "count": 2}]
    assert stats["signature_moves"] == [{"value": "以天气写心", "count": 1}]


def _beat(seq: int, ch: int, **beat) -> dict:
    return {"sequence_id": seq, "chapter_idx": ch, "beat": beat}


def test_aggregate_beat_stats_deterministic() -> None:
    beats = [
        _beat(0, 1, scene_type="开篇", emotional_arc="平静",
              foreshadow="埋下伏笔：神秘玉佩"),
        _beat(1, 1, scene_type="冲突", emotional_arc="紧张"),
        _beat(2, 2, scene_type="高潮", emotional_arc="爆发"),
        _beat(3, 3, scene_type="过渡", foreshadow="回收伏笔：玉佩来历揭晓"),
        _beat(4, 5, scene_type="高潮"),
    ]
    stats = bd.aggregate_beat_stats(beats)

    assert stats["beats"] == 5
    assert stats["chapters"] == 4
    assert {"value": "高潮", "count": 2} in stats["scene_type_distribution"]
    assert {"value": "开篇→冲突", "count": 1} in stats["chapter_beat_patterns"]

    # 高潮间隔(章): climaxes in ch2 and ch5 -> one gap of 3
    assert stats["climax"]["chapters_with_climax"] == 2
    assert stats["climax"]["gaps_sample"] == [3]
    assert stats["climax"]["avg_gap"] == 3.0

    # 铺垫→回收: plant ch1, payoff ch3 -> distance 2
    fs = stats["foreshadow"]
    assert fs["plants"] == 1 and fs["payoffs"] == 1
    assert fs["plant_to_payoff_distance_sample"] == [2]
    assert fs["avg_distance"] == 2.0
    # density: chapters span 1..5, plant in ch1 -> first decile
    assert fs["plant_density_by_decile"][0] == 1
    assert sum(fs["plant_density_by_decile"]) == 1

    # emotional transitions follow sequence order
    assert {"value": "平静→紧张", "count": 1} in stats["arc_transitions"]
    assert {"value": "紧张→爆发", "count": 1} in stats["arc_transitions"]


def test_aggregate_beat_stats_empty_and_bad_rows() -> None:
    stats = bd.aggregate_beat_stats(
        [{"sequence_id": 0, "chapter_idx": None, "beat": "oops"}]
    )
    assert stats["beats"] == 1
    assert stats["chapters"] == 0
    assert stats["climax"]["avg_gap"] is None
    assert stats["foreshadow"]["avg_distance"] is None


# =========================================================================
# Scrub + rendering
# =========================================================================

def test_scrub_proper_nouns() -> None:
    out = bd.scrub_proper_nouns("叶凡在青云宗修炼", ["叶凡", "青云宗", "凡"])
    assert out == "某某在某某修炼"
    # longest-first: 林晚晴 must not be half-scrubbed via 林晚
    out2 = bd.scrub_proper_nouns("林晚晴和林晚同行", ["林晚", "林晚晴"])
    assert out2 == "某某和某某同行"
    # single-char names are skipped, empty text passes through
    assert bd.scrub_proper_nouns("", ["叶凡"]) == ""
    assert bd.scrub_proper_nouns("凡人修仙", ["凡"]) == "凡人修仙"


def test_render_blocks_respect_caps() -> None:
    huge = "长" * 500
    style_data = {
        "profile": {
            **{k: huge for k, _ in bd._STYLE_FIELDS},
            "signature_moves": [huge, huge],
            "forbidden": [huge],
            "evidence_quotes": ["雨点砸在青石板上" * 20, "短句", "另一条"],
        },
        "stats": {},
    }
    block = bd.render_style_block(style_data)
    assert 0 < len(block) <= bd.STYLE_BLOCK_CAP
    assert block.startswith("【风格档案】")

    plot_data = {
        "profile": {k: huge for k, _ in bd._PLOT_FIELDS},
        "stats": {"climax": {"avg_gap": 4.5}, "foreshadow": {"avg_distance": 6}},
    }
    sblock = bd.render_structure_block(plot_data)
    assert 0 < len(sblock) <= bd.STRUCTURE_BLOCK_CAP

    world_data = {"profile": {
        **{k: huge for k, _ in bd._WORLD_FIELDS},
        "design_patterns": [huge],
    }}
    wblock = bd.render_world_block(world_data)
    assert 0 < len(wblock) <= bd.WORLD_BLOCK_CAP

    # failed sections render as empty strings
    assert bd.render_style_block({"error": "x"}) == ""
    assert bd.render_structure_block({}) == ""
    assert bd.render_world_block(None) == ""


def test_write_meta_copies_dict_and_flags(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(bd, "flag_modified", lambda obj, attr: calls.append((obj, attr)))

    original = {"quality_score": {"overall": 8}}
    book = SimpleNamespace(metadata_json=original)
    meta = bd._write_meta(book, dossier={"style_block": ""})

    assert book.metadata_json is not original  # copied, not mutated in place
    assert "dossier" not in original
    assert meta["dossier"] == {"style_block": ""}
    assert meta["quality_score"] == {"overall": 8}
    assert calls == [(book, "metadata_json")]


# =========================================================================
# build_dossier integration (real DB, faked LLM)
# =========================================================================

_STYLE_LLM = {
    "narrative_pov_rules": "第三人称有限视角，场景切换时换段",
    "syntax_rhythm": "短句为主，高潮段落连续短句",
    "dialogue_style": "口语化，对话密度高",
    "sensory_rhetoric": "偏听觉与触觉",
    "emotional_curve": "先抑后扬",
    "opening_patterns": "以动作切入",
    "hook_patterns": "章末留悬念",
    "signature_moves": ["以天气写心"],
    "forbidden": ["璀璨", "心潮澎湃"],
    "evidence_quotes": ["叶凡握紧了拳头" + "，雨还在下" * 20, "刀光一闪"],
}

_PLOT_LLM = {
    "macro_structure": "三卷式，每卷一个大高潮",
    "chapter_beat_template": "开场冲突→升级→章末钩子",
    "conflict_escalation": "层层递进",
    "foreshadow_strategy": "关键节点埋线，3章内回收",
    "payoff_rhythm": "每两章一个小爽点",
}

_WORLD_MAP_LLM = {
    "power_system": ["「主角」所在世界以炼气分九层"],
    "rules_constraints": ["越阶挑战会遭反噬"],
    "organizations": ["A势力垄断功法传承"],
    "geography": ["边缘小城与中央圣地对立"],
    "conflict_sources": ["资源稀缺引发争夺"],
    "proper_nouns": ["叶凡", "青云宗"],
}

_WORLD_MERGE_LLM = {
    "power_system": "九层线性体系，青云宗式门派垄断晋升资源",
    "rules_constraints": "越阶反噬制造实力天花板",
    "organizations": "中央垄断—边缘挑战者结构",
    "geography": "边缘→中心的空间升级路线",
    "conflict_sources": "资源稀缺+阶层固化",
    "design_patterns": ["力量体系分层制造长期爬升目标"],
}


def _fake_llm(fail_tasks: set[str] | None = None, calls: list[str] | None = None):
    fail_tasks = fail_tasks or set()

    async def fake(task_type, user_content, db, *args, **kwargs):
        if calls is not None:
            calls.append(task_type)
        if task_type in fail_tasks:
            raise RuntimeError(f"boom:{task_type}")
        if task_type == "style_consolidation":
            return dict(_STYLE_LLM)
        if task_type == "plot_consolidation":
            return dict(_PLOT_LLM)
        if task_type == "world_arch_extraction":
            return dict(_WORLD_MAP_LLM)
        if task_type == "world_arch_merge":
            return dict(_WORLD_MERGE_LLM)
        raise AssertionError(f"unexpected task_type {task_type}")

    return fake


async def _make_book(db, *, n_slices: int = 6, n_chunks: int = 10) -> str:
    from app.models.decompile import BeatSheetCard, ReferenceBookSlice, StyleProfileCard
    from app.models.project import ReferenceBook, TextChunk

    book = ReferenceBook(
        title="遮天测试书", author="辰东测试", source="upload_txt", status="ready",
        metadata_json={"plot_structure": {"arc_pattern": "英雄之旅"}},
    )
    db.add(book)
    await db.flush()
    book_id = str(book.id)

    for i in range(n_slices):
        ch = i // 2 + 1
        slc = ReferenceBookSlice(
            book_id=book.id, slice_type="scene", chapter_idx=ch, sequence_id=i,
            start_offset=i * 100, end_offset=i * 100 + 99,
            raw_text=f"第{ch}章片段{i}：叶凡抬头看向青云宗的山门。", token_count=30,
        )
        db.add(slc)
        await db.flush()
        db.add(StyleProfileCard(
            book_id=book.id, slice_id=slc.id,
            profile_json={
                "pov": "第三人称" if i % 2 else "第一人称",
                "pacing": "快", "emotional_register": "冷峻",
                "vocab_tone": ["硬核"], "signature_moves": ["以天气写心"],
                "forbidden_tells": ["璀璨"],
            },
        ))
        db.add(BeatSheetCard(
            book_id=book.id, slice_id=slc.id,
            beat_json={
                "scene_type": "高潮" if i == 3 else "冲突",
                "emotional_arc": "紧张",
                "foreshadow": "埋下伏笔" if i == 0 else "",
                "reusable_pattern": "弱者遇挑衅后反杀",
            },
        ))

    for i in range(n_chunks):
        db.add(TextChunk(
            book_id=book.id, chapter_idx=i // 3 + 1, block_idx=i % 3,
            chapter_title=f"第{i // 3 + 1}章", content=f"叶凡在青云宗修炼，第{i}块。",
            char_count=20, sequence_id=i,
        ))
    await db.commit()
    return book_id


async def _delete_book(book_id: str) -> None:
    from sqlalchemy import delete
    from app.db.session import async_session_factory
    from app.models.decompile import BeatSheetCard, ReferenceBookSlice, StyleProfileCard
    from app.models.project import ReferenceBook, TextChunk

    async with async_session_factory() as db:
        for model in (StyleProfileCard, BeatSheetCard, ReferenceBookSlice, TextChunk):
            await db.execute(delete(model).where(model.book_id == book_id))
        await db.execute(delete(ReferenceBook).where(ReferenceBook.id == book_id))
        await db.commit()


_CONTRACT_KEYS = {
    "style_block", "structure_block", "world_block",
    "style_data", "plot_data", "world_data",
    "consolidated_at", "source_counts",
}


@pytest.mark.asyncio
async def test_build_dossier_contract_persistence_and_scrub(monkeypatch) -> None:
    from app.db.session import async_session_factory

    monkeypatch.setattr(bd, "run_structured_prompt", _fake_llm())

    async with async_session_factory() as db:
        book_id = await _make_book(db)
    try:
        result = await bd.build_dossier(book_id)
        assert result["status"] == "done"
        dossier = result["dossier"]

        # Exact contract shape
        assert set(dossier.keys()) == _CONTRACT_KEYS
        assert dossier["source_counts"] == {
            "style_cards": 6, "beat_cards": 6, "chunks_sampled": 10,
        }
        assert isinstance(dossier["consolidated_at"], str)

        # Size caps
        assert len(dossier["style_block"]) <= bd.STYLE_BLOCK_CAP
        assert len(dossier["structure_block"]) <= bd.STRUCTURE_BLOCK_CAP
        assert len(dossier["world_block"]) <= bd.WORLD_BLOCK_CAP
        assert dossier["style_block"] and dossier["structure_block"] and dossier["world_block"]

        # Proper-noun scrub: extraction found 叶凡/青云宗; book title/author too.
        for block in (dossier["style_block"], dossier["structure_block"], dossier["world_block"]):
            for noun in ("叶凡", "青云宗", "遮天测试书", "辰东测试"):
                assert noun not in block
        # evidence quotes are scrubbed + hard-capped at 60 chars
        quotes = dossier["style_data"]["profile"]["evidence_quotes"]
        assert quotes and all(len(q) <= 60 for q in quotes)
        assert all("叶凡" not in q for q in quotes)
        # proper-noun scrub aid is not persisted in the dossier
        assert "proper_nouns" not in dossier["world_data"]

        # Cost discipline: 1 style + 1 plot + 2 world MAP (10 chunks / 8) + 1 merge
        assert result["llm_calls"] == 5
        assert result["llm_calls"] <= 15

        # flag_modified persistence: fresh session sees the dossier
        async with async_session_factory() as db2:
            from app.models.project import ReferenceBook
            book = await db2.get(ReferenceBook, book_id)
            meta = book.metadata_json or {}
            assert set(meta["dossier"].keys()) == _CONTRACT_KEYS
            assert meta["dossier_status"]["state"] == "done"
            assert meta["dossier_status"]["llm_calls"] == 5
            # pre-existing metadata untouched
            assert meta["plot_structure"] == {"arc_pattern": "英雄之旅"}
    finally:
        await _delete_book(book_id)


@pytest.mark.asyncio
async def test_build_dossier_partial_failure_tolerated(monkeypatch) -> None:
    from app.db.session import async_session_factory

    monkeypatch.setattr(
        bd, "run_structured_prompt", _fake_llm(fail_tasks={"style_consolidation"})
    )

    async with async_session_factory() as db:
        book_id = await _make_book(db)
    try:
        result = await bd.build_dossier(book_id)
        assert result["status"] == "done"  # other sections proceeded
        dossier = result["dossier"]
        assert set(dossier.keys()) == _CONTRACT_KEYS
        assert "error" in dossier["style_data"]
        assert dossier["style_block"] == ""
        assert "error" not in dossier["plot_data"]
        assert "error" not in dossier["world_data"]
        assert dossier["structure_block"] and dossier["world_block"]
    finally:
        await _delete_book(book_id)


@pytest.mark.asyncio
async def test_build_dossier_all_sections_failed_marks_error(monkeypatch) -> None:
    from app.db.session import async_session_factory

    monkeypatch.setattr(bd, "run_structured_prompt", _fake_llm(fail_tasks={
        "style_consolidation", "plot_consolidation",
        "world_arch_extraction", "world_arch_merge",
    }))

    async with async_session_factory() as db:
        book_id = await _make_book(db)
    try:
        result = await bd.build_dossier(book_id)
        assert result["status"] == "error"
        assert all(
            "error" in result["dossier"][k]
            for k in ("style_data", "plot_data", "world_data")
        )
    finally:
        await _delete_book(book_id)


@pytest.mark.asyncio
async def test_build_dossier_missing_book() -> None:
    result = await bd.build_dossier(str(uuid.uuid4()))
    assert result["status"] == "error"
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_consolidate_style_requires_cards() -> None:
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        with pytest.raises(ValueError, match="no style cards"):
            await bd.consolidate_style(str(uuid.uuid4()), db)
        with pytest.raises(ValueError, match="no beat cards"):
            await bd.consolidate_plot(str(uuid.uuid4()), db)
