from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.chapter_quality_gate as cqg


GOOD_CONFLICT_TEXT = """
邱成手背青筋微突，死死按住本子：“只露一行。多半个字，我立马撕了。”

“可以。”陈青没废话，“认错我赔钱。认对，这本东西今晚谁也别想碰，明早市书会见。”

陈青逼近半步。

邱成眼神瞬间警惕：“退回去。”

陈青没退，目光盯死那行字：“‘青儿药照前方，德安堂欠二钱’。”

“街上药单都这么写，算什么铁证？”

“德安堂老周记账，规矩是年月打头，病人在后。”陈青声音冷硬，“只有我祖母去赊药，怕老眼昏花抓错，才会把病人的名字写在最前头。”
"""


BAD_DIALOGUE_MACHINE_TEXT = """
邱老板把细绳绕了两圈，“三月十二出库，十三过河，十四到旧纸巷。车从灰河码头到这儿要绕河东桥，桥上收车税，赶不上城门点灯，就得在桥西停一夜。”

陈青把那页放到左边，“那本也在这车里？”

“也许在，也许不在。押物箱拆开前谁知道？”

陈青弯腰，“你昨晚拆的？”

邱老板抬手，“今早拆的。”

这句话落下，铺子里安静了一小会儿。

陈青看着他，“你怕我认出什么？”

邱老板把算盘拨了两下，“我想让你按规矩来。”
"""


BAD_CHEAP_WIT_TEXT = """
“踩烂了，算你买。”
“先问问它算谁卖。”
“你别装了。”
“我装什么？”
“你刚才说看一行，现在又想看，你就是想赖账。”
"""

BAD_STRUCTURAL_MACHINE_TEXT = """
邱成把伞柄往门框上一靠，雨水顺着木杆往下滴。“河东桥外？我没碰过那块印。”

杨记账把木牌往灯下一送。“不是问你碰没碰，是问它怎么塞进册子里。”

阿宣抱着油纸包，站在柜台外侧，肩头湿了一大片。“师父，市书会那边催得急。”

邱成把柜台上的散页往一边拢了拢。“催得再急，也得先说清楚。灰河码头怎么不先查自己？”

门房把灯往外一探。“夜封？”

杨记账把牌递过去。“夜封。”

陈青把袖口往回一收。“我站院里？”

门房把门缝让开一点。“账房不行。”

罗管事把灯往前一抬。“谁碰过？”

邱成把伞柄攥紧。“都碰过。”

罗管事把灯压低。“我问册子。”

邱成把话咬得很硬。“只露一行。”

阿宣把包往怀里缩。“我说错了？”

邱成把眼一横。“现在才问？”

陈青把目光落在纸口。“能看吗？”

杨记账把竹尺横过去。“先记。”

许掌柜把竹签停在封边。“开吗？”

罗管事把灯往桌上一按。“只开一条。”

罗管事把灯往前一抬。“谁碰过？”

邱成回得快。“都碰过。”

罗管事把灯压低。“我问册子。”

邱成嘴角一绷。“只露了一行。”

许掌柜用竹签点了点蓝印边角。“回封是货走过一趟再回到原处才压的。河东桥外那边的转手点，我见过三回。收摊的人一看这个，就知道哪包货该往哪边回。”
"""

BAD_STORY_BIBLE_AND_DIRECTIONAL_TEXT = """
便利店里，收银台后的女人压低声音：“执行者昨晚又去南环了，听说那帮血裔闹得很凶，旧神那边也不太安生。”

楼下大爷接话：“奥丁那条线，你少往外说，网上都传开了。”

左边一排旧信箱都合着口，右墙贴着张褪色通知。东头有连廊，西头是楼梯口。
"""

BAD_MUNDANE_NATURALNESS_TEXT = """
烤肠摊老板探出半张脸：“别挡锅，水全甩进来了。两块，拿着。”

少年从纸箱里翻出临期面包，又去接摊位上的热水，旁边汤锅冒着白气。

他把手机按黑，想着房东月底又会发来收租截图。

食堂阿姨打饭时把空盘递给下一个人，才想起他还在队伍里。

保安的声音已经带了急：“别碰那边，那栋楼早没人住了。别进楼。”

便利店里有股消毒水味，冷柜边还摆着温水。

老师的目光滑走，门卫敷衍，班群无声，食堂漏人，房东催租。
"""

GOOD_MUNDANE_NATURALNESS_TEXT = """
烤肠摊老板掀开塑料帘：“别挡着锅，水都溅进来了。要站就买根烤肠。”

少年数了数零钱，还是退到棚边。手机快没电，他锁屏后塞回口袋，没再打开。

便利店门口亮着灯，他进去买了瓶矿泉水。收银员扫完码，把袋子往台上一放。

保安看了眼学生证：“别往楼里跑，锁门前自己出来。”

水从鞋底淌到地上，很快又被新的雨水冲淡。
"""

BAD_ROUTE_SEMANTIC_AND_SHELTER_TEXT = """
末班车早走了，最近一趟换乘也赶不上。

门缝里透不出灯，也没有任何可以借口留下来的声音。

叫车页面刷出来的价格比他身上能拿出来的钱更难看。

真进去，先得拿证件、交押金，再被前台隔着柜台从头看到脚。他现在连把背包放干的地方都没有，没力气再去碰这种脸色。
"""

GOOD_ROUTE_SEMANTIC_AND_SHELTER_TEXT = """
末班车已经没了，夜班线路绕远，他等不起。

门缝里没有灯光，也听不见人声。

叫车价格超过余额。

旅馆要押金，他的余额不够。前台看完身份证说满房，他只好退回雨里。

雨还在下，车站棚顶漏水，他只能沿着檐下往前走。手机电量快见底，屏幕亮一下又暗下去，没有司机接单，也没有人愿意在这个点绕到封路边上。

旅馆门口挂着满房牌，前台隔着玻璃摆了摆手。他没再争，转身时鞋底带起一滩水，背包贴在肩上，冷得像一块铁。
"""

BAD_PSEUDO_LITERARY_REGISTER_TEXT = """
少年喊了声师傅。值守员终于抬头，问他干什么的。

他把来意说得很低：车停了，手机快没电，想找个门厅坐到天亮，不进住户家，也不敲门。

楼外有人喊了一句，声音被关门声挡住一半。
"""

GOOD_PLAIN_CONTEMPORARY_REGISTER_TEXT = """
他叫了一声：“师傅。”

值守员抬头：“干嘛？”

“车没了，手机快没电了。我想在门厅坐到天亮，不上楼，也不敲门。”

值守员看了眼外面的雨，拉开登记本：“身份证拿出来。”
"""


@pytest.mark.asyncio
async def test_quality_gate_accepts_passing_text_without_rewrite(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("rewrite should not be called for passing text")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=GOOD_CONFLICT_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert result.status == "passed"
    assert result.final_text == GOOD_CONFLICT_TEXT
    assert result.rewrite_rounds == 0
    assert result.initial_report.passed is True
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_blocks_short_passing_text_against_target_word_count(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("rewrite should not be called for target-length shortfall")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=GOOD_MUNDANE_NATURALNESS_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        target_word_count=12500,
    )

    assert result.status == "blocked"
    assert result.warning_reason == "target_length_shortfall"
    assert result.final_text == GOOD_MUNDANE_NATURALNESS_TEXT
    assert result.rewrite_rounds == 0
    assert result.initial_report.passed is True
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_accepts_text_above_min_target_ratio(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("rewrite should not be called for passing long text")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)
    long_text = (
        "他沿着雨棚往前走，手机屏幕很暗，雨声一直压在头顶。"
        "街边的店门陆续落锁，玻璃上只剩模糊的灯影。"
        "他没有再回头，顺着能避雨的檐下慢慢往前挪，直到看见小区门口那盏旧灯。"
        "保安隔着窗问了一句，他只说躲雨，很快就走。"
        "风从楼缝里灌过来，身上的水冷透了，门洞深处却有一线干燥的光。"
        "\n\n"
        * 8
    ).strip()

    result = await cqg.apply_chapter_quality_gate(
        text=long_text,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        target_word_count=1000,
    )

    assert len(long_text) >= 850
    assert result.status == "passed"
    assert result.final_text == long_text
    assert result.rewrite_rounds == 0


@pytest.mark.asyncio
async def test_quality_gate_accepts_4000_target_at_3200_soft_floor(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("rewrite should not be called for passing soft-floor text")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)
    text = ("雨声压着街口，他沿着店檐往前走。手机只剩最后一格电，叫车页面没人接单。" * 100).strip()

    result = await cqg.apply_chapter_quality_gate(
        text=text,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        target_word_count=4000,
        min_target_word_ratio=0.80,
    )

    assert len(text) >= 3200
    assert result.status == "passed"
    assert result.min_target_word_count == 3200


@pytest.mark.asyncio
async def test_quality_gate_blocks_4000_target_below_3200_soft_floor(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("rewrite should not be called for target-length shortfall")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)
    text = ("雨声压着街口，他沿着店檐往前走。手机只剩最后一格电，叫车页面没人接单。" * 20).strip()

    result = await cqg.apply_chapter_quality_gate(
        text=text,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        target_word_count=4000,
        min_target_word_ratio=0.80,
    )

    assert len(text) < 3200
    assert result.status == "blocked"
    assert result.warning_reason == "target_length_shortfall"
    assert result.min_target_word_count == 3200


def test_all_generation_quality_gate_calls_pass_target_word_count() -> None:
    """Generation callers must not bypass the hard target-length gate."""
    import ast
    from pathlib import Path

    test_path = Path(__file__).resolve()
    candidates = [
        (
            parent / "backend/app/api/generate.py",
            parent / "backend/app/tasks/generation_tasks.py",
        )
        for parent in (test_path, *test_path.parents)
    ] + [
        (
            parent / "app/api/generate.py",
            parent / "app/tasks/generation_tasks.py",
        )
        for parent in (test_path, *test_path.parents)
    ]
    files = next(pair for pair in candidates if all(path.exists() for path in pair))
    missing: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            else:
                continue
            if name != "apply_chapter_quality_gate":
                continue
            if not any(kw.arg == "target_word_count" for kw in node.keywords):
                missing.append(f"{path}:{node.lineno}")

    assert missing == []


@pytest.mark.parametrize("text", [
    "他把册子夹在腋下，伸手去扶伤者。",
    "孔径还差半寸，不能用这根销子。",
    "“别碰那边。”她说，“人可以过去，那罐药不能碰。”",
    "他按黑板上的字念了一遍。",
    "“别进楼，在门口等。”",
])
def test_prose_format_cleanup_preserves_actions_measurements_and_dialogue(text):
    assert cqg._normalize_text("\n\n" + text + "\n") == text


@pytest.mark.asyncio
async def test_quality_gate_rejects_too_short_rewrite_and_keeps_original(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):
        return SimpleNamespace(text="太短了。")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text="第十二章\n" + BAD_DIALOGUE_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert result.status == "blocked"
    assert result.final_text == "第十二章\n" + BAD_DIALOGUE_MACHINE_TEXT
    assert result.rewrite_rounds == 2
    assert result.final_report.passed is False
    assert result.final_report.action_dialogue_beat_count >= 3


# ---------------------------------------------------------------------------
# Rewrite-round budget routing (2026-07-26 audit): the default must come from
# Settings.CHAPTER_MAX_REWRITE_ROUNDS at CALL time — the previous import-time
# os.getenv read bypassed the config default of 2 (deployments without the env
# var silently got 5 rounds) and made monkeypatched settings ineffective.
# ---------------------------------------------------------------------------


def test_no_import_time_env_round_default() -> None:
    assert not hasattr(cqg, "_DEFAULT_MAX_REWRITE_ROUNDS")


@pytest.mark.asyncio
async def test_quality_gate_default_rounds_come_from_settings(monkeypatch) -> None:
    from app.config import settings

    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="太短了。")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)
    monkeypatch.setattr(settings, "CHAPTER_MAX_REWRITE_ROUNDS", 1)

    result = await cqg.apply_chapter_quality_gate(
        text="第十二章\n" + BAD_DIALOGUE_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert len(calls) == 1
    assert result.rewrite_rounds == 1
    assert result.status == "blocked"


@pytest.mark.asyncio
async def test_quality_gate_explicit_rounds_override_settings(monkeypatch) -> None:
    from app.config import settings

    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="太短了。")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)
    monkeypatch.setattr(settings, "CHAPTER_MAX_REWRITE_ROUNDS", 1)

    result = await cqg.apply_chapter_quality_gate(
        text="第十二章\n" + BAD_DIALOGUE_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        max_rewrite_rounds=3,
    )

    assert len(calls) == 3
    assert result.rewrite_rounds == 3


def test_quality_penalty_ignores_reader_feedback_metrics() -> None:
    base = cqg.ChineseProseMechanicsReport()
    flagged = cqg.ChineseProseMechanicsReport(
        micro_action_beat_count=5,
        micro_action_density_per_1000=6.0,
        micro_action_overload=True,
        repeated_rare_phrase_count=2,
        translationese_marker_count=1,
    )

    assert cqg._quality_penalty(flagged) == cqg._quality_penalty(base)
    # Overload gating: the same beat count without overload adds no penalty.
    unflagged = cqg.ChineseProseMechanicsReport(
        micro_action_beat_count=5,
        micro_action_density_per_1000=1.0,
        micro_action_overload=False,
    )
    assert cqg._quality_penalty(unflagged) == cqg._quality_penalty(base)


def test_structure_repair_prompt_does_not_request_style_changes():
    text = "第十二章\n她顿了顿，把钥匙交给他。"
    content = cqg._build_rewrite_user_content(text=text, initial_report=cqg.analyze_chinese_prose_mechanics(text), round_idx=1, previous_attempts=[])
    assert text in content
    assert "不要重写句子" in content
    assert "均不构成此次改稿理由" in content
    assert "删除显性停顿" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "他把册子夹在腋下，伸手去扶伤者。",
    "孔径还差半寸，不能用这根销子。",
    "铜针比样图长了寸许，他没有动手截断，先去核对原件。",
])
async def test_precise_body_or_measurement_words_do_not_trigger_rewrite(monkeypatch, text):
    from unittest.mock import AsyncMock
    generate = AsyncMock(side_effect=AssertionError("No prose defect to rewrite"))
    monkeypatch.setattr(cqg, "run_text_prompt", generate)
    result = await cqg.apply_chapter_quality_gate(
        text=text, db=SimpleNamespace(), project_id="p", chapter_id="c")
    assert result.status == "passed"
    assert result.final_text == text
    assert result.final_report.space_watchlist_hits > 0
    from dataclasses import replace
    without_watchlist = replace(result.final_report, space_watchlist_hits=0)
    assert cqg._quality_penalty(result.final_report) == cqg._quality_penalty(without_watchlist)
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,metric', [
    ('桥面宽一尺，门缝只有一指宽。她量过以后把箱子留在原处，背包过去。', 'spatial_mapping_count'),
    ('“院里那棵树今年又开花了，风一吹，花瓣全落在你以前坐的那张椅子上。我原来想把椅子收进屋里，后来还是放在那里。邻居的小孩放学经过，常常坐一会儿，有时读书，有时就看着树。我在厨房忙完出来，他已经回家了，椅子上留下几片花瓣，跟你小时候一样。”', 'long_quote_segments_gt80'),
    ('六本台账都在木箱里。箱盖掀开后，六本台账露出卷角。她抽出六本台账，逐册查看封面的名字。', 'repeated_rare_phrase_count'),
])
async def test_editorial_observations_preserve_useful_prose_without_paid_rewrite(monkeypatch, text, metric):
    from unittest.mock import AsyncMock
    from dataclasses import replace
    generate = AsyncMock(side_effect=AssertionError('Observation is not a prose defect'))
    monkeypatch.setattr(cqg, 'run_text_prompt', generate)
    result = await cqg.apply_chapter_quality_gate(text=text, db=SimpleNamespace(), genre_hint='现实生活')
    assert getattr(result.final_report, metric) > 0
    assert result.status == 'passed'
    assert result.final_text == text
    cleared = replace(result.final_report, spatial_mapping_count=0,
                      long_quote_segments_gt80=0, biographical_infodump_count=0, repeated_rare_phrase_count=0,
                      repeated_rare_phrases={})
    assert cqg._quality_penalty(result.final_report) == cqg._quality_penalty(cleared)
    generate.assert_not_awaited()


def test_observations_do_not_hide_explicit_structure_leakage():
    report = cqg.analyze_chinese_prose_mechanics('[CH-12]\n她从三步外赶来。')
    assert report.spatial_mapping_count > 0
    assert report.meta_structure_leakage_count > 0
    assert not report.passed


def test_rewrite_and_preflight_do_not_command_observation_counts_to_zero():
    from app.services.chinese_prose_mechanics_checker import build_generation_preflight_prompt
    text = '六本台账都在木箱里。六本台账没有缺页。六本台账将由她保管。'
    report = cqg.analyze_chinese_prose_mechanics(text)
    prompt = cqg._build_rewrite_user_content(text=text, initial_report=report, round_idx=1, previous_attempts=[])
    preflight = build_generation_preflight_prompt(report)
    assert '六本台账' in prompt and '六本台账' in preflight
    assert '均不构成此次改稿理由' in prompt and '不要求清零' in preflight
    assert 'spatial_mapping_count 必须为 0' not in preflight
    assert 'repeated_rare_phrase_count 必须为 0' not in preflight
    assert '删除三步外' not in prompt
    assert '不得反复出现三次以上' not in prompt + preflight


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    BAD_DIALOGUE_MACHINE_TEXT, BAD_CHEAP_WIT_TEXT, BAD_STRUCTURAL_MACHINE_TEXT,
    BAD_STORY_BIBLE_AND_DIRECTIONAL_TEXT, BAD_MUNDANE_NATURALNESS_TEXT,
    BAD_ROUTE_SEMANTIC_AND_SHELTER_TEXT, BAD_PSEUDO_LITERARY_REGISTER_TEXT,
    '他顿了顿，把六本台账放进柜子。她沉默了一会儿，终于抬头。',
    '“你怕我认出什么？”\n“我想让你按规矩来。”',
])
async def test_form_observations_never_request_paid_rewrite(monkeypatch, text):
    async def forbidden_call(**kwargs):
        pytest.fail('counts alone must not request a model rewrite')
    monkeypatch.setattr(cqg, 'run_text_prompt', forbidden_call)
    result = await cqg.apply_chapter_quality_gate(text=text, db=None)
    assert result.status == 'passed'
    assert result.final_text == text
    assert result.rewrite_rounds == 0
    assert result.final_report.to_safe_dict()['literary_acceptance'] == 'not_assessed'


@pytest.mark.parametrize('text', [
    '她读到第三章，终于抬头。', '“第一卷读完了吗？”他问。',
    '纸上写着[CH-12]。', '第三章讲的是河上的桥。',
    '第三章 里的那个人也有六本台账。',
    '她读出纸上的字：“\n第十二章\n本章完\n”',
    '她读出纸上的字：「\n[CH-12]\n本章完\n」',
])
def test_book_references_are_not_output_scaffolding(text):
    assert cqg.analyze_chinese_prose_mechanics(text).meta_structure_leakage_count == 0
    assert cqg.chapter_persist_status(text) == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('text,reason', [('', 'empty'), ('  ', 'empty'), ('她打开柜子，发现', 'truncated'), ('作为AI，我无法为您创建任何图片。', 'refusal')])
@pytest.mark.parametrize('skip', [False, True])
async def test_invalid_output_cannot_bypass_gate(monkeypatch, text, reason, skip):
    async def forbidden_call(**kwargs):
        pytest.fail('invalid output must be retained as draft, not rewritten for style')
    monkeypatch.setattr(cqg, 'run_text_prompt', forbidden_call)
    result = await cqg.apply_chapter_quality_gate(text=text, db=None, skip_polish=skip)
    assert result.status == 'blocked'
    assert result.warning_reason == reason
    assert result.final_text == text
    assert cqg.chapter_persist_status(text) == 'draft'


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate,reason', [
    ('她顿了顿，把五本台账锁进柜子。\n“明早一起核对。”她说。', 'prose_changed'),
    ('她顿了顿，把六本台账锁进柜子。\n“明早一起核对。”她说', 'truncated'),
    ('作为AI，我无法为您创建任何图片。', 'refusal'),
    ('', 'empty'),
])
async def test_structure_repair_rejects_changed_or_invalid_prose(monkeypatch, candidate, reason):
    original = '[CH-12]\n她顿了顿，把六本台账锁进柜子。\n“明早一起核对。”她说。'
    async def fake_call(**kwargs):
        return SimpleNamespace(text=candidate)
    monkeypatch.setattr(cqg, 'run_text_prompt', fake_call)
    result = await cqg.apply_chapter_quality_gate(text=original, db=None, max_rewrite_rounds=1)
    assert result.status == 'blocked'
    assert result.final_text == original
    assert result.rewrite_attempts[0]['reason'] == reason


@pytest.mark.asyncio
async def test_structure_only_repair_keeps_pause_dialogue_and_all_facts(monkeypatch):
    prose = '她顿了顿，把六本台账锁进柜子。\n“明早一起核对。”她说。'
    original = '[CH-12]\n' + prose + '\n本章完'
    async def fake_call(**kwargs):
        assert original in kwargs['user_content']
        assert '不要执行风格润色' in kwargs['extra_system']
        return SimpleNamespace(text=prose)
    monkeypatch.setattr(cqg, 'run_text_prompt', fake_call)
    result = await cqg.apply_chapter_quality_gate(text=original, db=None)
    assert result.status == 'passed'
    assert result.final_text == prose
    assert result.final_report.explicit_pause_marker_count == 1
    assert result.rewrite_rounds == 1
    assert cqg.chapter_persist_status(original) == 'draft'


def test_all_observational_metrics_are_excluded_from_candidate_ranking():
    from dataclasses import fields
    baseline = cqg.ChineseProseMechanicsReport()
    for spec in fields(baseline):
        if spec.name in {'passed', 'meta_structure_leakage_count'}:
            continue
        sample = cqg.ChineseProseMechanicsReport()
        value = getattr(sample, spec.name)
        if type(value) in (int, float):
            setattr(sample, spec.name, 1000)
        elif type(value) is bool:
            setattr(sample, spec.name, True)
        assert cqg._quality_penalty(sample) == 0, spec.name
    assert cqg._quality_penalty(cqg.ChineseProseMechanicsReport(meta_structure_leakage_count=2)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('length,expected', [(970, 'blocked'), (1439, 'blocked'), (1440, 'passed')])
async def test_1800_word_target_keeps_short_candidates_for_review_without_padding(monkeypatch, length, expected):
    call = AsyncMock(side_effect=AssertionError('No padding rewrite should run'))
    monkeypatch.setattr(cqg, 'run_text_prompt', call)
    text = ('她沿街慢慢走着。' * (length // 8 + 1))[:length - 1] + '。'
    result = await cqg.apply_chapter_quality_gate(
        text=text, db=SimpleNamespace(), target_word_count=1800,
        min_target_word_ratio=0.80,
    )
    # 显式 0.80:本条验证软地板的边界(1440 = 1800×0.80)本身;
    # 生产默认已是 0.95,口径回归见 tests/test_chapter_length_overalloc.py。
    assert result.status == expected
    assert result.final_text == text
    assert result.min_target_word_count == 1440
    call.assert_not_called()
