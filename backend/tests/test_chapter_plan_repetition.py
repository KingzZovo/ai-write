"""卷纲计划重复检测(2026-11-28 全书质量检查)。

《白蜡树下》卷 5 的卷纲从第 33 章起循环重规划同一高潮:100 章里 57 个标题重复,
两两摘要 8-gram Jaccard 最高 0.70。正文因此成片重复(标题重复 238 章、相邻章
Jaccard 最高 0.97、章内句级模板循环最多 33 次)。规划层没有叙事理由让两章计划
相同,所以这里设**硬闸**并把原因回喂给模型(正文层则只做证据注入,因为文风
契约允许有叙事作用的重复)。
"""
from __future__ import annotations

from app.services.chapter_plan_repetition import (
    describe_violation,
    plan_repetition,
)


def _entry(idx, title, summary):
    return {"chapter_idx": idx, "title": title, "summary": summary}


_SCENES = [
    "陈默蹲在门卫室窗下,把昨夜收到的快递单一张张理顺,按送件人归堆,发现其中一张的收件栏被涂过。",
    "灰袍文书半夜来敲门,只放下一只牛皮纸袋就走;陈默没有当场拆,先把它压在登记本底下。",
    "训练场上落雨,教官临时改了集合点;陈默绕到旧操场东侧,看见老槐树底下多了一行新车辙。",
    "沈栀来信,纸很薄,只有两行字;陈默把信折成小方块塞进内兜,指腹来回压了三遍。",
    "仓库外的铁门换了锁,新锁比旧锁短一截;陈默蹲下比了比锁孔,把尺寸记在掌心里。",
    "社团活动室停电,有人点了蜡烛;陈默借着火光辨认墙上那张旧合照里被剪掉的人。",
    "母亲托人带话,说保洁外派要换片区;陈默听完没有应声,只把那句话在纸上写了两遍。",
    "操场边的水管冻裂,水漫到跑道上;陈默绕开水洼,却发现冰面下压着半张招生简章。",
    "食堂后门卸货,纸箱堆得比人高;陈默帮着搬了两趟,顺手把标签朝向自己看了一眼。",
    "夜里起了风,门卫室的窗被吹开;陈默起身关窗,看见操场旗杆的影子比昨天偏了一格。",
]


def _distinct_summary(n):
    return _SCENES[(n - 1) % len(_SCENES)]

def _clean_plan(n=10):
    return [_entry(i, f"档案{i}", _distinct_summary(i)) for i in range(1, n + 1)]


def test_clean_plan_reports_nothing():
    report = plan_repetition(_clean_plan())
    assert report["duplicate_titles"] == {}
    assert report["near_duplicate_pairs"] == []
    assert describe_violation(report) is None


def test_duplicate_title_is_a_violation():
    plan = _clean_plan()
    plan[3]["title"] = plan[0]["title"]
    report = plan_repetition(plan)
    assert report["duplicate_titles"] == {plan[0]["title"]: [1, 4]}
    reason = describe_violation(report)
    assert "标题重复" in reason and "重新规划" in reason


def test_near_duplicate_summaries_are_a_violation():
    plan = _clean_plan()
    # 同一情节点被轻微改写后重规划 —— 正是卷 5 的形态。
    plan[5]["summary"] = plan[1]["summary"].replace("第2次", "第6次")
    report = plan_repetition(plan)
    assert report["near_duplicate_pairs"], report
    assert report["worst_similarity"] >= 0.45
    assert "近似重复" in describe_violation(report)


def test_short_summaries_do_not_create_pairs():
    plan = [_entry(i, f"章{i}", "短摘要。") for i in range(1, 6)]
    assert plan_repetition(plan)["near_duplicate_pairs"] == []


def test_violation_message_names_the_chapters_so_the_model_can_repair():
    plan = _clean_plan()
    plan[7]["title"] = plan[0]["title"]
    reason = describe_violation(plan_repetition(plan), index_span="1-10")
    assert "1-10" in reason
    assert "章 [1, 8]" in reason