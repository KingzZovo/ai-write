"""回归:长跑曲线分析(纯函数,不联网)。

要证的三件事:
1. 篇幅比按"章字符数 / 章节目标"算,并且**趋势**用前 1/3 与后 1/3 的中位比来表达;
2. 状态与终态提取分别统计 —— "章写成了"不等于"台账跟上了";
3. `extraction` 字段既可能是 dict 也可能是 repr 字符串,都要能读。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.quality_eval.longrun import curve, load, summarize


def _row(index: int, chars: int, *, status: str = "ok", extraction=None) -> dict:
    return {"index": index, "status": status, "chars": chars, "extraction": extraction}


def test_ratio_and_curve_are_sorted_by_chapter():
    rows = [_row(2, 600), _row(1, 1200)]
    points = curve(rows, 1200)
    assert [p["index"] for p in points] == [1, 2]
    assert [p["ratio"] for p in points] == [1.0, 0.5]


def test_tercile_medians_expose_decay():
    # 前 3 章满额,后 3 章只剩一半 → 后 1/3 中位比必须明显更低
    rows = [_row(i, 1200) for i in (1, 2, 3)] + [_row(i, 600) for i in (4, 5, 6)]
    summary = summarize(rows, 1200)
    assert summary["first_third_median"] == 1.0
    assert summary["last_third_median"] == 0.5
    assert summary["below_70pct"] == [4, 5, 6]


def test_statuses_and_terminals_are_counted_separately():
    rows = [
        _row(1, 1200, extraction={"entity": {"status": "ok"}, "cognition": {"status": "completed"}}),
        _row(2, 900, status="error", extraction={"entity": {"status": "failed"}}),
    ]
    summary = summarize(rows, 1200)
    assert summary["statuses"] == {"ok": 1, "error": 1}
    assert summary["terminals"] == {"entity:ok": 1, "cognition:completed": 1, "entity:failed": 1}
    assert summary["failed"] == [2]


def test_extraction_repr_string_is_parsed():
    # 探针写 chapters.json 时 extraction 可能是 Python repr(单引号)
    rows = [_row(1, 1200, extraction="{&#39;entity&#39;: {&#39;status&#39;: &#39;ok&#39;}}")]
    rows[0]["extraction"] = "{'entity': {'status': 'ok'}}"
    assert summarize(rows, 1200)["terminals"] == {"entity:ok": 1}


def test_load_returns_empty_for_missing_or_broken_file(tmp_path: Path):
    assert load(tmp_path) == []
    (tmp_path / "chapters.json").write_text("{not json", encoding="utf-8")
    assert load(tmp_path) == []


def test_load_reads_a_real_file(tmp_path: Path):
    (tmp_path / "chapters.json").write_text(
        json.dumps([_row(1, 1100)]), encoding="utf-8")
    assert [r["index"] for r in load(tmp_path)] == [1]
