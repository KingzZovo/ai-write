"""Tests for worldview architecture extraction (app/services/worldview_extractor.py)."""

from __future__ import annotations

import uuid

import pytest

from app.services import book_dossier as bd
from app.services import worldview_extractor as wx


def test_merge_candidates_dedupes_preserving_order_and_caps() -> None:
    out1 = {
        "power_system": ["炼气分九层", "越阶挑战遭反噬"],
        "rules_constraints": ["灵气浓度决定上限"],
        "organizations": [],
        "geography": ["边缘小城"],
        "conflict_sources": ["资源稀缺"],
        "proper_nouns": ["叶凡", "青云宗"],
    }
    out2 = {
        "power_system": ["炼气分九层", "金丹为分水岭"],  # first is a dup
        "conflict_sources": ["资源稀缺", "阶层固化"],
        "proper_nouns": ["青云宗", "紫霄剑"],  # dup + new
    }
    merged, nouns = wx.merge_candidates([out1, "not a dict", out2])

    assert merged["power_system"] == ["炼气分九层", "越阶挑战遭反噬", "金丹为分水岭"]
    assert merged["conflict_sources"] == ["资源稀缺", "阶层固化"]
    assert merged["organizations"] == []
    assert nouns == ["叶凡", "青云宗", "紫霄剑"]

    # per-field cap
    many = [{"power_system": [f"要点{i}" for i in range(60)]}]
    capped, _ = wx.merge_candidates(many)
    assert len(capped["power_system"]) == wx.MAX_CANDIDATES_PER_FIELD


def test_stratified_sampling_spacing_over_chunks() -> None:
    chunks = [f"c{i}" for i in range(200)]
    sampled = bd.select_stratified(chunks, wx.SAMPLE_CHUNKS)
    assert len(sampled) == wx.SAMPLE_CHUNKS
    assert sampled[0] == "c0" and sampled[-1] == "c199"
    idxs = [int(s[1:]) for s in sampled]
    gaps = [b - a for a, b in zip(idxs, idxs[1:])]
    assert max(gaps) - min(gaps) <= 1  # evenly spaced


@pytest.mark.asyncio
async def test_extract_batches_merges_and_scrubs(monkeypatch) -> None:
    from app.db.session import async_session_factory
    from app.models.project import ReferenceBook, TextChunk

    calls: list[str] = []

    async def fake(task_type, user_content, db, *args, **kwargs):
        calls.append(task_type)
        if task_type == "world_arch_extraction":
            return {
                "power_system": ["「主角」炼气分九层"],
                "rules_constraints": ["越阶反噬"],
                "organizations": ["A势力垄断传承"],
                "geography": ["边缘与中心对立"],
                "conflict_sources": ["资源稀缺"],
                "proper_nouns": ["叶凡", "青云宗"],
            }
        assert task_type == "world_arch_merge"
        # merge output leaks a proper noun on purpose -> must be scrubbed
        return {
            "power_system": "青云宗式九层线性体系",
            "rules_constraints": "越阶反噬制造天花板",
            "organizations": "垄断—挑战者结构",
            "geography": "边缘→中心升级路线",
            "conflict_sources": "资源稀缺",
            "design_patterns": ["分层体系制造爬升目标", "叶凡式底层逆袭起点"],
        }

    monkeypatch.setattr(bd, "run_structured_prompt", fake)

    async with async_session_factory() as db:
        book = ReferenceBook(title="世界观测试书", source="upload_txt", status="ready")
        db.add(book)
        await db.flush()
        book_id = str(book.id)
        for i in range(10):
            db.add(TextChunk(
                book_id=book.id, chapter_idx=1, block_idx=i,
                content=f"第{i}块内容", char_count=10, sequence_id=i,
            ))
        await db.commit()

    try:
        counter = bd.LLMCallCounter()
        result = await wx.extract(book_id, counter=counter)

        # 10 chunks -> all sampled -> 2 MAP batches (8+2) + 1 merge
        assert calls == ["world_arch_extraction", "world_arch_extraction", "world_arch_merge"]
        assert counter.total == 3
        assert result["chunks_sampled"] == 10
        assert result["proper_nouns"] == ["叶凡", "青云宗"]

        profile = result["profile"]
        assert "青云宗" not in profile["power_system"]  # scrubbed
        assert all("叶凡" not in p for p in profile["design_patterns"])
        assert profile["conflict_sources"] == "资源稀缺"
    finally:
        from sqlalchemy import delete
        async with async_session_factory() as db:
            await db.execute(delete(TextChunk).where(TextChunk.book_id == book_id))
            await db.execute(delete(ReferenceBook).where(ReferenceBook.id == book_id))
            await db.commit()


@pytest.mark.asyncio
async def test_extract_raises_without_chunks() -> None:
    with pytest.raises(ValueError, match="no text chunks"):
        await wx.extract(str(uuid.uuid4()))
