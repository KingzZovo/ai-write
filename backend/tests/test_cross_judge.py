"""回归:交叉盲评的比对逻辑(纯函数,不联网)。

要证的三件事:
1. 只统计**两方都判过**的条目 —— 覆盖率差异不能被当成分歧;
2. 一致率与分歧清单按逐条判定算,分歧项带上两方的原句便于人工复核;
3. 只有一方判过的篇目单独列出(说明"结论不可比",而不是"一致")。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.quality_eval.cross_judge import compare, verdict_map


def _record(stem: str, verdicts: list[tuple[str, bool]], *, status: str = "ok") -> dict:
    rows = [{"id": item_id, "delivered": delivered, "verified": delivered,
             "locatable": "verbatim" if delivered else "missing", "quote": f"{stem}:{item_id}"}
            for item_id, delivered in verdicts]
    return {"stem": stem, "case_id": stem.split("-")[0], "requested_model": "m",
            "chars": 100, "status": status, "items": len(rows),
            "delivered": sum(1 for r in rows if r["delivered"]),
            "unverified": 0, "verdicts": rows}


def _write(run_dir: Path, name: str, records: list[dict]) -> None:
    (run_dir / f"judged.{name}.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    _write(tmp_path, "a", [
        _record("real_ferry-scene_writer", [("o1", True), ("o2", False)]),
        _record("real_stall-scene_writer", [("o1", True)]),
    ])
    _write(tmp_path, "b", [
        _record("real_ferry-scene_writer", [("o1", True), ("o2", True)]),
        _record("real_stall-scene_writer", [("o1", True), ("o3", False)]),
    ])
    return tmp_path


def test_verdict_map_skips_failed_records():
    records = [_record("x-scene_writer", [("o1", True)]),
               _record("y-scene_writer", [("o1", True)], status="error")]
    assert set(verdict_map(records)) == {("x-scene_writer", "o1")}


def test_only_commonly_judged_items_are_compared(run_dir: Path):
    report = compare(run_dir, "a", "b")
    # o1×2 篇 + o2 一篇 = 3 条共同条目;o3 只有 b 判过
    assert report["compared_items"] == 3
    assert report["agreed"] == 2          # o1/o1 一致 + o2 分歧里的另一条
    assert report["disagreed"] == 1
    assert report["only_b"] == ["real_stall-scene_writer"]
    assert report["only_a"] == []


def test_disagreement_carries_both_quotes_for_human_adjudication(run_dir: Path):
    report = compare(run_dir, "a", "b")
    row = report["disagreements"][0]
    assert row["item"] == "o2"
    assert row["a_delivered"] is False and row["b_delivered"] is True
    assert row["a_quote"] and row["b_quote"]


def test_agreement_rate_is_none_when_nothing_compared(tmp_path: Path):
    _write(tmp_path, "a", [_record("k-scene_writer", [("o1", True)])])
    report = compare(tmp_path, "a", "missing-judge")
    assert report["compared_items"] == 0
    assert report["agreement_rate"] is None
    assert report["judges"]["missing-judge"] is None


def test_per_judge_summary_is_included(run_dir: Path):
    report = compare(run_dir, "a", "b")
    # a: (o1✓ o2✗) + (o1✓) → 条目 3、交付 2 → 0.6667
    # b: (o1✓ o2✓) + (o1✓ o3✗) → 条目 4、交付 3 → 0.75
    assert report["judges"]["a"]["delivered"] == 2
    assert report["judges"]["b"]["delivered"] == 3
    assert report["judges"]["a"]["delivered_rate"] == round(2 / 3, 4)
    assert report["judges"]["b"]["delivered_rate"] == 0.75

# ---------------------------------------------------------------------------
# 评审输出容错(实测:claude-sonnet-4-6 会把对白引号原样写进 JSON 值)
# ---------------------------------------------------------------------------


def test_unescaped_inner_quotes_are_repaired_not_fatal():
    """实测样本:值里带未转义的对白引号 → 严格 JSON 直接 JSONDecodeError。"""
    from scripts.quality_eval.judge import _escape_inner_quotes, _parse

    broken = ('[{"id": "loss_reaction", "delivered": true, "quote": '
              '"许舟低声道:"这回真成穷光蛋了。"林叶没说话。"}]')
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    parsed = _parse(broken)
    assert parsed[0]["id"] == "loss_reaction" and parsed[0]["delivered"] is True
    assert '"这回真成穷光蛋了。"' in parsed[0]["quote"]
    # 已经正确转义的不能被动坏(严格路径优先)。
    assert _escape_inner_quotes('{"a": "b\\"c"}') == '{"a": "b\\"c"}'


def test_keys_are_not_mistaken_for_inner_quotes():
    """对象键后面紧跟 `:`,必须仍被当作字符串结束,否则键会被转义、整份输出作废。"""
    from scripts.quality_eval.judge import _escape_inner_quotes

    text = '[{"id": "a", "delivered": false, "quote": ""}]'
    assert _escape_inner_quotes(text) == text
