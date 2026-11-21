"""连续性探针检查器的纯函数与端到端行为。

这些检查是"长篇能不能记住自己前面写下的钱"的测量装置,装置错了结论就错
(2026-11-14 就因为探针把大纲写在渲染器不认识的字段名下,得出过错误结论)。
"""
import json
from pathlib import Path
import pytest

from scripts.quality_eval.continuity_probe import (
    CHAPTERS,
    PINS,
    _amount_value,
    _chapter_specs,
    _nearby_amounts,
    check,
)


@pytest.mark.parametrize("token,expected", [
    ("一千二", 1200), ("八百二", 820), ("九百", 900), ("1200", 1200),
    ("二十", 20), ("三千", 3000), ("两千八", 2800), ("一千二百", 1200),
    ("五十", 50), ("一万", 10000),
])
def test_amount_value_reads_colloquial_chinese_money(token, expected):
    assert _amount_value(token) == expected


def test_nearby_amounts_ignores_other_sentences():
    text = "医生说再交一千二才能排上手术。复查单折好收进包里。"
    assert _nearby_amounts(text, "复查") == set()


def test_nearby_amounts_accepts_amount_without_unit():
    assert _nearby_amounts("复查费一共一千块。", "复查") == {1000.0}


def test_nearby_amounts_ignores_time_expressions():
    assert _nearby_amounts("复查在十一点开始。", "复查") == set()


def test_chapter_specs_extend_to_second_act_without_reuse():
    specs = _chapter_specs(len(CHAPTERS))
    titles = [spec["title"] for spec in specs]
    assert len(titles) == len(set(titles)) == len(CHAPTERS) >= 24
def test_pins_point_at_real_chapters_of_a_known_keyword():
    for pin_name, keyword, allowed in PINS:
        number = int("".join(ch for ch in pin_name if ch.isdigit()))
        spec = CHAPTERS[number - 1]
        assert keyword in spec["brief"], f"{keyword} 不在第 {number} 章大纲里"
        assert allowed and all(_amount_value(a) for a in allowed)
    assert len(CHAPTERS) >= 20


def _write(tmp: Path, name: str, text: str) -> None:
    (tmp / name).write_text(text, encoding="utf-8")


def test_pinned_number_conflict_is_reported(tmp_path):
    _write(tmp_path, "ch11.txt", "医生说再交一千二才能排上手术,复查单在她手里。")
    _write(tmp_path, "ch12.txt", "老周说起医院那一幕,周航才问清复查费是一千块。")
    report = check(tmp_path)
    hits = [f for f in report["findings"] if f["check"] == "pinned_number_conflict"]
    assert [h["chapter"] for h in hits] == ["ch12.txt"]
    assert hits[0]["keyword"] == "复查" and hits[0]["found"] == [1000.0]


def test_pinned_number_conflict_absent_when_amount_matches(tmp_path):
    _write(tmp_path, "ch11.txt", "医生说再交一千二才能排上手术,复查单在她手里。")
    _write(tmp_path, "ch12.txt", "周航把钱交到窗口:复查费一千二元,一分不多。")
    report = check(tmp_path)
    assert not [f for f in report["findings"] if f["check"] == "pinned_number_conflict"]


def test_pin_does_not_apply_to_chapters_before_it(tmp_path):
    _write(tmp_path, "ch02.txt", "房东来催八百块的铺租,期限就在下月初一。")
    report = check(tmp_path)
    assert not [f for f in report["findings"] if f["check"] == "pinned_number_conflict"]


def test_unknown_character_is_reported(tmp_path):
    _write(tmp_path, "ch03.txt", "隔壁的李师傅推门进来,手里拎着一台旧风扇。")
    report = check(tmp_path)
    names = [f["name"] for f in report["findings"] if f["check"] == "unknown_character"]
    assert "李师傅" in names


def test_roster_names_are_not_reported_as_unknown(tmp_path):
    _write(tmp_path, "ch03.txt", "老周把船靠了岸,周航站在码头上等。")
    report = check(tmp_path)
    assert not [f for f in report["findings"] if f["check"] == "unknown_character"]




def test_role_titled_new_person_is_reported(tmp_path):
    _write(tmp_path, "ch03.txt", "隔壁的李师傅推门进来,手里拎着一台旧风扇。")
    report = check(tmp_path)
    assert "李师傅" in [f["name"] for f in report["findings"]
                      if f["check"] == "unknown_character"]


def test_role_title_address_to_roster_person_is_not_new(tmp_path):
    _write(tmp_path, "ch03.txt", "“周老板,这日子可是到头了,半年的铺租……”老刘说。")
    report = check(tmp_path)
    assert not [f for f in report["findings"] if f["check"] == "unknown_character"]
def test_address_form_of_roster_person_is_not_a_new_character(tmp_path):
    _write(tmp_path, "ch03.txt", "邻居抱着布料进来:“周姐,忙着呢?”")
    report = check(tmp_path)
    assert not [f for f in report["findings"] if f["check"] == "unknown_character"]
