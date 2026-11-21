"""对照页必须能读懂"两条臂不是同一个东西"(2026-11-17)。

用户反馈:页面里两栏看起来一模一样,因为两条臂其实都是 gemini(只差阈值),
而页面没写模型与设置。这里锁死:
- 章标题要出现在标题行(titles.json);
- 每侧的目标字数来自各自的 continuity.json,不写死;
- 顶部配置表要写出两条臂的标签与目录名。
"""

from __future__ import annotations

import json
import os

from scripts.quality_eval.sbs_report import build, detected_target


def _write(path: str, name: str, text: str) -> None:
    with open(os.path.join(path, name), "w", encoding="utf-8") as handle:
        handle.write(text)


def _run_dir(base: str, name: str, *, chapters: dict[int, str], titles: dict[int, str],
             target: int, findings: int = 0) -> str:
    path = os.path.join(base, name)
    os.makedirs(path, exist_ok=True)
    for idx, text in chapters.items():
        _write(path, f"ch{idx:02d}.txt", text)
    with open(os.path.join(path, "titles.json"), "w", encoding="utf-8") as handle:
        json.dump({f"ch{i:02d}.txt": t for i, t in titles.items()}, handle, ensure_ascii=False)
    with open(os.path.join(path, "continuity.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "chapters": len(chapters),
            "word_counts": {f"ch{i:02d}.txt": {"chars": len(t), "target": target,
                                               "ratio": round(len(t) / target, 3)}
                            for i, t in chapters.items()},
            "findings": [{"check": "x", "chapter": "ch01.txt"}] * findings,
            "violations": findings,
        }, handle, ensure_ascii=False)
    return path


def test_page_shows_titles_targets_and_both_arm_labels(tmp_path):
    base = str(tmp_path)
    old = _run_dir(base, "old", chapters={1: "甲" * 1000, 2: "乙" * 900},
                   titles={1: "第一章 碗底的信封", 2: "第二章 顶针与八百块"}, target=1200)
    new = _run_dir(base, "new", chapters={1: "丙" * 1900, 2: "丁" * 2100},
                   titles={1: "第一章 碗底的信封", 2: "第二章 顶针与八百块"}, target=2000)

    page = build(old, new, old_label="gemini-3.1-pro · 目标 1200 字",
                 new_label="deepseek-v4-flash · 目标 2000 字")

    assert "第一章 碗底的信封" in page
    assert "gemini-3.1-pro · 目标 1200 字" in page
    assert "deepseek-v4-flash · 目标 2000 字" in page
    assert "old" in page and "new" in page           # 目录名(来源可追溯)
    assert "<td>1200</td>" in page and "<td>2000</td>" in page  # 每侧目标
    # 比值按各自目标算:1900/2000 = 0.95(若错用 1200 会是 1.58)
    assert ">0.95<" in page
    assert ">1.58<" not in page


def test_detected_target_reads_side_specific_value(tmp_path):
    base = str(tmp_path)
    path = _run_dir(base, "x", chapters={1: "甲" * 100}, titles={1: "t"}, target=2345)
    assert detected_target(path) == 2345
    assert detected_target(os.path.join(base, "missing")) is None