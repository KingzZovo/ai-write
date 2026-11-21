"""探针的每章目标字数必须真的传到生产链(2026-11-17)。

用户口径是"单章至少 2000 字"。踩过的坑:探针把 `CHAPTERS` 里的 `words` 改成 2000
只影响落库的 `chapter.target_word_count`,而调用 `orchestrate_chapter_stream` 时
**另有一处写死 `target_words=1200`** —— 于是分场规划出来的 `chapter_target` 仍是
1200,实测每章只有 1326–1633 字,看起来像"模型写不够",其实是目标没传对。

这里锁死三件事:默认值是 2000、`--words` 能覆盖、调用点用的是本章真实目标。
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.quality_eval import continuity_probe as cp


def test_default_chapter_target_is_2000():
    assert cp.CHAPTER_TARGET_WORDS == 2000
    specs = cp._chapter_specs(5)
    assert [spec["words"] for spec in specs] == [2000] * 5


def test_chapter_specs_cover_long_runs_without_regressing_to_the_old_default():
    """规模演练章(>24)也必须是同一个目标,不能回落成旧值。"""
    specs = cp._chapter_specs(120)
    assert len(specs) == 120
    assert {spec["words"] for spec in specs} == {2000}


def test_orchestrate_call_does_not_hardcode_the_target():
    """调用点不得再出现写死的目标字数(回归:曾导致规划目标恒为 1200)。"""
    source = Path(cp.__file__).read_text(encoding="utf-8")
    assert "target_words=1200" not in source
    assert re.search(r"target_words=int\(targets\.get\(index\)", source), \
        "生产链调用必须用本章自己的目标"
    assert "target_word_count=targets.get(index, 1200)" not in source


def test_cli_exposes_words_override():
    source = Path(cp.__file__).read_text(encoding="utf-8")
    assert '"--words"' in source