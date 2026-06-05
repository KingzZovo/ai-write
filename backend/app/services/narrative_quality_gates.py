"""Genre-agnostic narrative generation principles.

This module turns evaluator issue tags into reusable, cross-project writing
principles.  These rules are meant to be internalized before generation and
used for diagnostics after scoring. They should not be used as a post-output
blocking gate in the happy path.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.services.prose_quality_rules import render_prose_quality_prompt

VIOLATION_TAG_RE = re.compile(r"\[([a-z_]+_violation)\]")

@dataclass(frozen=True)
class QualityGateRule:
    tag: str
    title: str
    root_cause: str
    detection: str
    repair_action: str
    hard_gate: str  # pre-generation principle; not a post-output blocker

ALLOWED_REPAIR_ACTIONS: tuple[str, ...] = (
    "补支撑",
    "降级结果",
    "删除冲突设定",
    "拆分人物或资源",
    "提前埋伏笔",
)

QUALITY_GATE_RULES: dict[str, QualityGateRule] = {
    "time_rule_violation": QualityGateRule(
        tag="time_rule_violation",
        title="高压时序预算",
        root_cause="高压状态下仍让角色完成过长对白、整理、换装、拆验、传递或训练流程，时间压力没有约束行动。",
        detection="追捕/封锁/压近/倒计时/敌方绕回等压力词出现后，连续解释、问答、整理证据或训练口径过长。",
        repair_action="补具体时间差与打断；删减流程；让行动失败、受伤、遗失物件或只获得弱线索。",
        hard_gate="高压段必须缩短、打断或付代价；不得完整完成多步低风险流程。",
    ),
    "space_rule_violation": QualityGateRule(
        tag="space_rule_violation",
        title="空间路径与同步",
        root_cause="人物、物件、消息刚好出现、瞬间汇合或同步抵达，缺少入口、路径、阻隔、时间差和见证锚点。",
        detection="突然/刚好/赶到/现身/汇合/已在/转入/退走等跃迁词附近缺少前置路径或滞留原因。",
        repair_action="补入口、路线、阻隔和滞留原因；若补不了，降级为只见尾段、只留痕迹或延后回收。",
        hard_gate="每次关键出现或转运必须有路径锚点；没有路径就不得完整目击或直接对话。",
    ),
    "information_rule_violation": QualityGateRule(
        tag="information_rule_violation",
        title="信息权限与证据链",
        root_cause="角色知道了未亲见、未转述、未被物证支持、未被制度公开的信息；推理从碎片跳到身份/意图/机制结论。",
        detection="判断词、身份词、意图词、制度词、定性词附近缺少来源：亲见/转述/物证/旧账/公开制度/可验证痕迹。",
        repair_action="补来源、可信度和替代解释；把结论降级为假设/疑点；让信息只泄露碎片。",
        hard_gate="任何强判断必须绑定来源锚点；来源不足时只能写疑点，不能写定案。",
    ),
    "mechanism_rule_violation": QualityGateRule(
        tag="mechanism_rule_violation",
        title="机制与物证边界",
        root_cause="机关、能力、物证、反应或证据用途只给效果，不给条件、边界、污染风险、可见特征和不可确认范围。",
        detection="物证/机关/反应/残留/封签/能力被用作关键推理或行动，但没有可观察细节、保护动作或边界表述。",
        repair_action="补触发条件、可见特征、成本、副作用、污染风险和不可确认范围；必要时降级为疑似。",
        hard_gate="机制必须像可运行流程；物证必须保护边界；不能用抽象反应直接确认真相。",
    ),
    "power_resource_violation": QualityGateRule(
        tag="power_resource_violation",
        title="资源差与行动代价",
        root_cause="低资源方连续压制或骗过高资源方，高资源方免费让步，缺少制度限制、视线死角、外部压力或低资源方损失。",
        detection="通缉、强势组织、官府、军队、富户、监管者在场时，弱势方连续成功且无代价。",
        repair_action="补高资源方限制和低资源方代价；拆分帮助者；让部分帮助失败或只制造片刻窗口。",
        hard_gate="强势方失手必须有约束；弱势方成功必须付代价；多人援助不得整齐无损。",
    ),
    "result_strength_violation": QualityGateRule(
        tag="result_strength_violation",
        title="线索强度与结果降级",
        root_cause="章节节点尤其章末把疑点写成确认，把方向写成答案，把局部收获写成可靠胜利。",
        detection="章末或关键节点出现确认/坐实/直接听到/就是/已经/真相/明确指向等强确认，且大纲只要求可复核疑点。",
        repair_action="改成半截信息、异常痕迹、可复核疑点、诱饵风险或后续待验项。",
        hard_gate="章末只许给方向和疑点；不得提前确认身份、机制、阵营或完整答案。",
    ),
    "expression_contract_violation": QualityGateRule(
        tag="expression_contract_violation",
        title="表达契约与压力风格",
        root_cause="高压场景中喜剧插科、说明书式对白、程序清单、作者总结或现代管理词过密，削弱沉浸和危机。",
        detection="追捕/封锁/问供/对峙段中连续俏皮话、三项以上信息清单、口号式总结或解释性对白。",
        repair_action="压缩为短句、动作、打断、碎片对白和角色推断；删除重复清单和口号式总结。",
        hard_gate="高压段对白每次最多承载两个信息点；说明必须被动作和风险打断。",
    ),
}

BLOCKING_CONTRACT_VIOLATION_TYPES: frozenset[str] = frozenset(QUALITY_GATE_RULES)

REUSABLE_REVISE_BUCKETS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("空间/时间连续性", ("space_rule_violation", "time_rule_violation"), "先补路径、时间差、阻隔和代价；高压链过长时删动作、拆场或降级结果。"),
    ("证据强度与结论降级", ("result_strength_violation", "information_rule_violation"), "所有判断绑定来源；来源不足时降级为疑点、佐证、待验实物或后续线索。"),
    ("资源差与行动代价", ("power_resource_violation",), "强势方失手必须有制度/视线/舆论/分兵限制；弱势方成功必须有损失或只得到片刻窗口。"),
    ("机制/物证边界", ("mechanism_rule_violation",), "补可见特征、触发条件、污染风险和不可确认范围；不能让道具成为万能答案。"),
    ("语体契约/压力对白", ("expression_contract_violation",), "删喜剧噪声和清单复述；用短句、打断、动作观察替代说明书式对白。"),
)


CHINESE_PROSE_MECHANICS_PROMPT = """\
【中文行文机械约束（跨章节/跨项目，生成前内化）】
这些规则约束句法、动词、视角、复述和物理动作，不是某一章特化补丁。写正文前必须内部自查，但不要输出自查表。
- 句子呼吸感：长短句结合。禁止连续出现四句以上的短句；环境描写用较长句顺着人物视线、听觉或行动轨迹铺陈，核心动作、危险反应和判断转折用短句提速。
- 朴素动词优先：基础动作使用日常白话动词，如看、走、停、拿、放、转身、低头、退后、靠近；不要为追求古风、干练或短促而频繁使用生僻、拗口或过度文学化动词。
- 克制动词堆砌：动作描写必须准确自然，同一小段内不要连续堆叠多个强动词；能用一个清楚动词完成的动作，不拆成多个硬挤的动词。
- 禁止生造动宾短语：严格遵守中文基础词语搭配。写“走到檐下”，不要写“进檐”；写“试探鼻息”，不要写“探鼻”；宁可朴素准确，也不要压缩成不通顺的词组。
- 空间和动作符合物理常识：人物与环境互动必须使用准确方位介词和可执行动作。屋檐只能“退至檐下/站在檐下/走到檐下”，雨网只能“穿过/步入/站进雨里”；不得把地点、遮挡、天气或器物写成不合物理的动作对象。
- 视角流动：环境细节必须通过人物视线、身体移动、听觉来源或行动路线自然带出；禁止清单罗列式说明，禁止脱离 POV 的全知式摆设盘点。
- 重复信息处理：角色复述他人长段台词、传闻、供词或制度说明时，只能用概括性侧写和少量关键词承接，禁止全文复述；重复信息必须产生新反应、新判断或新代价，否则合并或删除。
- 拒绝动作切片：不要把一个连贯动作拆成僵硬步骤说明。能写“他走到门边停下”，不要拆成“他抬脚、迈步、落脚、转身、停住”；除非每一步都承担风险、观察或代价。
- 视角与动作联动：环境铺陈先绑定“谁在看/听/走向哪里”，再写被看见的物件和空间关系；动作句必须能回答“身体在哪里、朝哪里、凭什么做到”。
- communication_damping：密集交锋不必每句都接住；允许无视、岔开、迟钝、重复尾音、信息掉在地上或被环境打断，不要让所有人像带提词器一样无缝对答。
- plain_register_no_wit：日常护财、试探、讨价还价和街头冲突必须服从人物当下情绪，禁止廉价机智、抖机灵、对仗式反击和硬凹“聪明”；粗鄙直接比俏皮更真实。
- focal_measure_only：数字化距离只在生死、翻脸、机密暴露或必须对齐证物的焦点时刻使用；普通走位、站立、互动和压迫不要写成坐标测绘。
- motive_exposition_zero：禁止把角色或对方的底层动机直接说破；不要写“你就是想赖账/你其实……”这类拆解句，改用反问、压价、动作和结果施压。
- floating_dialogue_exchange：密集交锋、讨价还价、逼问、互相试探时，必须允许连续纯台词交替。不要给每句台词前都分配“弯腰、抬手、低头、看了、拿起、放下”等伴随动作；动作只留给真正改变局面的一拍。
- dialogue_symmetry_break：高压对话禁止连续四组以上短问短答，禁止镜像复述对方句式。必须加入答非所问、抢白、动作打断或直接抛结论，让交锋像活人临场反应，不像排比口令。尤其禁用“说好一行 / 就一行 / 多看一个字呢 / 你自己合上 / 认错呢 / 认对呢 / 明早我带见证到市书会认旧物”这种口诀式对仗。
- prop_fiddling_guard：禁止用拨算盘、绕细绳、擦砚台、摸杯子、挪纸张等摆弄道具来凑画面。道具动作必须承担物理阻挡、掩饰心虚、转移证物或情绪爆发，否则删除。
- explicit_pause_marker_zero：禁止“安静了一会儿、沉默了、没有立刻回话、一小会儿、半晌、顿了顿、停了一下”等显性停顿标记。停顿只能由风声、木铃、脚步、纸页响动、视线转移或直接切入下一句形成。
- subtext_occlusion：交锋对白必须藏住潜台词。禁止“你怕我……、你想让……、我想让你……、你其实……”这类直接拆穿真实意图的句式；改用反问、施压动作、避答、转价码或局部事实逼对方露出破绽。
- spatial_mapping_zero：禁用“三步外、一指宽、影子外、几寸、一尺、一丈、几步、几尺、几丈”等静态物理测绘词。人物位置只用逼近、退开、让开、挡住、压住等动态趋势或遮挡关系表达；“半步”只可用于动态压迫。
- biographical_infodump_zero：角色用往事作证时最多两句，禁止“从X岁到X岁”或按时间轴背履历。事实必须直接切进当前冲突。
- story_bible_leakage_zero：禁止把世界观设定、体系名、等级名、主角能力名、组织名和终局谜团集中塞进广告、海报、新闻、路人闲聊或旁白说明。隐藏世界必须先以异常、误认、局部痕迹和人物反应出现；POV 角色不知道的专名不得提前命名。
- setting_name_dialogue_zero：路人、新闻、店员、邻居、广告、海报和闲聊不能字正腔圆讨论核心世界观名词。超自然影响必须降维成封路、停电、绕路、物价、黑车、查得紧、上面、那帮人、那种事、清道等生活抱怨和代词。
- directional_listing_zero：环境描写禁止“左边/右边/东头/西头/前后”等导览式罗列。只抓一个与当前氛围或剧情冲突的核心反差点，砍掉无关陈设扫描。
- mundane_scene_plausibility：街边摊、便利店、食堂、保安、房东催租等日常场景必须符合真实生活经验。烤肠摊不要顺手卖临期面包、汤锅或热水，便利店不要硬塞消毒水味和温水，大雨院子里不要安排人群围电视。
- plain_modern_register：能用正常现代汉语就用正常说法。写“锁屏/按灭屏幕”“别挡着锅”“别往楼里跑/别进去”，不要写“把手机按黑”“别挡锅”“别碰那边”“带了急”这类别扭压缩词。
- plain_contemporary_chinese：日常场景、求助、买东西、登记、问路和避雨必须使用完整自然的现代中文，不要为了显得干练、有文气或冷硬而压缩成半文言/伪文学表达。写“他叫了一声：‘师傅。’”“车没了，手机快没电了”，不要写“喊了声师傅”“来意说得很低”“终于抬头”“声音被关门声挡住一半”。
- age_plausibility：公共场景里的拒绝、催促和登记要与年龄身份一致。十八岁去旅馆不要写成“未成年不行”；可用满房、押金、证件、前台态度、关门时间等真实阻力。
- abstract_reasoning_zero：删除“试错”“本质上”“底层逻辑”“慢慢试错”这类元语言解释，把原因落到余额、体力、时间、电量、路况和退路上。
- limited_pov_only：第三人称有限视角只写主角能看见、听见、感觉到的事实，不替别人下心理结论，不写“才想起他、没人记得他、没人问他”这类越界旁白。
- semantic_density_budget：开篇苦难感和边缘化信息只保留两三个与当前困境直接相关的物理痛点，不要把老师、门卫、班群、食堂、房东同质标签堆成清单。
- resource_continuity：钱、手机、电量、支付方式、交通选择要前后一致。能在手机上叫车就意味着移动支付/账户语境，不能同时把行动能力写成只剩口袋零钱；附近无车也不能让同场路人无解释坐上同类出租车。
- action_causality：动作、空间和结果必须能成立。不要写挡着锅却雨水进锅、香味直接导致胃更空、看到灯就进入陌生门这类因果跳跃。
- motivation_bridge：角色进入陌生建筑、异常门或私人空间前，必须有压力、退路关闭、误判理由、求生诱因或外部催逼，不能只因一线光、门没锁或门缝干燥就主动进门。
- dialogue_topology_limit：写完每个场景前必须检查对白拓扑。连续含引号段落不得超过四段；连续纯短对白不得超过两段；每个场景紧贴问答不得超过三组。超过时必须把部分问答改成动作结果、误解、环境打断、未答、抢白、概括性侧写或证物推进。
- 结构拓扑自检：动作对白绑定率不得超过 0.35；如果多数段落都是“人物做微动作 + 台词”，必须合并、删除或改成纯台词/证物推进。
- 紧贴问答限制：紧贴问答不得超过 12 组；禁止让角色连续完美接话，必须出现无视、抢白、答非所问、说半截、证物打断或环境打断。
- 短对白密度限制：短对白密度不得过半；短句可以用于压迫，但不能把整章写成剧本回合或口令梯子。
- 短段落密度限制：短段落密度不得超过 0.35；同一动作、同一证据链和同一轮施压应合并成有呼吸的自然段。
- 程序性解释簇限制：旧库、码头、灯籍、封存、回封、待验等制度/证据说明不能由专家 NPC 一口气讲完，必须拆进物证、争执、误解和局部记录中。
"""

def issue_violation_type(issue: dict[str, Any]) -> str:
    for key in ("violation_type", "type", "tag"):
        raw = issue.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    text = " ".join(str(issue.get(key) or "") for key in ("description", "suggestion"))
    match = VIOLATION_TAG_RE.search(text)
    return match.group(1) if match else ""

def coerce_issues(eval_obj: Any) -> list[dict[str, Any]]:
    if eval_obj is None:
        return []
    if isinstance(eval_obj, dict):
        raw = eval_obj.get("issues") or []
    else:
        raw = getattr(eval_obj, "issues", None) or []
    return [x for x in raw if isinstance(x, dict)]

def group_issues_by_violation_type(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        vtype = issue_violation_type(issue) or "untyped_issue"
        grouped.setdefault(vtype, []).append(issue)
    return grouped

def blocking_contract_violations(eval_obj: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for issue in coerce_issues(eval_obj):
        vtype = issue_violation_type(issue)
        if vtype in BLOCKING_CONTRACT_VIOLATION_TYPES and vtype not in seen:
            found.append(vtype)
            seen.add(vtype)
    return found

def blocking_contract_violation_set(eval_obj: Any) -> set[str]:
    return set(blocking_contract_violations(eval_obj))

def repair_method_for(vtype: str) -> str:
    rule = QUALITY_GATE_RULES.get(vtype)
    if rule:
        return f"根因：{rule.root_cause} 修复：{rule.repair_action} 硬门槛：{rule.hard_gate}"
    return "先判断问题属于补支撑、降级结果、删除冲突设定、拆分人物或资源、提前埋伏笔中的哪一种；不得随机重抽。"

def reusable_bucket_lines(issues: list[dict[str, Any]]) -> list[str]:
    grouped = group_issues_by_violation_type(issues)
    lines: list[str] = []
    for title, vtypes, instruction in REUSABLE_REVISE_BUCKETS:
        count = sum(len(grouped.get(vtype, [])) for vtype in vtypes)
        if count > 0:
            lines.append(f"- {title}（{count}）：{instruction}")
    return lines

def contract_hard_gate_prompt() -> str:
    lines = ["【生成前内化约束】以下规则适用于所有章节和项目，写正文前先在内部自检，不要等写完再靠后置流程纠错："]
    for rule in QUALITY_GATE_RULES.values():
        lines.append(f"- {rule.tag}｜{rule.title}：{rule.hard_gate} 检测：{rule.detection}")
    lines.append("若内部自检发现风险，优先用以下写作决策处理：" + " / ".join(ALLOWED_REPAIR_ACTIONS) + "；目标是第一稿直接满足要求，评分后仅作诊断。")
    lines.append(CHINESE_PROSE_MECHANICS_PROMPT)
    lines.append(render_prose_quality_prompt())
    lines.append(
        "cross_project_prose_quality_contract 必须执行；发现问题时先归因到规则族，不要只追加单词黑名单。"
        "plain_contemporary_violation_count 必须为 0；普通现代场景必须使用完整自然的现代中文。"
        "duplicate_explanation_span_count 必须为 0；同一压力链、退路解释和心理判断只保留一次，重复时改成行动推进。"
    )
    return "\n".join(lines)

def preflight_scene_blueprint_prompt(chapter_idx: int | None = None) -> str:
    """Return generic direct-generation-first v4.9 micro-continuity budget guidance.

    Cross-project / cross-chapter scaffold. This is not a post-output blocking
    gate, not a repair loop, and not a scene_planner JSON plan. It forces the
    model to internally convert the current chapter outline into executable
    units, allocate movement/resource/information/expression/result budgets,
    then expand only budgeted and anchored units at the allowed strength.
    """
    chapter_label = f"第{chapter_idx}章" if chapter_idx else "本章"
    return f"""
【direct_generation_first_v4.13｜空间可行性、信息遮蔽、巧合摩擦与中文行文约束】
目标：{chapter_label}写正文前，必须先在内部执行 outline_execution_units / chapter_outline_unit_ledger / outline_beat_execution_ledger / foreshadow_control_ledger / character_state_ledger / pacing_budget_ledger / evidence_permission_ledger / mechanism_boundary_ledger / inference_uncertainty_ledger / time_window_budget / spatial_feasibility_ledger / channel_occlusion_ledger / coincidence_friction_ledger / dialogue_density_ledger / anchor_audit_before_prose / micro_continuity_budget。不要输出合同、表格、自检、分析、JSON 或说明；最终只输出小说正文。

核心原则：v4.12 是跨章节、跨项目通用约束。章节大纲仍是本章全部剧情骨架，正文阶段只做扩写、润色、动作化、场景化和因果支撑；不得新增大纲外关键剧情、钥匙/暗门/逃生捷径、强线索、强结论或章末硬 payoff。v4.8 的锚点审计仍保留，但必须升级为“微连续性预算扣账”：每个执行单元写正文前，先为路径、资源、信息、表达和结果强度分配预算；预算缺失时只能降级，不得先写强结果再补理由。历史评分 issue 只作诊断；本约束用于生成前内化，不是后置 hard gate / needs_repair / auto_revise，也不依赖 scene_planner。

零、hierarchical_outline_contract（层级来源合同，保留执行）
写正文第一句前，内部建立来源链：book_to_volume_contract → volume_to_chapter_contract → chapter_outline_source_of_truth → prose_expand_unit_only。
任何关键剧情事实、人物行动、道具线索、机制解释、关系变化、章末钩子，都必须能回溯到章节大纲或上层大纲授权。找不到授权时，不得作为剧情功能出现。

一、outline_execution_units（章节大纲执行单元，内部执行，不输出）
把章节大纲逐条拆成 execution_unit，每个 unit 至少包含：
`outline_source / unit_goal / start_state / required_action / cause_effect_bridge / information_source / resource_pressure / character_response / allowed_revelation / result_state_delta / result_ceiling / unresolved_tail / prose_budget / outline_beat_execution_ledger / foreshadow_control_ledger / character_state_ledger / pacing_budget_ledger / evidence_permission_ledger / mechanism_boundary_ledger / inference_uncertainty_ledger / time_window_budget / spatial_feasibility_ledger / channel_occlusion_ledger / coincidence_friction_ledger / dialogue_density_ledger / anchor_audit_before_prose / micro_continuity_budget`。

每个 unit 的含义：
- outline_source：来自章节大纲的哪一句/哪一项，不得凭空新增。
- unit_goal：本单元只完成一个剧情功能，不能顺手完成多个奖励。
- start_state：承接上一单元的地点、人物、伤势、物件、认知和压力。
- required_action：角色必须用可见动作推进，不用旁白替代。
- cause_effect_bridge：从上一状态到本动作的因果桥，补足路径、耗时、权限、载体或代价。
- information_source：角色为什么知道、看见、听见、判断；无来源只能写疑似。
- resource_pressure：高低资源差、守卫、制度、时间、身体代价如何限制行动。
- character_response：至少给出关键人物对压力/信息/损失的反应，避免降智或工具人。
- allowed_revelation：本单元允许透露的最强信息，不能越过大纲。
- result_state_delta：本单元结束后状态具体改变了什么；没有状态变化就只作氛围，不承担剧情功能。
- result_ceiling：结果强度上限，默认从低到高为 seed_only / weak_hint / verifiable_question / partial_evidence / stage_confirmation / global_truth。
- unresolved_tail：本单元必须留下什么不确定、代价或后续问题。
- prose_budget：高压段简短，程序段不教程化，动作量不得超过时间窗口。
- outline_beat_execution_ledger：每个大纲节点必须写清“目标→原因→可见行动→直接后果→未解尾巴”，正文只能扩写这条链；链外内容只能做氛围，不承担剧情功能。
- foreshadow_control_ledger：每条伏笔/线索在本单元只能标记为 preserve / seed_only / weak_hint / partial_reveal / deferred_payoff / payoff；没有大纲授权时禁止 payoff，章末默认只能到疑点或待验。
- character_state_ledger：写动作前先扣“已知信息、误解、动机、身体位置、可用资源、可承受代价”；缺任一项时角色只能迟疑、试探、误判或降级行动。
- pacing_budget_ledger：每个 unit 预先限定段落功能和展开量；低重要度节点只允许承接/转场/弱线索，不得扩成新冲突或新机制说明。
- evidence_permission_ledger：任何册页、证物、卷宗、钥匙、门路、口供、制度信息，必须先扣“谁有权接触、为何此刻能看见/拿到/听到、凭什么打开或引用”；无权限只能写远观、误听、残页、待验，不得越权得到完整信息。
- mechanism_boundary_ledger：机关、锁舌、钥匙、门闩、暗格、制度流程必须先写触发条件、物理结构、成本、失败可能和反制边界；不清楚时只能表现为卡顿、异响、错位或疑似，不得直接顺利通行。
- inference_uncertainty_ledger：所有线索判断必须同时保留至少一个替代解释；从痕迹到结论至少经过观察→推测→交叉验证，未验证不得定语化为事实。
- time_window_budget：倒计时、追捕、落闸、三十息等高压窗口必须限制观察项数量；每个窗口最多完成一个核心动作和一个弱判断，其他信息延后。
- spatial_feasibility_ledger：任何“半寸、半尺、半丈、几步、几尺、几丈、一尺、一丈、贴身、越过、钻入、伸手、递物、踢开、退至、站在……下”等空间动作，必须先扣身体尺度、支点、可达距离、遮挡/碰撞、视线和受力；不满足时只能写停住、擦过、够不到、被挡、改换路径，不得强行过人或穿模。
- channel_occlusion_ledger：隔墙、雨声、渠声、人群、门缝、暗处听见的信息必须扣声源、遮挡、噪音、距离和可辨词数；只能得到片段、语气或关键词，不得完整听清制度/计划/口供。
- coincidence_friction_ledger：钥匙、木片、册页、证物、脚边物、恰好相合的线索必须有中介原因、距离/时间成本、误差或替代用途；缺少摩擦时只能写“像是、可疑、待验”，不得直接送到脚边或正合结论。
- dialogue_density_ledger：喊令、重复命令、转述长句必须有密度预算；连续喊令不得压过动作与环境，通过概括侧写处理重复台词，短促命令只留给真正转折点。
- communication_damping：密集交锋不必每句都接住；允许无视、岔开、迟钝、重复尾音、信息掉在地上或被环境打断，不要让所有人像带提词器一样无缝对答。
- plain_register_no_wit：日常护财、试探、讨价还价和街头冲突必须服从人物当下情绪，禁止廉价机智、抖机灵、对仗式反击和硬凹“聪明”；粗鄙直接比俏皮更真实。
- focal_measure_only：数字化距离只在生死、翻脸、机密暴露或必须对齐证物的焦点时刻使用；普通走位、站立、互动和压迫不要写成坐标测绘。
- motive_exposition_zero：禁止把角色或对方的底层动机直接说破；不要写“你就是想赖账/你其实……”这类拆解句，改用反问、压价、动作和结果施压。
- anchor_audit_before_prose：写本单元正文前必须完成来源/路径/观察/动机/代价/上限审计；审计缺口决定降级.
- micro_continuity_budget：写本单元正文前必须完成下列五类预算扣账，预算不足不得升级结果。

二、micro_continuity_budget（微连续性预算扣账，内部执行，不输出）
每个 execution_unit 写成正文前，先检查并满足以下字段：
`unit_movement_budget / unit_resource_budget / unit_information_ladder / unit_expression_role / unit_result_delta_cap / outline_beat_execution_ledger / foreshadow_control_ledger / character_state_ledger / pacing_budget_ledger / evidence_permission_ledger / mechanism_boundary_ledger / inference_uncertainty_ledger / time_window_budget / spatial_feasibility_ledger / channel_occlusion_ledger / coincidence_friction_ledger / dialogue_density_ledger`。

字段规则：
- unit_movement_budget：人物、物件、消息、线索跨地点时，必须有起点、路径、耗时、载体、遮挡/风险、到达条件；缺任一项，不得直接到场或直接被发现，只能写痕迹、延迟、误认或待验。
- unit_resource_budget：权力、工具、人手、体力、时间、制度权限和信息优势必须扣账；低资源方推进必须付出暴露、受伤、失物、关系、机会、时间或制度风险；无扣账不得得到强推进或强奖励。
- unit_information_ladder：信息只能按“观察/听闻/触碰 → 推测 → 交叉验证 → 阶段结论”逐级升级；未验证只能写疑点、半证、待核、替代解释，不得直接写确定事实。
- unit_expression_role：每个重复动作、意象、制度说明、压迫描写或对白，必须声明本单元的新功能；无新功能就换动作、删减、合并或改成环境承载，避免重复堆叠。
- unit_result_delta_cap：每个单元只允许一个小幅状态变化；强结论、强奖励、机制突破、身份确认、章末硬 payoff 必须有多单元支撑，否则降级为 weak_hint / verifiable_question / partial_evidence。
- outline_beat_execution_ledger：若正文想写的内容无法对应章节大纲 beat 的目标/原因/行动/后果，必须删除或改为无剧情功能的氛围。
- foreshadow_control_ledger：伏笔只能按当前标记推进；preserve/deferred 不得解释，seed_only/weak_hint 不得定案，partial_reveal 不得给全貌，payoff 必须有章节大纲授权。
- character_state_ledger：角色的每次突破、毁证、夺物、发现暗路、说服他人，都必须有可见动机、位置窗口、资源代价和风险反馈。
- pacing_budget_ledger：低权重单元不得承载超过一个信息点；追捕/倒计时/落闸等高压单元必须缩短观察和对白。
- evidence_permission_ledger：开册、翻页、拿证、看卷、听供、触钥、过门之前必须有接触权限/位置窗口/遮挡条件/代价；没有就降级为“瞥见一角、听见半句、摸到旧痕、只能待验”。
- mechanism_boundary_ledger：锁舌、夜钥、暗门、机关不得作为万能捷径；必须有结构线索、触发动作、失败噪声、阻滞或误触风险，且结果只能按大纲授权升级。
- inference_uncertainty_ledger：不得用“便是、定是、由此可知”等定案语气越级；强判断前必须留下误差、替代解释或待核对象。
- time_window_budget：高压时间窗内禁止完成多项查证；若时间少于一炷香/三十息，只能写一个可见动作、一个局部观察、一个未定推测。
- spatial_feasibility_ledger：人物和物体互动必须符合身体尺度、方位介词、接触点、支撑点和遮挡；“半寸过人、隔物递取、无支点发力、雨网/屋檐方位错误”一律降级为受阻或改道。
- channel_occlusion_ledger：水渠、门缝、墙后、人群和雨声中的信息只能碎片化；不得让角色通过弱通道听清完整指令、制度规则或关键口供。
- coincidence_friction_ledger：关键物不能自动出现在脚边、手边或正好对上；必须经过寻找、误判、代价、第三方动作或延迟确认。
- dialogue_density_ledger：喊令/口令/重复台词要稀释；用动作后果、旁人反应和概括性侧写承载重复信息，避免密集命令堆砌。

三、anchor_audit_before_prose（锚点审计，保留执行）
每个 execution_unit 写成正文前，仍须满足：
`unit_source_anchor / unit_transfer_path / unit_observation_window / unit_adversary_incentive / unit_cost_or_interference / unit_downgrade_rule / payoff_ceiling_before_reveal`。
- unit_source_anchor：人物、物件、消息、证据、制度权限从哪里来；必须能回溯到上文/大纲/当前可见动作。
- unit_transfer_path：人物/物件/消息从 A 到 B 的路径、耗时、载体、权限、接应或风险；没有路径就不能直接到场。
- unit_observation_window：主角为何能看见/听见/判断；写清距离、遮挡、光线、噪声、视角、时间窗口或触碰条件。
- unit_adversary_incentive：高资源方为何会说漏、失手、暴露、让步；必须有误判、压力、诱因、制度约束、第三方制衡或自保动机。
- unit_cost_or_interference：低资源方推进必须付出身体、时间、信息、关系、暴露、制度风险或机会成本；高压场景必须被打断。
- unit_downgrade_rule：任一锚点或预算缺失时，本单元结果自动降级为疑点、半句、残片、误认、弱线索、待验项或替代解释。
- payoff_ceiling_before_reveal：写任何证物/暗语/钥匙/身份/机制/章末钩子前，先确定本单元最高只能到 seed_only / weak_hint / verifiable_question / partial_evidence / stage_confirmation / global_truth；不得越级。

四、no_budget_no_upgrade（无预算不得升级）
以下情况一律触发降级，不得直接写强证据、强结论或强奖励：
- 没有 unit_movement_budget 的同路、接应、物件到场、线索被发现。
- 没有 unit_resource_budget 的低资源突破、高资源失守、证物获得、权限调用。
- 没有 unit_information_ladder 的直接识破、直接确认、直接推出完整制度/动机/幕后关系。
- 没有 unit_expression_role 的重复动作/重复意象/重复压迫句式。
- 没有 unit_result_delta_cap 的多重收获、章末连环 payoff、机制突破或强结论。
- 没有 evidence_permission_ledger 的开册、取证、引用卷宗、拿到钥匙、穿过门路或读取少页。
- 没有 mechanism_boundary_ledger 的夜钥触发、锁舌开合、机关启动、暗门通行或制度例外。
- 没有 inference_uncertainty_ledger 的定案语气、唯一解释、路线确认或章末判断。
- 没有 time_window_budget 的倒计时内多重观察、查证、追问、转场和机制解释。
- 没有 spatial_feasibility_ledger 的半寸过人、贴身穿越、隔物递取、无支点发力、方位介词错误。
- 没有 channel_occlusion_ledger 的隔墙/渠声/雨声/人群中完整听清。
- 没有 coincidence_friction_ledger 的钥匙、木片、证物、册页恰好送到脚边或正好相合。
- 没有 dialogue_density_ledger 的密集喊令、重复口令、长段复述。
降级写法：像、疑似、半截、残片、断词、旧痕、待验、误听、替代解释、暂不能证实；必须把“确认”推迟到后续有锚点与预算的单元。

五、chapter_outline_unit_ledger（单元账本硬约束，内部执行，不输出）
正文每一段必须挂靠一个 execution_unit。没有 unit 的段落只能做氛围或过渡，不得新增剧情事实。
每个 execution_unit 必须按顺序写出以下链条：
1. unit_cause_effect_chain：先承接状态和路径成本，再写推进动作。
2. anchor_audit_before_prose：先满足来源/路径/观察/动机/代价/上限。
3. micro_continuity_budget：先扣路径/资源/信息/表达/结果预算，再写结果。
4. unit_character_response：写关键人物反应、犹豫、误判、阻拦或代价，避免突然配合/突然降智。
5. unit_foreshadow_resolution：线索只能按 foreshadow_control_ledger 和大纲允许的强度种植、保留、弱回收或延迟；不得一章内投放并完整解释。
6. unit_character_state_ledger：关键人物行动必须先有已知信息、动机、位置、可用资源和代价；不能为推进剧情突然降智或突然配合。
7. pacing_budget_ledger：每段必须知道自己是承接、动作、观察、推断、转场还是钩子；无新功能就合并或删除。
8. unit_result_state_delta：最后只交付本单元授权的一个状态变化，并留下 unresolved_tail。

六、outline_coverage_before_generation（写前覆盖检查，内部执行，不输出）
写正文前必须确认：
- 每条章节大纲都有至少一个 execution_unit，且每个 execution_unit 都有 outline_beat_execution_ledger。
- 每个 execution_unit 只服务一个大纲节点；多个节点相邻时也要用动作/时间/压力分隔。
- 所有关键转场都有 unit_movement_budget。
- 所有资源突破都有 unit_resource_budget。
- 所有角色认知都有 information_source + unit_observation_window + unit_information_ladder。
- 所有证物/线索都有 source_seed / observation_condition / low_strength_inference / alternative_explanation / foreshadow_control_ledger / evidence_permission_ledger / inference_uncertainty_ledger / channel_occlusion_ledger / coincidence_friction_ledger。
- 所有章末收获都有 result_delta_cap，且不得超过 payoff_ceiling_before_reveal；默认不得从疑点直接升级为定案。
- 所有关键角色动作都有 character_state_ledger；所有低重要度段落都有 pacing_budget_ledger；所有机关/钥匙/门路都有 mechanism_boundary_ledger；所有倒计时段都有 time_window_budget；所有贴身/狭窄/方位动作都有 spatial_feasibility_ledger；所有喊令/复述都有 dialogue_density_ledger。

七、针对 v4.8 复发问题的生成前压制
- space_rule_violation：人物/物件/消息从 A 到 B 必须写起点、路径、耗时、载体、权限或风险；否则降级为痕迹。
- information_rule_violation：角色不能知道文本没给来源的信息；弱信息只能推出疑点，不能推出强结论；信息要走 unit_information_ladder。
- power_resource_violation：低资源方突破高资源方必须有扣账；高资源方不能为送证据突然降智；无 unit_resource_budget 不给奖励。
- mechanism_rule_violation：机制/制度/能力必须有触发条件、成本、边界、失败可能和反制；锁舌/钥匙/门路必须可视化为物理结构，不得临场万能化。
- time_rule_violation：高压窗口内动作量必须受限；程序检查要被锣声、脚步、武器、伤痛、催促打断；三十息内不得完成多项查证和机制说明。
- result_strength_violation：每单元只允许一个小状态变化；章末最多一个主收获，其他只能保留疑似、残片、误差、替代解释或后续问题；不得用定案语气写“走门/定路/真相”。
- expression_contract_violation：制度/推理说明拆进动作、物件、短对白；重复意象和关键词必须有新功能，否则删减、分散或改写成视线/动作承载。

八、runtime_prompt_snapshot（运行时观测要求）

八、chinese_prose_mechanics（中文行文机械约束，内部执行，不输出）
写正文时必须把“准确、自然、可读”置于“短促、古风、干练”之前：
- 句子呼吸：连续短句不得超过四句；环境用长句顺人物视线/听觉/行动轨迹铺陈，核心动作和风险反应用短句提速；避免为了紧张感把普通动作压缩成碎片。
- 动词克制：基础动作还原为看、走、停、拿、放、退、靠近、转身、走到、站在、试探等朴素动词；禁止堆砌生僻动词或为了文学性强行换词。
- 搭配守正：禁止生造动宾短语；必须写符合中文习惯的“走到檐下”“试探鼻息”“站在门边”，不得写“进檐”“探鼻”等压缩词。
- 物理准确：屋檐、雨网、门槛、墙角、桌案、阴影等只能与人物形成合理方位和动作关系；方位介词必须准确。
- 视角流动：环境细节跟随人物看见、听见、走近、停下、转头而出现；禁止清单式罗列陈设。
- 信息复述：长段台词/传闻/供词/制度说明再次出现时，用概括性侧写承接，只保留关键词和新反应，禁止全文复述。
- 动作整体性：一个连贯动作不要切成机械步骤；只有当每一步带来风险、观察、阻碍或代价时才拆开。
- floating_dialogue_exchange：讨价还价、逼问、互相试探时允许连续纯台词交替；不要写成“动作+台词、动作+台词”的节拍器。纯台词必须靠内容咬合，动作只服务局势改变。
- dialogue_symmetry_break：禁止连续四组以上短问短答，禁止原样复述或镜像回应。用答非所问、抢白、动作打断、直接抛结论破坏对称性。不要把“说好一行 / 就一行 / 多看一个字呢 / 你自己合上 / 认错呢 / 认对呢”连成排比回合。
- prop_fiddling_guard：禁止拨算盘、绕细绳、擦砚台、摸杯子、挪纸张等无意义道具交互。除非它在物理阻挡、掩饰心虚、转移证物或情绪爆发中起作用，否则删除。
- explicit_pause_marker_zero：禁用“安静了一会儿、沉默了、没有立刻回话、一小会儿、半晌、顿了顿、停了一下”。停顿用环境音突入、物件响动、脚步靠近或直接切下一句承载。
- subtext_occlusion：禁止角色直接说破对方真实意图，例如“你怕我……”“你想让……”“我想让你……”。用反问、避答、压价、局部事实和动作压力保留潜台词。
- spatial_mapping_zero：禁用三步外、一指宽、影子外、几寸等静态测绘词；人物位置只写逼近、退开、让开、挡住、压住等动态趋势或遮挡关系。
- biographical_infodump_zero：往事作证最多两句，禁止从几岁讲到几岁，禁止按时间轴背履历。
- story_bible_leakage_zero：隐藏世界不得通过广告、海报、新闻、路人闲聊或旁白一次性列出设定词。不要在正文里集中出现“血脉体系、执行者体系、核心权能、校准、未知X、奥丁源头”等故事说明书式词串；POV 不知道的名词只能留作异常痕迹、误听片段或后续解释。
- setting_name_dialogue_zero：路人、新闻、店员、邻居、广告、海报和闲聊不能字正腔圆讨论核心世界观名词。超自然影响必须降维成封路、停电、绕路、物价、黑车、查得紧、上面、那帮人、那种事、清道等生活抱怨和代词。
- directional_listing_zero：禁止“左边/右边/东头/西头/前后”导览式罗列；环境只抓一个与当前氛围或剧情冲突的核心反差点，砍掉无关陈设扫描。
- mundane_scene_plausibility：修正不合生活逻辑的日常场景。烤肠摊只卖摊位合理物，不顺手出现临期面包、热水、汤锅；便利店不硬塞消毒水味和温水；大雨院子里不要安排人群围电视。
- plain_modern_register：改回正常现代汉语。写锁屏/按灭屏幕、别挡着锅、别往楼里跑/别进去，不写把手机按黑、别挡锅、别碰那边、带了急、收租截图这类压缩拗口词。
- plain_contemporary_chinese：普通现代场景必须用完整、自然、当代的中文表达，不写半文言、伪文学、为了显得干练而硬压缩的句子。写“他叫了一声：‘师傅。’”“车没了，手机快没电了”，不要写“喊了声师傅”“来意说得很低”“终于抬头”“声音被关门声挡住一半”。
- age_plausibility：公共场景里的拒绝、催促和登记要与年龄身份一致。十八岁去旅馆不要写成“未成年不行”；更自然的阻力是满房、押金、证件、前台态度、关门时间。
- abstract_reasoning_zero：删除“试错”“本质上”“底层逻辑”“慢慢试错”这类元语言解释，把原因落到余额、体力、时间、电量、路况和退路上。
- limited_pov_only：第三人称有限视角只保留主角能看见、听见、感到的事实；删除“才想起他、没人记得、没人问他、他像水印一样会没”这类替别人下心理结论或点破比喻的旁白。
- semantic_density_budget：开篇苦难/边缘化信息限量，只留两三个与当前困境物理相关的痛点，删除老师、门卫、班群、食堂、房东同质标签堆叠。
- resource_continuity：校验钱、手机、电量、支付方式、交通选择是否互相支撑；叫车失败、现金不足、移动支付、旁人坐车都必须有一致资源逻辑。
- action_causality：校验动作和结果的物理/语义因果，不用一句漂亮话遮住不成立的动作链。
- motivation_bridge：进入危险空间前必须补足求生压力、退路关闭、误判理由或外部催逼；不能把看见门/灯/门缝干燥当成自动行动因果。
- dialogue_topology_limit：连续含引号段落不得超过四段；连续纯短对白不得超过两段；每个场景紧贴问答不得超过三组。超出时必须合并为自然段，或改成答非所问、未答、抢白、环境声打断、动作结果和证物变化。
- 结构拓扑自检：动作对白绑定率不得超过 0.35；多数段落若都写成“人物微动作 + 台词”，必须合并、删除或改成纯台词/证物推进。
- 紧贴问答限制：紧贴问答不得超过 12 组；连续追问必须被无视、抢白、答非所问、说半截、证物或环境打断。
- 短对白密度限制：短对白密度不得过半；短句用于压迫，不用于把整章写成剧本回合。
- 短段落密度限制：短段落密度不得超过 0.35；同一动作、同一证据链、同一轮施压应合并成自然段。
- 程序性解释簇限制：旧库、码头、灯籍、封存、回封、待验等制度/证据说明不能由专家 NPC 一口气讲完，必须拆进物证、争执、误解和局部记录。

dialogue_machine_few_shot（内部参照，不输出）
原段落（廉价机智与翻译腔抛梗）：
“踩烂了，算你买。”
“先问问它算谁卖。”
“你别装了。”
“我装什么？”

修正目标（平庸表达、护财、情绪服从）：
“脚底留神，踩坏了照价赔。”
“往后退，地上都是货。”
“没瞎就看着点脚下。”

原段落（节拍器式机器痕迹）：
邱成把笔记本放回柜台，手还压在上面。
“说好一行。”
“就一行。”
“只露一行。”
“看一行。”
“看一行。”
“多露一个字呢？”
“你自己合上。”
“认错呢？”
“赔礼，再补湿损纸钱。”
“认对呢？”
“明早我带见证到市书会认旧物。”

修正目标（破坏对称、直接压价、动作只服务局势）：
邱成手背青筋微突，死死按住本子：“只露一行。多半个字，我立马撕了。”
“可以。”陈青没废话，“认错我赔钱。认对，这本东西今晚谁也别想碰，明早市书会见。”

原段落（物理测绘和履历式自白）：
陈青往前挪了一步，脚尖仍停在柜台影子外。
“别过线。”
“我没过。”
“念。”
“青儿药照前方，德安堂欠二钱。”
“街上药单都这么写。”
“德安堂柜上不这么写。”
“你又懂德安堂？”
“六岁到十六岁，我替陈玉枝跑德安堂。老周记账，先写月份，再写病人，再写药钱。怕抓错药，家里人才把‘青儿’写在前头。”

修正目标（动态压迫和证据信息提纯）：
陈青逼近半步。
邱成眼神瞬间警惕：“退回去。”
陈青没退，目光盯死那行字：“‘青儿药照前方，德安堂欠二钱’。”
“街上药单都这么写，算什么铁证？”
“德安堂老周记账，规矩是年月打头，病人在后。”陈青声音冷硬，“只有我祖母去赊药，怕老眼昏花抓错，才会把我的名字强行顶在最前头。”

{render_prose_quality_prompt()}

九、runtime_prompt_snapshot（运行时可观测，不阻断）
本提示必须可在运行时被验证：contract_version、direct_generation_first_v4.13、chinese_prose_mechanics、outline_execution_units、chapter_outline_unit_ledger、outline_beat_execution_ledger、foreshadow_control_ledger、character_state_ledger、pacing_budget_ledger、evidence_permission_ledger、mechanism_boundary_ledger、inference_uncertainty_ledger、time_window_budget、spatial_feasibility_ledger、channel_occlusion_ledger、coincidence_friction_ledger、dialogue_density_ledger、communication_damping、plain_register_no_wit、focal_measure_only、motive_exposition_zero、setting_name_dialogue_zero、directional_listing_zero、mundane_scene_plausibility、plain_modern_register、plain_contemporary_chinese、age_plausibility、abstract_reasoning_zero、limited_pov_only、semantic_density_budget、resource_continuity、action_causality、motivation_bridge、anchor_audit_before_prose、micro_continuity_budget、unit_movement_budget、unit_resource_budget、unit_information_ladder、unit_expression_role、unit_result_delta_cap、no_budget_no_upgrade、runtime_prompt_snapshot 和 prompt_hash 应进入日志或任务元数据。该观测只用于诊断 prompt 是否注入，不阻断生成，不触发后置修订。
""".strip()
