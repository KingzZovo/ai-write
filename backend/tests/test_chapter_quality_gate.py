from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.chapter_quality_gate as cqg


GOOD_CONFLICT_TEXT = """
邱成手背青筋微突，死死按住本子：“只露一行。多半个字，我立马撕了。”

“可以。”陈青没废话，“认错我赔钱。认对，这本东西今晚谁也别想碰，明早市书会见。”

陈青逼近半步。

邱成眼神瞬间警惕：“退回去。”

陈青没退，目光盯死那行字：“‘青儿药照前方，德安堂欠二钱’。”

“街上药单都这么写，算什么铁证？”

“德安堂老周记账，规矩是年月打头，病人在后。”陈青声音冷硬，“只有我祖母去赊药，怕老眼昏花抓错，才会把我的名字强行顶在最前头。”
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
async def test_quality_gate_accepts_4000_target_at_2000_soft_floor(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("rewrite should not be called for passing soft-floor text")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)
    text = ("雨声压着街口，他沿着店檐往前走。手机只剩最后一格电，叫车页面没人接单。" * 60).strip()

    result = await cqg.apply_chapter_quality_gate(
        text=text,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        target_word_count=4000,
    )

    assert len(text) >= 2000
    assert result.status == "passed"
    assert result.min_target_word_count == 2000


@pytest.mark.asyncio
async def test_quality_gate_blocks_4000_target_below_2000_soft_floor(monkeypatch) -> None:
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
    )

    assert len(text) < 2000
    assert result.status == "blocked"
    assert result.warning_reason == "target_length_shortfall"
    assert result.min_target_word_count == 2000


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


@pytest.mark.asyncio
async def test_quality_gate_rewrites_mechanical_text_and_returns_final_text(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        assert kwargs["task_type"] == "rewrite"
        assert "三月十二出库" in kwargs["user_content"]
        assert "陈青" in kwargs["user_content"]
        assert "communication_damping" in kwargs["extra_system"]
        assert "plain_register_no_wit" in kwargs["extra_system"]
        assert "focal_measure_only" in kwargs["extra_system"]
        assert "motive_exposition_zero" in kwargs["extra_system"]
        assert "floating_dialogue_exchange" in kwargs["extra_system"]
        assert "dialogue_symmetry_break" in kwargs["extra_system"]
        assert "说好一行" in kwargs["extra_system"]
        assert "chapter_level_anti_padding" in kwargs["extra_system"]
        assert "chapter_level_anti_padding" in kwargs["user_content"]
        assert "repeated_realization_run" in kwargs["user_content"]
        return SimpleNamespace(text=GOOD_CONFLICT_TEXT)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_DIALOGUE_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert len(calls) == 1
    assert result.status == "passed"
    assert result.rewrite_rounds == 1
    assert result.final_text.strip() == GOOD_CONFLICT_TEXT.strip()
    assert result.initial_report.passed is False
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_rewrites_setting_name_leakage_and_directional_listing(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        assert kwargs["task_type"] == "rewrite"
        assert "setting_name_dialogue_zero" in kwargs["extra_system"]
        assert "directional_listing_zero" in kwargs["extra_system"]
        assert "封路" in kwargs["extra_system"]
        assert "停电" in kwargs["extra_system"]
        assert "绕路" in kwargs["extra_system"]
        assert "物价" in kwargs["extra_system"]
        assert "代词" in kwargs["extra_system"]
        assert "story_bible_leakage_count" in kwargs["user_content"]
        assert "directional_listing_count" in kwargs["user_content"]
        return SimpleNamespace(text=GOOD_CONFLICT_TEXT)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_STORY_BIBLE_AND_DIRECTIONAL_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert len(calls) == 1
    assert result.status == "passed"
    assert result.initial_report.story_bible_leakage_count >= 1
    assert result.initial_report.directional_listing_count >= 1
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_rewrites_mundane_naturalness_and_limited_pov(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        assert kwargs["task_type"] == "rewrite"
        assert "mundane_scene_plausibility" in kwargs["extra_system"]
        assert "plain_modern_register" in kwargs["extra_system"]
        assert "limited_pov_only" in kwargs["extra_system"]
        assert "semantic_density_budget" in kwargs["extra_system"]
        assert "age_plausibility" in kwargs["extra_system"]
        assert "试错" in kwargs["extra_system"]
        assert "awkward_register_count" in kwargs["user_content"]
        assert "limited_pov_leak_count" in kwargs["user_content"]
        assert "mundane_logic_violation_count" in kwargs["user_content"]
        return SimpleNamespace(text=GOOD_MUNDANE_NATURALNESS_TEXT)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_MUNDANE_NATURALNESS_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert len(calls) == 1
    assert result.status == "passed"
    assert result.initial_report.mundane_logic_violation_count >= 3
    assert result.initial_report.limited_pov_leak_count >= 1
    assert result.initial_report.awkward_register_count >= 3
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_rewrites_route_semantic_and_shelter_logic(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        assert kwargs["task_type"] == "rewrite"
        assert "transport_logic" in kwargs["user_content"]
        assert "semantic_collocation" in kwargs["user_content"]
        assert "shelter_cost_logic" in kwargs["user_content"]
        assert "abstract_evasion" in kwargs["user_content"]
        assert "末班车" in kwargs["user_content"]
        assert "灯光" in kwargs["user_content"]
        assert "余额" in kwargs["user_content"]
        assert "押金" in kwargs["user_content"]
        return SimpleNamespace(text=GOOD_ROUTE_SEMANTIC_AND_SHELTER_TEXT)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_ROUTE_SEMANTIC_AND_SHELTER_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert len(calls) == 1
    assert result.status == "passed"
    assert result.initial_report.transport_logic_count >= 1
    assert result.initial_report.semantic_collocation_count >= 2
    assert result.initial_report.shelter_cost_logic_count >= 1
    assert result.initial_report.abstract_evasion_count >= 1
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_rewrites_pseudo_literary_register(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        assert kwargs["task_type"] == "rewrite"
        assert "plain_contemporary_chinese" in kwargs["extra_system"]
        assert "pseudo_literary_register_count" in kwargs["user_content"]
        assert "喊了声师傅" in kwargs["user_content"]
        assert "来意说得很低" in kwargs["user_content"]
        assert "cross_project_prose_quality_contract" in kwargs["extra_system"]
        assert "plain_contemporary_violation_count" in kwargs["user_content"]
        assert "duplicate_explanation_span_count" in kwargs["user_content"]
        return SimpleNamespace(text=GOOD_PLAIN_CONTEMPORARY_REGISTER_TEXT)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_PSEUDO_LITERARY_REGISTER_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert len(calls) == 1
    assert result.status == "passed"
    assert result.initial_report.pseudo_literary_register_count >= 3
    assert result.final_report.pseudo_literary_register_count == 0
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_blocks_when_cheap_wit_residuals_remain(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):
        assert "communication_damping" in kwargs["extra_system"]
        assert "plain_register_no_wit" in kwargs["extra_system"]
        return SimpleNamespace(text=BAD_CHEAP_WIT_TEXT)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_CHEAP_WIT_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert result.status == "blocked"
    assert result.final_report.cheap_wit_count >= 2
    assert result.final_report.perfect_comeback_run_count == 0
    assert result.final_report.motive_exposition_count >= 1


@pytest.mark.asyncio
async def test_quality_gate_blocks_structural_machine_text_without_blacklisted_phrases(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run_text_prompt(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        assert "动作对白绑定率" in kwargs["user_content"]
        assert "紧贴问答" in kwargs["user_content"]
        assert "短对白密度" in kwargs["user_content"]
        return SimpleNamespace(text=BAD_STRUCTURAL_MACHINE_TEXT)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_STRUCTURAL_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert len(calls) == 2
    assert result.status == "blocked"
    assert result.final_report.action_quote_paragraph_count >= 6
    assert result.final_report.tight_qa_pair_count >= 8
    assert result.final_report.short_dialogue_density > 0.45
    assert result.final_report.procedural_exposition_cluster_count >= 1


@pytest.mark.asyncio
async def test_quality_gate_cleans_residual_lexical_watchlist_terms(monkeypatch) -> None:
    candidate_with_residual_watchlist = (
        GOOD_CONFLICT_TEXT.strip()
        + "\n\n陈青把湿纸挪开一点，杨记账把册子夹在腋下，纸边还差半寸。"
    )

    async def fake_run_text_prompt(*args, **kwargs):
        return SimpleNamespace(text=candidate_with_residual_watchlist)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_DIALOGUE_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert "腋下" not in result.final_text
    assert "半寸" not in result.final_text
    assert "臂弯里" in result.final_text
    assert "一点" in result.final_text
    assert result.final_report.space_watchlist_hits == 0
    assert result.final_report.passed is True


@pytest.mark.asyncio
async def test_quality_gate_rejects_too_short_rewrite_and_keeps_original(monkeypatch) -> None:
    async def fake_run_text_prompt(*args, **kwargs):
        return SimpleNamespace(text="太短了。")

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=BAD_DIALOGUE_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert result.status == "blocked"
    assert result.final_text == BAD_DIALOGUE_MACHINE_TEXT
    assert result.rewrite_rounds == 2
    assert result.final_report.passed is False
    assert result.final_report.action_dialogue_beat_count >= 3


@pytest.mark.asyncio
async def test_quality_gate_tells_model_to_generalize_user_feedback(monkeypatch) -> None:
    bad = "少年喊了声老板。他把来意讲得很轻。门缝里透不出灯。"
    good = "他叫了一声：“老板。”\n“车没了，手机快没电了。我想在门厅坐到天亮。”\n门缝里没有灯光。"

    async def fake_run_text_prompt(*args, **kwargs):
        assert "先按规则族修" in kwargs["user_content"]
        assert "不要只替换用户点名的一句话" in kwargs["user_content"]
        assert "cross_project_prose_quality_contract" in kwargs["extra_system"]
        return SimpleNamespace(text=good)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=bad,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert result.status == "passed"
    assert result.initial_report.plain_contemporary_violation_count >= 2
    assert result.final_report.plain_contemporary_violation_count == 0


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
        text=BAD_DIALOGUE_MACHINE_TEXT,
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
        text=BAD_DIALOGUE_MACHINE_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        max_rewrite_rounds=3,
    )

    assert len(calls) == 3
    assert result.rewrite_rounds == 3
