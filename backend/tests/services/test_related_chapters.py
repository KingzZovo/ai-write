"""C3 / F4: deterministic related-chapter recall + secondary-cast roster.

Pure-logic tests pin the restraint gates (min-total-chapters, min-signals),
the dedup/ranking, substring-safe appearance counting, the recall-gap reminder,
the render budget contracts, and the ContextPack injection tripwire.
"""

from __future__ import annotations

from app.services.character_roster import (
    count_appearances,
    outline_tokens,
    render_roster_block,
)
from app.services.related_chapters import (
    rank_related_chapters,
    render_recall_block,
)


# --- count_appearances ------------------------------------------------------


def test_count_appearances_basic():
    text = "林惊蛰走进来。林惊蛰看了一眼。"
    assert count_appearances(text, {"林惊蛰"}) == {"林惊蛰": 2}


def test_count_appearances_substring_names_no_double_count():
    # "林惊蛰" contains "林惊"; the shorter name must not recount inside it.
    text = "林惊蛰出现了三次：林惊蛰、林惊蛰。"
    counts = count_appearances(text, {"林惊蛰", "林惊"})
    assert counts.get("林惊蛰") == 3
    assert "林惊" not in counts  # fully shadowed by the longer name


def test_count_appearances_ignores_single_char_names():
    assert count_appearances("甲乙丙甲", {"甲"}) == {}  # len < 2 dropped


def test_outline_tokens_min_len():
    toks = outline_tokens("主角前往北境秘境探查古老封印", min_len=4)
    assert all(len(t) >= 4 for t in toks)
    assert any("封印" in t for t in toks)


# --- rank_related_chapters (restraint gates) --------------------------------


def _hit(ch, reason="r"):
    return {"chapter": ch, "reason": reason}


def test_rank_returns_empty_below_min_total_chapters():
    hits = [_hit(1), _hit(2), _hit(3)]
    assert rank_related_chapters(20, hits, [], [], min_total_chapters=30) == []


def test_rank_returns_empty_below_min_signals():
    # 1 signal, min_signals=2 -> nothing
    assert rank_related_chapters(100, [_hit(5)], [], [], min_signals=2) == []


def test_rank_dedups_and_counts_signals():
    fs = [_hit(5, "伏笔A"), _hit(7, "伏笔B")]
    cast = [_hit(5, "角色X")]
    ranked = rank_related_chapters(100, fs, cast, [], min_signals=2)
    # chapter 5 has 2 signals -> ranks first
    assert ranked[0]["chapter"] == 5
    assert ranked[0]["signals"] == 2
    assert set(ranked[0]["reasons"]) == {"伏笔A", "角色X"}
    assert {e["chapter"] for e in ranked} == {5, 7}


def test_rank_caps_top_k():
    fs = [_hit(i, f"r{i}") for i in range(1, 20)]
    ranked = rank_related_chapters(100, fs, [], [], min_signals=2, top_k=6)
    assert len(ranked) == 6


# --- render budget contracts ------------------------------------------------


def test_render_recall_budget_and_empty():
    items = [
        {"chapter": 5, "reasons": ["伏笔A"], "signals": 2, "summary": "某章摘要"},
        {"chapter": 7, "reasons": ["角色X"], "signals": 1, "summary": ""},
    ]
    block = render_recall_block(items, max_chars=600)
    assert 0 < len(block) <= 600
    assert "相关历史章回读" in block and "[CH-5]" in block
    assert render_recall_block([]) == ""


def test_render_roster_gap_reminder_and_budget():
    rows = [
        {"character_name": "老王", "last_seen_chapter": 3},   # gap 50 > 10 -> reminder
        {"character_name": "小李", "last_seen_chapter": 50},  # recent
    ]
    block = render_roster_block(rows, current_idx=53, max_chars=600)
    assert 0 < len(block) <= 600
    assert "老王" in block and "回读" in block          # long-absent reminder
    # most-recent-first ordering
    assert block.index("小李") < block.index("老王")


def test_render_roster_empty():
    assert render_roster_block([], current_idx=10) == ""


# --- injection tripwire -----------------------------------------------------


def test_context_pack_injects_related_recall():
    from app.services.context_pack import ContextPack

    pack = ContextPack()
    pack.related_chapter_recall = "【相关历史章回读（确定性反查）】\n- 第5章：伏笔A"
    out = pack.to_system_prompt()
    assert "相关历史章回读" in out
    assert "第5章" in out

    assert "相关历史章回读" not in ContextPack().to_system_prompt()


def test_roster_wired_into_recompute_task():
    """C3: the C2 recompute task must populate the roster from its chapter pull
    (one read serves both features). Source tripwire -- the original gap was an
    unwired roster."""
    import inspect

    from app.tasks import style_tasks

    src = inspect.getsource(style_tasks)
    assert "update_roster_for_chapter(" in src
