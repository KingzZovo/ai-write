"""题材种子:换题材跑同一套长跑流程,而不是改代码(2026-11-19)。

为什么需要:内置题材是《渡口旧账》(现实生活)。跨模型/跨参数的结论要能推广,
就得能在另一个题材上跑同一条生产链。种子是**纯数据**覆盖(题材/人物/章节大纲/
金额钉子),不触碰任何生产代码路径。

这里只测加载与校验:坏种子必须**明确报错**,而不是带着半个题材跑起来(那会产出
一批语义错乱、事后无法解释的章节)。
"""

from __future__ import annotations

import json

import pytest

from scripts.quality_eval.continuity_probe import load_seed_file

_GOOD = {
    "blueprint": {"title": "试书", "genre": "悬疑", "premise": "一句话前提。",
                  "volume": {"title": "第一卷", "summary": "卷概要。"}},
    "roster": ["甲", "乙"],
    "chapters": [
        {"title": "第一章 起", "time": "当日傍晚", "place": "屋内", "pov": "甲", "words": 2000,
         "brief": "本章需完成:开门。"},
    ],
}


def _write(tmp_path, payload):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_loads_blueprint_roster_and_chapters(tmp_path):
    seed = load_seed_file(_write(tmp_path, _GOOD))
    assert seed["blueprint"]["title"] == "试书"
    assert seed["roster"] == ("甲", "乙")
    assert len(seed["chapters"]) == 1


def test_pins_default_to_empty(tmp_path):
    """换题材后不能继续套用内置的金额钉子(那是按原题材章号写的)。"""
    seed = load_seed_file(_write(tmp_path, _GOOD))
    assert seed["pins"] == ()


def test_pins_are_normalised(tmp_path):
    payload = dict(_GOOD, pins=[["ch11.txt", "复查", ["一千二", "1200"]]])
    seed = load_seed_file(_write(tmp_path, payload))
    assert seed["pins"] == (("ch11.txt", "复查", ("一千二", "1200")),)


@pytest.mark.parametrize("broken, message", [
    ({"chapters": _GOOD["chapters"]}, "blueprint"),
    ({"blueprint": {"title": "只有标题"}, "chapters": _GOOD["chapters"]}, "premise"),
    ({"blueprint": _GOOD["blueprint"], "chapters": []}, "chapters"),
    ({"blueprint": _GOOD["blueprint"], "chapters": [{"time": "当日"}]}, "title"),
])
def test_broken_seed_fails_loudly(tmp_path, broken, message):
    with pytest.raises(ValueError) as err:
        load_seed_file(_write(tmp_path, broken))
    assert message in str(err.value)


def test_non_object_seed_is_rejected(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_seed_file(str(path))


def test_repo_seed_file_for_the_second_genre_is_valid():
    """仓库里随附的第二题材种子必须可加载 —— 它是换题材验证的输入。"""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "docs/evaluation/seeds/urban_mystery.json"
    seed = load_seed_file(str(path))
    assert seed["blueprint"]["genre"] == "都市悬疑"
    assert len(seed["chapters"]) >= 29
    assert seed["roster"]