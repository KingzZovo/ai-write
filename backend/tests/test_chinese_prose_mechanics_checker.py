from app.services.chinese_prose_mechanics_checker import (
    analyze_chinese_prose_mechanics,
    build_generation_preflight_prompt,
)
from app.services.narrative_quality_gates import preflight_scene_blueprint_prompt


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


GOOD_CONFLICT_TEXT = """
邱成手背青筋微突，死死按住本子：“只露一行。多半个字，我立马撕了。”

“可以。”陈青没废话，“认错我赔钱。认对，这本东西今晚谁也别想碰，明早市书会见。”

陈青逼近半步。

邱成眼神瞬间警惕：“退回去。”

陈青没退，目光盯死那行字：“‘青儿药照前方，德安堂欠二钱’。”

“街上药单都这么写，算什么铁证？”

“德安堂老周记账，规矩是年月打头，病人在后。”陈青声音冷硬，“只有我祖母去赊药，怕老眼昏花抓错，才会把我的名字强行顶在最前头。”
"""


BAD_SYMMETRIC_DIALOGUE_TEXT = """
“看一行。”
“就一行。”
“多看一个字呢？”
“你自己合上。”
“认错呢？”
“赔礼，再补湿损纸钱。”
“认对呢？”
“明早我带见证到市书会认旧物。”

“看一行。”
“就一行。”
“多看一个字呢？”
“你自己合上。”
“认错呢？”
“赔礼，再补湿损纸钱。”
“认对呢？”
“明早我带见证到市书会认旧物。”
"""

BAD_CHEAP_WIT_AND_MOTIVE_TEXT = """
“踩烂了，算你买。”
“先问问它算谁卖。”
“你别装了。”
“我装什么？”
“你刚才说看一行，现在又想看，你就是想赖账。”
"""

PLAIN_SHORT_EXCHANGE_TEXT = """
“封条呢？”
“在后面。”
“灯籍？”
“我去拿。”
"""

BAD_MACHINE_LADDER_TEXT = """
“退开。”
“我不碰。”
“你刚才说看一行。”
“就一行。”
“多看半个字。”
“我自己合上。”
"""


BAD_SPATIAL_AND_BIO_TEXT = """
陈青往前挪了一步，脚尖仍停在柜台影子外。
册页从书口滑出一指宽，邱成站在柜台前三步外。
“六岁到十六岁，我替陈玉枝跑德安堂。老周记账，先写月份，再写病人，再写药钱。怕抓错药，家里人才把‘青儿’写在前头。”
"""


BAD_WIDER_SPATIAL_TEXT = """
邱成站在几步外，一尺宽的柜台影子旁，陈青又退了半尺。
册页压在一丈外的桌沿。
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

BAD_STORY_BIBLE_LEAKAGE_TEXT = """
便利店里，收银台后的女人压低声音：“执行者昨晚又去南环了，听说那帮血裔闹得很凶，旧神那边也不太安生。”

楼下大爷接话：“奥丁那条线，你少往外说，网上都传开了。”

旁边人跟着附和：“血脉一乱，城里就没清静过。”
"""

BAD_DIRECTIONAL_LISTING_TEXT = """
左边一排旧信箱都合着口，右墙贴着张褪色通知。东头有连廊，西头是楼梯口，前头还拐了个弯，后头又接着一条窄道。

陈青站在门口，眼睛被这些方位词绕得发晕。
"""

GOOD_CONTRAST_SPATIAL_TEXT = """
门板是烂透的旧铁皮，旁边却死死钉着一块崭新的金属门牌，亮得刺眼。

门没锁，扣舌一送，里头露出来的楼道干得发亮。
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

BAD_GENERAL_NATURALNESS_TEXT = """
他刚在手机上叫车，页面转了半天才说附近没车。口袋里只剩几枚硬币，明早早饭都不够，却还想着如果车来了就坐回出租屋。

公交站旁边的人接了电话，转身就钻进一辆出租车，车灯一晃就走远了。

烤肠摊老板把夹子搁在锅沿，抬头说：“别挡着锅，雨都飘进去了。”

炸香味扑过来，他的胃反而更空。

那道光不亮，却很稳。他看见门缝里没潮气，就走过去推门。
"""

BAD_AGE_LOGIC_AND_TRIAL_ERROR_TEXT = """
他十八岁，站在旅馆前台，前台却说未成年不行。
兜里剩下的钱不多，够明早买点吃的，不够住旅馆，更不够在雨夜里慢慢试错。
那道光不亮，却很稳，门缝里没潮气，他还是推门进去。
"""

GOOD_AGE_LOGIC_AND_TRIAL_ERROR_TEXT = """
他十八岁，站在旅馆前台，前台看了眼身份证，说今晚满房。
兜里那点钱只够明早买早饭，不够多撑一晚。
门口的灯亮着，他没有进去，转身去找别的地方躲雨。
"""

BAD_PRICE_LOGIC_TEXT = """
烤肠摊前挂着一杯白开水，两块钱。
他站了半天，最后还是没买，只买了根烤肠。
"""

GOOD_PRICE_LOGIC_TEXT = """
烤肠摊老板把纸巾盒往旁边推了推，抬头说：“水自己带，烤肠两块。”
他点点头，只买了根烤肠。
"""

BAD_TRANSPORT_LOGIC_TEXT = """
末班车早走了，最近一趟换乘也赶不上。
"""

GOOD_TRANSPORT_LOGIC_TEXT = """
末班车已经没了，夜班线路绕远，他等不起。
"""

BAD_SEMANTIC_COLLOCATION_TEXT = """
门缝里透不出灯，也没有任何可以借口留下来的声音。

叫车页面刷出来的价格比他身上能拿出来的钱更难看。
"""

GOOD_SEMANTIC_COLLOCATION_TEXT = """
门缝里没有灯光，也听不见人声。

叫车价格超过余额。
"""

BAD_SHELTER_COST_LOGIC_TEXT = """
真进去，先得拿证件、交押金，再被前台隔着柜台从头看到脚。他现在连把背包放干的地方都没有，没力气再去碰这种脸色。
"""

GOOD_SHELTER_COST_LOGIC_TEXT = """
旅馆要押金，他的余额不够。前台看完身份证说满房，他只好退回雨里。
"""

GOOD_GENERAL_NATURALNESS_TEXT = """
叫车页面转了半天，没有司机接单。他看了眼余额，又退出去，手机只剩最后一格电。

公交站旁边的人接了电话，撑伞走进雨里，没有再等。

烤肠摊老板把夹子往锅里一放：“别站锅前面，雨都被你带进棚里了。”

香味被风吹过来，他才想起晚饭没吃。

车棚尽头漏出一线暖黄的灯。门槛前却是干的，雨水到那里断开。他在门洞里冻得发抖，回头看见岗亭的窗又合上了，才咬牙往那边走。
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


def test_detects_dialogue_machine_traces() -> None:
    report = analyze_chinese_prose_mechanics(BAD_DIALOGUE_MACHINE_TEXT)

    assert report.action_dialogue_beat_count >= 3
    assert report.prop_fiddling_count >= 2
    assert report.explicit_pause_marker_count >= 1
    assert report.direct_intent_exposition_count >= 2
    assert not report.passed


def test_allows_asymmetric_conflict_with_dynamic_half_step() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_CONFLICT_TEXT)

    assert report.action_dialogue_beat_count <= 1
    assert report.prop_fiddling_count == 0
    assert report.explicit_pause_marker_count == 0
    assert report.direct_intent_exposition_count == 0
    assert report.dialogue_symmetry_risk_count == 0
    assert report.duplicate_short_dialogue_ladder_count == 0
    assert report.spatial_mapping_count == 0
    assert report.biographical_infodump_count == 0
    assert report.short_sentence_runs_over_target == 0
    assert report.passed


def test_detects_symmetric_short_dialogue_and_duplicate_ladder() -> None:
    report = analyze_chinese_prose_mechanics(BAD_SYMMETRIC_DIALOGUE_TEXT)

    assert report.dialogue_symmetry_risk_count >= 1
    assert report.perfect_comeback_run_count >= 1
    assert report.duplicate_short_dialogue_ladder_count >= 1
    assert not report.passed


def test_detects_cheap_wit_and_motive_exposition() -> None:
    report = analyze_chinese_prose_mechanics(BAD_CHEAP_WIT_AND_MOTIVE_TEXT)

    assert report.cheap_wit_count >= 2
    assert report.perfect_comeback_run_count == 0
    assert report.motive_exposition_count >= 1
    assert not report.passed


def test_allows_plain_brief_short_dialogue_exchange() -> None:
    report = analyze_chinese_prose_mechanics(PLAIN_SHORT_EXCHANGE_TEXT)

    assert report.dialogue_symmetry_risk_count == 0
    assert report.perfect_comeback_run_count == 0
    assert report.duplicate_short_dialogue_ladder_count == 0
    assert report.passed


def test_detects_cue_based_short_dialogue_ladder() -> None:
    report = analyze_chinese_prose_mechanics(BAD_MACHINE_LADDER_TEXT)

    assert report.dialogue_symmetry_risk_count == 0
    assert report.perfect_comeback_run_count >= 1
    assert not report.passed


def test_detects_static_spatial_mapping_and_biographical_infodump() -> None:
    report = analyze_chinese_prose_mechanics(BAD_SPATIAL_AND_BIO_TEXT)

    assert report.spatial_mapping_count >= 3
    assert report.biographical_infodump_count >= 1
    assert not report.passed


def test_detects_wider_static_measure_words() -> None:
    report = analyze_chinese_prose_mechanics(BAD_WIDER_SPATIAL_TEXT)

    assert report.spatial_mapping_count >= 3
    assert not report.passed


def test_detects_structural_machine_topology_beyond_blacklisted_phrases() -> None:
    report = analyze_chinese_prose_mechanics(BAD_STRUCTURAL_MACHINE_TEXT)

    assert report.action_quote_paragraph_count >= 6
    assert report.action_quote_paragraph_rate > 0.35
    assert report.tight_qa_pair_count >= 8
    assert report.short_dialogue_density > 0.45
    assert report.short_paragraph_density > 0.35
    assert report.procedural_exposition_cluster_count >= 1
    assert not report.passed


def test_detects_story_bible_leakage_in_public_dialogue() -> None:
    report = analyze_chinese_prose_mechanics(BAD_STORY_BIBLE_LEAKAGE_TEXT)

    assert report.story_bible_leakage_count >= 1
    assert not report.passed


def test_allows_grounded_public_conflict_without_setting_name_leakage() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_CONFLICT_TEXT)

    assert report.story_bible_leakage_count == 0
    assert report.passed


def test_detects_directional_listing_and_allows_single_contrast_point() -> None:
    report = analyze_chinese_prose_mechanics(BAD_DIRECTIONAL_LISTING_TEXT)

    assert report.directional_listing_count >= 1
    assert not report.passed


def test_allows_single_contrast_point_without_directional_listing() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_CONTRAST_SPATIAL_TEXT)

    assert report.directional_listing_count == 0
    assert report.passed


def test_detects_mundane_logic_pov_and_register_problems() -> None:
    report = analyze_chinese_prose_mechanics(BAD_MUNDANE_NATURALNESS_TEXT)

    assert report.mundane_logic_violation_count >= 3
    assert report.limited_pov_leak_count >= 1
    assert report.awkward_register_count >= 3
    assert report.hardship_stack_count >= 1
    assert not report.passed


def test_allows_plain_modern_limited_pov_scene() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_MUNDANE_NATURALNESS_TEXT)

    assert report.mundane_logic_violation_count == 0
    assert report.limited_pov_leak_count == 0
    assert report.awkward_register_count == 0
    assert report.hardship_stack_count == 0
    assert report.passed


def test_detects_general_resource_action_and_motivation_gaps() -> None:
    report = analyze_chinese_prose_mechanics(BAD_GENERAL_NATURALNESS_TEXT)

    assert report.resource_continuity_count >= 1
    assert report.scene_plausibility_count >= 1
    assert report.action_causality_count >= 1
    assert report.mundane_register_count >= 1
    assert report.motivation_gap_count >= 1
    assert not report.passed


def test_allows_generalized_natural_motivation_and_resource_flow() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_GENERAL_NATURALNESS_TEXT)

    assert report.resource_continuity_count == 0
    assert report.scene_plausibility_count == 0
    assert report.action_causality_count == 0
    assert report.mundane_register_count == 0
    assert report.motivation_gap_count == 0
    assert report.passed


def test_detects_age_logic_and_trial_error_abstraction() -> None:
    report = analyze_chinese_prose_mechanics(BAD_AGE_LOGIC_AND_TRIAL_ERROR_TEXT)

    assert report.mundane_logic_violation_count >= 1
    assert report.mundane_register_count >= 2
    assert report.motivation_gap_count >= 1
    assert not report.passed


def test_allows_age_logic_and_trial_error_without_mechanical_language() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_AGE_LOGIC_AND_TRIAL_ERROR_TEXT)

    assert report.mundane_logic_violation_count == 0
    assert report.mundane_register_count == 0
    assert report.motivation_gap_count == 0
    assert report.passed


def test_detects_white_water_price_logic() -> None:
    report = analyze_chinese_prose_mechanics(BAD_PRICE_LOGIC_TEXT)

    assert report.mundane_logic_violation_count >= 1
    assert not report.passed


def test_allows_normal_shelter_price_logic() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_PRICE_LOGIC_TEXT)

    assert report.mundane_logic_violation_count == 0
    assert report.passed


def test_detects_transport_route_contradiction() -> None:
    report = analyze_chinese_prose_mechanics(BAD_TRANSPORT_LOGIC_TEXT)

    assert report.transport_logic_count >= 1
    assert not report.passed


def test_allows_transport_route_without_false_transfer() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_TRANSPORT_LOGIC_TEXT)

    assert report.transport_logic_count == 0
    assert report.passed


def test_detects_bad_semantic_collocations_and_abstract_evasion() -> None:
    report = analyze_chinese_prose_mechanics(BAD_SEMANTIC_COLLOCATION_TEXT)

    assert report.semantic_collocation_count >= 2
    assert report.abstract_evasion_count >= 1
    assert not report.passed


def test_allows_plain_semantic_collocations() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_SEMANTIC_COLLOCATION_TEXT)

    assert report.semantic_collocation_count == 0
    assert report.abstract_evasion_count == 0
    assert report.passed


def test_detects_shelter_cost_logic_evasion() -> None:
    report = analyze_chinese_prose_mechanics(BAD_SHELTER_COST_LOGIC_TEXT)

    assert report.shelter_cost_logic_count >= 1
    assert report.abstract_evasion_count >= 1
    assert not report.passed


def test_allows_concrete_shelter_cost_logic() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_SHELTER_COST_LOGIC_TEXT)

    assert report.shelter_cost_logic_count == 0
    assert report.abstract_evasion_count == 0
    assert report.passed


def test_detects_pseudo_literary_compressed_register() -> None:
    report = analyze_chinese_prose_mechanics(BAD_PSEUDO_LITERARY_REGISTER_TEXT)

    assert report.pseudo_literary_register_count >= 3
    assert not report.passed


def test_allows_plain_contemporary_register() -> None:
    report = analyze_chinese_prose_mechanics(GOOD_PLAIN_CONTEMPORARY_REGISTER_TEXT)

    assert report.pseudo_literary_register_count == 0
    assert report.passed


def test_generation_preflight_uses_soft_4000_word_target_without_filler_pressure() -> None:
    prompt = build_generation_preflight_prompt(analyze_chinese_prose_mechanics(""))

    assert "约 4000 字" in prompt
    assert "2000-6000" in prompt
    assert "不要为凑字数" in prompt
    assert "5200-5600" not in prompt
    assert "不得低于 5000" not in prompt
    assert "transport_logic" in prompt
    assert "semantic_collocation" in prompt
    assert "shelter_cost_logic" in prompt
    assert "abstract_evasion" in prompt
    assert "plain_contemporary_chinese" in prompt
    assert "pseudo_literary_register" in prompt
    assert "cross_project_prose_quality_contract" in prompt
    assert "先归因到规则族" in prompt
    assert "plain_contemporary_violation_count" in prompt
    assert "duplicate_explanation_span_count" in prompt


def test_detects_general_plain_contemporary_violations_not_only_original_phrase() -> None:
    text = """
    少年喊了声老板。门房终于抬头，问他找谁。
    他把来意讲得很轻，说手机快没电，想在门厅坐到天亮。
    门缝里透不出灯，也听不见任何可以借口留下来的声音。
    手机还活着，他就没有再问。
    """

    report = analyze_chinese_prose_mechanics(text)

    assert report.pseudo_literary_register_count >= 2
    assert report.semantic_collocation_count >= 2
    assert report.plain_contemporary_violation_count >= 4
    assert not report.passed


def test_allows_general_plain_contemporary_rewrite() -> None:
    text = """
    他叫了一声：“老板。”
    门房抬头：“找谁？”
    “车没了，手机快没电了。我想在门厅坐到天亮，不上楼，也不敲门。”
    门缝里没有灯光，也听不见人声。手机还没关机，他先把它收起来。
    """

    report = analyze_chinese_prose_mechanics(text)

    assert report.pseudo_literary_register_count == 0
    assert report.semantic_collocation_count == 0
    assert report.plain_contemporary_violation_count == 0
    assert report.passed


def test_detects_duplicate_explanation_spans() -> None:
    text = """
    回去，值守员多半会让他别挡门；继续站在这里，住户出来一次，他就要解释一次。手机已经只剩百分之五。
    他看了眼活动室的锁，又看了眼值守棚。回去可能被赶出来，继续站在这里，住户出来一次，他就要解释一次。
    """

    report = analyze_chinese_prose_mechanics(text)

    assert report.duplicate_explanation_span_count >= 1
    assert not report.passed


def test_user_feedback_lessons_generalize_across_variants() -> None:
    bad_samples = [
        "少年喊了声阿姨。她终于抬头，问他干嘛。",
        "他把来意讲得很轻，说自己只想躲到天亮。",
        "门缝里透不出灯，也没有可以借口留下来的声音。",
        "手机还活着，他就继续往前。",
        "所有能解释的退路都在变窄。",
    ]

    for sample in bad_samples:
        report = analyze_chinese_prose_mechanics(sample)
        assert not report.passed, sample
        assert (
            report.plain_contemporary_violation_count
            + report.duplicate_explanation_span_count
            + report.semantic_collocation_count
            + report.abstract_evasion_count
        ) >= 1, sample


def test_preflight_prompt_contains_dialogue_machine_constraints() -> None:
    prompt = preflight_scene_blueprint_prompt(chapter_idx=1)

    assert "communication_damping" in prompt
    assert "plain_register_no_wit" in prompt
    assert "focal_measure_only" in prompt
    assert "motive_exposition_zero" in prompt
    assert "floating_dialogue_exchange" in prompt
    assert "dialogue_symmetry_break" in prompt
    assert "prop_fiddling_guard" in prompt
    assert "explicit_pause_marker_zero" in prompt
    assert "subtext_occlusion" in prompt
    assert "spatial_mapping_zero" in prompt
    assert "biographical_infodump_zero" in prompt
    assert "算你买" in prompt
    assert "算谁卖" in prompt
    assert "安静了一会儿" in prompt
    assert "你怕我" in prompt
    assert "原段落（节拍器式机器痕迹）" in prompt
    assert "说好一行" in prompt
    assert "看一行" in prompt
    assert "三步外" in prompt
    assert "一尺" in prompt
    assert "一丈" in prompt
    assert "六岁到十六岁" in prompt
    assert "动作对白绑定率" in prompt
    assert "紧贴问答" in prompt
    assert "短对白密度" in prompt
    assert "story_bible_leakage_zero" in prompt
    assert "setting_name_dialogue_zero" in prompt
    assert "directional_listing_zero" in prompt
    assert "mundane_scene_plausibility" in prompt
    assert "plain_modern_register" in prompt
    assert "limited_pov_only" in prompt
    assert "semantic_density_budget" in prompt
    assert "resource_continuity" in prompt
    assert "action_causality" in prompt
    assert "motivation_bridge" in prompt
    assert "age_plausibility" in prompt
    assert "试错" in prompt
    assert "dialogue_topology_limit" in prompt


def test_plain_contemporary_aggregate_does_not_double_count_overlapping_spans() -> None:
    # One span ("可以借口留下来的声音") matches BOTH the semantic-collocation and
    # abstract-evasion families. The aggregate must count the span once, not once
    # per family — otherwise the human-facing audit number is inflated.
    text = "门缝里没有任何可以借口留下来的声音。"

    r = analyze_chinese_prose_mechanics(text)

    assert r.semantic_collocation_count >= 1
    assert r.abstract_evasion_count >= 1
    assert r.plain_contemporary_violation_count < (
        r.semantic_collocation_count + r.abstract_evasion_count
    )
