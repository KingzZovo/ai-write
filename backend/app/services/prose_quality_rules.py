from __future__ import annotations

from dataclasses import dataclass

from app.services.prompts.anti_ai_rules_zh import render_anti_ai_prompt_block


@dataclass(frozen=True)
class ProseQualityRule:
    rule_id: str
    metric_names: tuple[str, ...]
    title: str
    root_cause: str
    bad_examples: tuple[str, ...]
    good_examples: tuple[str, ...]
    regex_patterns: tuple[str, ...]
    prompt_instruction: str


PROSE_QUALITY_RULES: tuple[ProseQualityRule, ...] = (
    ProseQualityRule(
        rule_id="plain_contemporary_chinese",
        metric_names=("pseudo_literary_register_count", "plain_contemporary_violation_count"),
        title="伪文学压缩腔",
        root_cause="普通现代场景被写成半文言、舞台说明或假文学压缩句。",
        bad_examples=(
            "少年喊了声师傅。",
            "他把来意说得很低。",
            "值守员终于抬头，问他干什么的。",
            "声音被关门声挡住一半。",
        ),
        good_examples=(
            "他叫了一声：“师傅。”",
            "值守员抬头：“干什么？”",
            "“车没了，手机快没电了。我想在门厅坐到天亮，不上楼，也不敲门。”",
            "门已经合上了。",
        ),
        regex_patterns=(
            r"喊了声(?:师傅|老板|阿姨|叔|大叔|婶|哥|姐|同学|老师|保安)",
            r"(?:把)?来意(?:说|讲|交代)得很(?:低|轻)",
            r"(?:把话|话|声音)?说得很低",
            r"终于抬头",
            r"声音被.{0,12}(?:挡住|盖住|拉散|压住)",
            r"把声音.{0,8}压得很低",
            r"声音.{0,8}压得很低",
        ),
        prompt_instruction=(
            "plain_contemporary_chinese：普通现代场景必须用完整、自然、当代的中文表达。"
            "不要写半文言、伪文学、为了显得干练而硬压缩的句子。"
            "发现坏例时先归因到规则族，再按好例方向改写。"
        ),
    ),
    ProseQualityRule(
        rule_id="semantic_collocation_completeness",
        metric_names=(
            "semantic_collocation_count",
            "abstract_evasion_count",
            "plain_contemporary_violation_count",
        ),
        title="正常汉语搭配缺失",
        root_cause="为了短、硬、深而省略现代汉语必须出现的词，或用抽象形容替代具体事实。",
        bad_examples=(
            "门缝里透不出灯。",
            "叫车页面刷出来的价格更难看。",
            "没有任何可以借口留下来的声音。",
            "手机还活着。",
        ),
        good_examples=(
            "门缝里没有灯光。",
            "叫车价格超过余额。",
            "里面没有人声，也没有开门动静。",
            "手机还没关机。",
        ),
        regex_patterns=(
            r"透不出灯(?!光)",
            r"(?:价格|车费|房费|房价|账单|金额|价钱).{0,14}(?:更)?难看",
            r"(?:可以|能|任何).{0,12}借口.{0,12}(?:声音|留下)",
            r"手机.{0,8}还活着",
        ),
        prompt_instruction=(
            "semantic_collocation_completeness：现代汉语搭配必须完整自然。"
            "写灯光、人声、余额、没关机等具体说法，不用抽象或故作深沉的搭配。"
        ),
    ),
    ProseQualityRule(
        rule_id="resource_and_scene_logic",
        metric_names=(
            "mundane_logic_violation_count",
            "resource_continuity_count",
            "scene_plausibility_count",
            "transport_logic_count",
            "shelter_cost_logic_count",
        ),
        title="生活逻辑与资源链断裂",
        root_cause="困境、氛围和行动没有经过钱、电量、交通、场地规则和人物行为的现实校验。",
        bad_examples=(
            "十八岁住旅馆被说未成年不行。",
            "附近没车，路人立刻坐上同类出租车。",
            "只剩口袋零钱，却还准备用手机叫车。",
            "烤肠摊卖临期面包、热水和汤。",
        ),
        good_examples=(
            "前台看了眼身份证，说今晚满房。",
            "叫车页面没人接单，他看了眼余额，又退出去。",
            "烤肠摊只卖烤肠，水自己带。",
        ),
        regex_patterns=(),
        prompt_instruction=(
            "resource_and_scene_logic：钱、手机、电量、交通、住宿和场地规则必须互相支撑。"
            "普通生活场景先按真实经验校验，再写氛围。"
        ),
    ),
    ProseQualityRule(
        rule_id="duplicate_explanation_control",
        metric_names=("duplicate_explanation_span_count", "hardship_stack_count", "abstract_evasion_count"),
        title="重复解释和金句式心理剖白",
        root_cause="同一压力、退路或心理结论被反复换说法，造成凑字数和金句堆叠。",
        bad_examples=(
            "所有能解释的退路都在变窄。",
            "同一场景连续两次解释回去会被赶、站着会被问、手机快没电。",
        ),
        good_examples=(
            "只保留一次具体压力链：活动室锁着、值守棚看不清、手机只剩百分之五。",
            "下一段让角色行动：去灯亮的门檐下问一句。",
        ),
        regex_patterns=(
            r"所有能解释的退路都在变窄",
            r"退路.{0,8}变窄",
            r"寄在别人生活边缘",
        ),
        prompt_instruction=(
            "duplicate_explanation_control：同一压力链只解释一次。"
            "心理剖白和金句不能连续堆叠，重复时改成行动推进。"
        ),
    ),
    ProseQualityRule(
        rule_id="dialogue_human_friction",
        metric_names=(
            "dialogue_symmetry_risk_count",
            "perfect_comeback_run_count",
            "cheap_wit_count",
            "story_bible_leakage_count",
        ),
        title="对话过度聪明和回合制",
        root_cause="角色完美接话、押节奏、抖机灵，或让路人朗读世界观设定。",
        bad_examples=(
            "踩烂了，算你买 / 先问问它算谁卖。",
            "路人讨论执行者、血裔、旧神、奥丁。",
        ),
        good_examples=(
            "踩坏了照价赔。",
            "路人只抱怨封路、停电、绕路、查得紧。",
        ),
        regex_patterns=(),
        prompt_instruction=(
            "dialogue_human_friction：角色不能完美接梗或连续短问短答。"
            "紧张场景要允许无视、抢白、答非所问、说半截和信息掉地。"
        ),
    ),
    ProseQualityRule(
        rule_id="chapter_level_anti_padding",
        metric_names=(
            "interiority_monologue_rate",
            "repeated_realization_run",
            "dialogue_paragraph_rate",
        ),
        title="章节级注水（重复心理剖白/独白堆叠）",
        root_cause="把同一洞察换比喻反复重述、整章心理剖白段落堆叠、对话与剧情密度过低，用内省凑字数。",
        bad_examples=(
            "他忽然意识到这栋楼并不普通，自己心里越来越清楚。",
            "他越来越觉得，自己像一件被搁置的麻烦。",
            "他终于明白，原来这里让他看见的，和真正想留下的，不是一回事。",
        ),
        good_examples=(
            "他叫了一声：“师傅。”值守员抬头：“干什么？”",
            "他把身份证放在台面上，水顺着袖口往下滴。",
        ),
        regex_patterns=(),
        prompt_instruction=(
            "chapter_level_anti_padding：每段必须带新信息（动作/对话/发现/关系变化）。"
            "同一洞察全章只写一次，禁止换比喻反复重述；不得连续堆叠心理剖白段。"
            "对话与有信息的叙述/动作要平衡：既不要用独白和金句凑字数，也不要走到另一个极端、"
            "用整章短对白墙或一句一段凑字数；每个自然段都要有实质内容。"
        ),
    ),
)


def rule_by_id(rule_id: str) -> ProseQualityRule:
    for rule in PROSE_QUALITY_RULES:
        if rule.rule_id == rule_id:
            return rule
    raise KeyError(rule_id)


def metric_names() -> tuple[str, ...]:
    names: list[str] = []
    for rule in PROSE_QUALITY_RULES:
        for name in rule.metric_names:
            if name not in names:
                names.append(name)
    return tuple(names)


def regex_patterns_for(rule_id: str) -> tuple[str, ...]:
    return rule_by_id(rule_id).regex_patterns


def render_prose_quality_prompt() -> str:
    lines = ["【cross_project_prose_quality_contract｜跨项目中文正文质量契约】"]
    lines.append("发现问题时先归因到规则族，不要只追加单词黑名单。")
    for rule in PROSE_QUALITY_RULES:
        lines.append(f"- {rule.rule_id}｜{rule.title}：{rule.prompt_instruction}")
        lines.append("  坏例：" + " / ".join(rule.bad_examples))
        lines.append("  好例：" + " / ".join(rule.good_examples))
    # QMAI-derived concrete blacklist + rewrite examples (single injection
    # point: this prompt feeds both contract_hard_gate_prompt and the
    # preflight blueprint, each of which renders it exactly once).
    lines.append(render_anti_ai_prompt_block())
    return "\n".join(lines)
