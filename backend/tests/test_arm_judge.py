"""回归:双盲 A/B 评审(arm_judge)。全部离线,不调真实模型。

要证的五件事:

1. **顺序随机化可复现**:同一 ``seed`` 两次派生同一个呈现顺序,且两臂都会当"文稿一";
2. **映射正确**:评审说"1"要落到实际当"文稿一"的那一臂,不是永远落到 A;
3. **解析容错**:评审把对白引号原样写进 JSON 值(实测 claude-sonnet-4-6 就这样)仍能取到偏好;
4. **坏输出不崩批**:解析失败/调用异常计入 ``skipped``,其余章对照常判定并落盘;
5. **一致率口径**:只统计两个评审都判过的章对,双方都判平局算一致,单方判定不进分母。

另外覆盖章节配对(两边都缺的章跳过)与 ``--dry-run`` 全流程。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.quality_eval import arm_judge
from scripts.quality_eval.arm_judge import (
    agreement,
    build_user,
    map_preference,
    order_for,
    pair_chapters,
    parse_verdict,
    summarize,
)

TEXT_A = ("雨是从后半夜开始下的。周芸把最后一盏油灯的芯剪短,火苗缩成豆子大。\n"
          "她数了两遍抽屉里的钱,两千一,差的那三百还压在渡口的账上。\n")
TEXT_B = ("后半夜落了雨。许舟把船绳又绕了一圈,绳子吃水,勒得手心发红。\n"
          "他听见岸上有灯灭了一盏,灭灯的地方是面棚,苏姐每晚最后收的就是那盏灯。\n")


def _write_arm(directory: Path, texts: dict[int, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index, text in texts.items():
        (directory / f"ch{index:02d}.txt").write_text(text, encoding="utf-8")
    return directory


def _verdict(preference: str, reason: str = "理由", evidence: str = "") -> str:
    return json.dumps({"preference": preference, "reason": reason,
                       "evidence": evidence}, ensure_ascii=False)


@pytest.fixture()
def fake_models(monkeypatch):
    """把真实调用与取 key 都替换掉;返回一个可改的调用记录。"""
    calls: list[dict] = []

    def fake_call(system, user, *, model, endpoint, api_key, max_tokens=1200, timeout=180.0):
        calls.append({"system": system, "user": user, "model": model})
        return _verdict("1", "第一篇更好")

    monkeypatch.setattr(arm_judge, "_call", fake_call)
    monkeypatch.setattr(arm_judge, "resolve_api_key", lambda explicit=None: "test-key")
    return calls


# ---------------------------------------------------------------------------
# 1. 顺序随机化
# ---------------------------------------------------------------------------

def test_order_is_reproducible_for_same_seed():
    assert order_for(17, 3, "m1") == order_for(17, 3, "m1")
    assert {order_for(17, chapter, "m1") for chapter in range(1, 41)} == {("A", "B"), ("B", "A")}
    # 换 seed / 换评审 / 换章号都能改变顺序:否则"随机化"只是把 A 永远放前面。
    assert {order_for(18, chapter, "m1") for chapter in range(1, 41)} == {("A", "B"), ("B", "A")}
    assert any(order_for(17, chapter, "m1") != order_for(17, chapter, "m2")
               for chapter in range(1, 41))


def test_order_does_not_encode_arm_preference():
    """顺序是伪随机的,不能因为 A 是"实验组"就偏向某一侧。"""
    orders = [order_for(17, chapter, judge)
              for chapter in range(1, 201) for judge in ("m1", "m2")]
    first_is_a = sum(1 for order in orders if order[0] == "A")
    assert 0.4 < first_is_a / len(orders) < 0.6


# ---------------------------------------------------------------------------
# 2. "1"/"2" 映射回 A/B
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order,expected", [
    (("A", "B"), {"1": "A", "2": "B"}),
    (("B", "A"), {"1": "B", "2": "A"}),
])
def test_preference_maps_back_to_the_arm_that_was_first(order, expected):
    assert map_preference("1", order) == expected["1"]
    assert map_preference("2", order) == expected["2"]
    assert map_preference("tie", order) == "tie"


def test_run_maps_preference_through_the_derived_order(tmp_path: Path, fake_models):
    """评审恒答"1"时,落地的偏好必须等于这一对实际排在第一的臂(不是永远 A)。"""
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A, 2: TEXT_B})
    dir_b = _write_arm(tmp_path / "b", {1: TEXT_B, 2: TEXT_A})
    result = arm_judge.run(dir_a=dir_a, dir_b=dir_b, out_dir=tmp_path / "out",
                           judges=["m1"], seed=17)
    assert result["judged"] == 2
    verdicts = json.loads((tmp_path / "out" / "verdicts.json").read_text(encoding="utf-8"))
    for row in verdicts["pairs"]:
        assert row["arm_1"] == order_for(17, row["chapter"], "m1")[0]
        assert row["preference"] == row["arm_1"]
    assert {row["preference"] for row in verdicts["pairs"]} == {"A", "B"}


def test_judge_prompt_does_not_leak_arm_labels():
    user = build_user("甲文", "乙文")
    assert "文稿一" in user and "文稿二" in user
    # 提示词里不许出现臂标签,评审也就无法用它当偏好信号。
    assert "A" not in user and "B" not in user
    for word in ("臂", "实验组", "对照组", "配置", "参数", "variant"):
        assert word not in user


# ---------------------------------------------------------------------------
# 3. 解析容错
# ---------------------------------------------------------------------------

def test_unescaped_inner_quotes_still_yield_a_preference():
    """实测样本:评审把对白引号原样写进 JSON 值,严格 json.loads 直接失败。"""
    broken = ('{"preference": "1", "reason": "把"守着渡口"这一步写在明面上", '
              '"evidence": "许舟低声道:"这回真成穷光蛋了。"林叶没说话。"}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    parsed = parse_verdict(broken)
    assert parsed["preference"] == "1"
    assert '"这回真成穷光蛋了。"' in parsed["evidence"]


def test_fenced_and_numeric_preferences_are_normalized():
    assert parse_verdict('```json\n{"preference": 2, "reason": "ok"}\n```')["preference"] == "2"
    assert parse_verdict('{"preference": "平局", "reason": "难分高下"}')["preference"] == "tie"
    assert parse_verdict('前言\n{"preference": "文稿一"}\n后记')["preference"] == "1"


def test_unrecognized_preference_raises():
    with pytest.raises(ValueError):
        parse_verdict('{"preference": "maybe", "reason": "??"}')
    with pytest.raises(ValueError):
        parse_verdict("这不是 JSON")


# ---------------------------------------------------------------------------
# 4. 解析失败计入 skipped,不抛异常
# ---------------------------------------------------------------------------

def test_parse_failure_is_skipped_not_fatal(tmp_path: Path, monkeypatch):
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A, 2: TEXT_B, 3: TEXT_A})
    dir_b = _write_arm(tmp_path / "b", {1: TEXT_B, 2: TEXT_A, 3: TEXT_B})

    def flaky_call(system, user, *, model, endpoint, api_key, max_tokens=1200, timeout=180.0):
        if model == "bad":
            return "抱歉,我无法比较这两篇。"
        if model == "worse":
            raise RuntimeError("gateway down")
        return _verdict("2", "第二篇更好")

    monkeypatch.setattr(arm_judge, "_call", flaky_call)
    monkeypatch.setattr(arm_judge, "resolve_api_key", lambda explicit=None: "test-key")

    result = arm_judge.run(dir_a=dir_a, dir_b=dir_b, out_dir=tmp_path / "out",
                           judges=["bad", "worse", "good"], seed=17)
    # 3 章 × 3 评审,两个坏评审每次都失败 → 跳过 6 次,好的 3 次照常落盘
    assert result["skipped"] == 6
    assert result["judged"] == 3
    verdicts = json.loads((tmp_path / "out" / "verdicts.json").read_text(encoding="utf-8"))
    assert {row["judge"] for row in verdicts["pairs"]} == {"good"}
    assert {item["judge"] for item in verdicts["skipped"]} == {"bad", "worse"}
    assert all(item["error_type"] and item["chapter"] for item in verdicts["skipped"])
    stats = {row["model"]: row for row in result["summary"]["judges"]}
    assert stats["bad"]["skipped"] == 3 and stats["bad"]["judged"] == 0
    assert stats["bad"]["a_win_rate"] is None        # 没有判定时不给 0,避免读成"全输"
    assert stats["good"]["judged"] == 3


def test_fabricated_evidence_is_flagged_but_preference_kept(tmp_path: Path, monkeypatch):
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A})
    dir_b = _write_arm(tmp_path / "b", {1: TEXT_B})

    def fake_call(system, user, *, model, endpoint, api_key, max_tokens=1200, timeout=180.0):
        return _verdict("1", "第一篇更好", "完全不存在的编造句子内容")

    monkeypatch.setattr(arm_judge, "_call", fake_call)
    monkeypatch.setattr(arm_judge, "resolve_api_key", lambda explicit=None: "test-key")
    result = arm_judge.run(dir_a=dir_a, dir_b=dir_b, out_dir=tmp_path / "out",
                           judges=["m1"], seed=17)
    verdicts = json.loads((tmp_path / "out" / "verdicts.json").read_text(encoding="utf-8"))
    assert verdicts["pairs"][0]["evidence_grade"] == "unlocated"
    assert verdicts["pairs"][0]["preference"] in ("A", "B")   # 偏好不因证据问题被改写
    assert result["summary"]["judges"][0]["evidence_unlocated"] == 1


# ---------------------------------------------------------------------------
# 5. 一致率
# ---------------------------------------------------------------------------

def _row(chapter: int, judge: str, preference: str) -> dict:
    return {"chapter": chapter, "judge": judge, "arm_1": "A", "preference": preference,
            "reason": "r", "evidence": "", "evidence_grade": "n/a"}


def test_agreement_counts_ties_and_ignores_one_sided_verdicts():
    rows = [
        _row(1, "m1", "A"), _row(1, "m2", "A"),        # 一致
        _row(2, "m1", "A"), _row(2, "m2", "B"),        # 分歧
        _row(3, "m1", "tie"), _row(3, "m2", "tie"),    # 双方都判平局 → 一致
        _row(4, "m1", "A"),                            # 只有 m1 判过 → 不进分母
    ]
    result = agreement(rows, ["m1", "m2"])
    assert result["compared_pairs"] == 3
    assert result["agreed_pairs"] == 2
    assert result["agreement"] == round(2 / 3, 4)
    assert result["incomplete_pairs"] == [4]
    # 逐对比只算两人都判过的章(ch1/ch2/ch3),ch4 只有 m1
    assert result["pairwise"]["m1|m2"] == {"compared": 3, "agreed": 2, "rate": round(2 / 3, 4)}


def test_agreement_is_none_with_a_single_judge_or_no_overlap():
    assert agreement([_row(1, "m1", "A")], ["m1"])["agreement"] is None
    mixed = [_row(1, "m1", "A"), _row(2, "m2", "B")]
    result = agreement(mixed, ["m1", "m2"])
    assert result["agreement"] is None and result["compared_pairs"] == 0


def test_summary_win_rate_excludes_ties_and_counts_pairs():
    rows = [_row(1, "m1", "A"), _row(2, "m1", "A"), _row(3, "m1", "B"), _row(4, "m1", "tie")]
    summary = summarize(rows, ["m1"], [])
    assert summary["pairs"] == 4
    stats = summary["judges"][0]
    assert (stats["a_wins"], stats["b_wins"], stats["ties"]) == (2, 1, 1)
    assert stats["a_win_rate"] == round(2 / 3, 4)
    assert stats["tie_rate"] == 0.25


# ---------------------------------------------------------------------------
# 6. 章节配对:只取两边都有的章
# ---------------------------------------------------------------------------

def test_pairs_only_include_chapters_present_in_both_dirs(tmp_path: Path):
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A, 2: TEXT_B, 3: TEXT_A})
    dir_b = _write_arm(tmp_path / "b", {2: TEXT_B, 3: TEXT_A, 4: TEXT_B})
    assert [index for index, _, _ in pair_chapters(dir_a, dir_b)] == [2, 3]


def test_pairs_honour_chapter_list_and_sample(tmp_path: Path):
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A, 2: TEXT_B, 3: TEXT_A, 5: TEXT_B})
    dir_b = _write_arm(tmp_path / "b", {1: TEXT_B, 2: TEXT_A, 3: TEXT_B, 5: TEXT_A})
    assert [index for index, _, _ in pair_chapters(dir_a, dir_b, chapters=[3, 5])] == [3, 5]
    assert [index for index, _, _ in pair_chapters(dir_a, dir_b, sample=2)] == [1, 2]


def test_blank_chapters_are_not_paired(tmp_path: Path):
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A, 2: "   \n"})
    dir_b = _write_arm(tmp_path / "b", {1: TEXT_B, 2: TEXT_B})
    assert [index for index, _, _ in pair_chapters(dir_a, dir_b)] == [1]


def test_no_common_chapters_is_an_error(tmp_path: Path):
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A})
    dir_b = _write_arm(tmp_path / "b", {2: TEXT_B})
    with pytest.raises(arm_judge.QualityEvalError):
        arm_judge.run(dir_a=dir_a, dir_b=dir_b, out_dir=tmp_path / "out", judges=["m1"])


# ---------------------------------------------------------------------------
# 7. --dry-run 全流程
# ---------------------------------------------------------------------------

def test_dry_run_writes_three_well_formed_artifacts(tmp_path: Path):
    out = tmp_path / "dry"
    assert arm_judge.main(["--out", str(out), "--dry-run"]) == 0
    assert sorted(path.name for path in out.iterdir()) == ["REVIEW.md", "summary.json",
                                                           "verdicts.json"]
    verdicts = json.loads((out / "verdicts.json").read_text(encoding="utf-8"))
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert verdicts["skipped"] == []
    assert verdicts["pairs"] and all(
        set(row) >= {"chapter", "judge", "arm_1", "preference", "reason", "evidence"}
        for row in verdicts["pairs"])
    assert all(row["preference"] in ("A", "B", "tie") for row in verdicts["pairs"])
    assert summary["pairs"] == 1
    assert len(summary["judges"]) == 2
    for stats in summary["judges"]:
        assert {"model", "a_wins", "b_wins", "ties", "a_win_rate"} <= set(stats)
    assert summary["agreement"] is not None          # 两个假评审都判过同一章
    report = (out / "REVIEW.md").read_text(encoding="utf-8")
    assert "# 双盲 A/B 评审" in report and "dry-run" in report
    assert "一致率" in report and "分歧最多的章节" in report


def test_dry_run_makes_no_model_calls(tmp_path: Path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run 不许调用模型")

    monkeypatch.setattr(arm_judge, "_call", forbidden)
    monkeypatch.setattr(arm_judge, "resolve_api_key", forbidden)
    result = arm_judge.run(out_dir=tmp_path / "dry", dry_run=True)
    assert result["calls"] == 0 and result["judged"] == 2


def test_run_reports_call_count_so_cost_is_predictable(tmp_path: Path, fake_models):
    dir_a = _write_arm(tmp_path / "a", {1: TEXT_A, 2: TEXT_B})
    dir_b = _write_arm(tmp_path / "b", {1: TEXT_B, 2: TEXT_A})
    result = arm_judge.run(dir_a=dir_a, dir_b=dir_b, out_dir=tmp_path / "out",
                           judges=["m1", "m2"], seed=17)
    assert result["calls"] == 4          # 2 章 × 2 评审
    assert len(fake_models) == 4
    # 每次调用都只看到两篇正文,提示词逐字一致(没有臂信息可以泄漏)
    assert len({call["user"] for call in fake_models}) <= 2