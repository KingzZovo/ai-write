"""Genre-agnostic narrative logic contract.

This module deliberately avoids book-specific or genre-specific rules.  It
defines the meta-contract every long-form generation pass must apply first:
infer the current story world's time / space / power-resource / information /
mechanism / result-strength rules from the project context, then generate and
evaluate prose against those inferred rules.
"""

from __future__ import annotations

NARRATIVE_CONTRACT_VERSION = "world_logic_contract_v1"


WORLD_LOGIC_CONTRACT = """\
【世界逻辑合同（题材无关，最高优先级）】
不要套用某本书、某一章或某个固定题材的限制。你必须先从当前项目的题材、设定、大纲、人物、已生成章节和目标风格中，推导本书自己的 World Logic Contract，再写作。

所有关键剧情推进都必须同时接受六类底层规则约束：
1. 时间规则：移动、传讯、准备、恢复、冷却、社会流程都可能消耗时间。不得跳过会影响可信度的时间成本。
2. 空间规则：人物、物件、消息、风险从 A 到 B 必须有路径、权限、载体或代价。不得让关键实体瞬移。
3. 权力/资源规则：冲突双方的制度权力、武力/能力、财富/物资、信息优势、关系、声望、技术权限等必须被识别。低资源方不能无成本全面压倒高资源方；若获胜，只能依靠规则漏洞、信息差、第三方制衡、局部胜利或代价交换。
4. 信息规则：角色只能根据已获得、可理解、可信任的信息行动。弱信息只能推出疑点或假设，不能直接推出强结论。
5. 能力/机制规则：任何能力、技术、法术、制度工具、资源调用都必须有触发条件、成本、边界、冷却/副作用/风险和可反制方式。禁止临时万能补丁。
6. 结果强度规则：剧情结果必须与前置支撑匹配。支撑不足时必须降级为疑点、局部胜利、暂缓、误导、代价胜、后续线索或延迟回收，不能强行完成强结果。

写作前先在心中完成“题材适配”：判断当前题材中什么相当于时间成本、空间门槛、权力资源、信息证据、能力机制和结果强度。正文不得显式输出这份分析，但每个关键场景必须受它约束。
"""


SCENE_CONTRACT_FIELDS_PROMPT = """\
【场景合同字段】
每个 scene JSON 除 title/brief/pov/location/time_cue/key_action/target_words/hook 外，必须尽量补齐以下题材无关字段：
- start_state：本场开头承接上一场的状态。
- time_delta：距离上一场过去多久；如果是首场，说明开场时间锚点。
- location_path：关键人物/风险/信息从上一地点到本场地点的路径；无移动也要说明“同地承接”。
- entity_transfers：关键人物、物件、尸体、文书、载具、数据、法器、资源、消息等如何到场/转移/留置。
- power_resource_map：本场冲突各方可调用的权力与资源，以及低资源方推进时要付出的成本。
- information_state：本场开始时各方已知/未知/误解的信息。
- mechanism_limits：本场会改变局势的能力、技术、法术、制度工具或资源的触发条件、边界和代价。
- result_strength：本场允许达成的结果强度；若支撑不足，必须写明降级结果。
- transition_bridge：本场结尾如何把时间、空间、人物、物件、信息状态交给下一场。
"""


WRITER_CONTRACT_PROMPT = """\
【正文写作合同】
- 只能把 scene 合同允许的 result_strength 写成正文，不得为了爽感擅自升级剧情成果。
- 如果信息支撑不足，只能写疑点、暂缓、误导、暗查、局部胜利或代价胜，不能写定论、全面胜利、公开压倒或万能解决。
- 必须显化会影响读者可信度的时间/空间/实体转移；不得用“转眼”“不多时”“已经”等粗暴跳过关键成本。
- 冲突必须体现当前世界的权力/资源差与违抗成本；高资源方不能无城府崩盘，低资源方不能无成本碾压。
- 能力、技术、法术、系统、制度、金钱、人脉、证据、舆论等不得临时万能化。
- 同类动作、意象、压迫感、情绪和对白句式要变换表达，避免连续复用同一核心动词或影视剧式口号互怼。
"""


EVALUATOR_CONTRACT_PROMPT = """\
【合同验收硬规则】
评估前先推导当前项目的 World Logic Contract，不要按固定题材模板审稿。以下为题材无关违规类型：
- time_rule_violation：时间成本/恢复/准备/流程被跳过。
- space_rule_violation：人物、物件、消息、风险、资源无路径转移或瞬移。
- power_resource_violation：低资源方无成本全面压倒高资源方，或高资源方无合理制衡而降智崩盘。
- information_rule_violation：角色掌握未获得信息，或弱信息推出强结论。
- mechanism_rule_violation：能力、技术、法术、制度工具、资源调用无触发条件/成本/边界/反制。
- result_strength_violation：前置支撑不足却达成强结果，没有降级为疑点、局部胜利、暂缓、误导、代价胜或后续线索。
- expression_contract_violation：表达层重复、时代/场合/身份不适配、口号式或现代影视剧式互怼破坏沉浸。

若出现上述高严重度违规，对应维度不得高于 7.5；若直接破坏因果链、角色认知或世界机制，对应维度不得高于 6.5。每条 issue 的 description 必须以违规类型标签开头，例如“[information_rule_violation] ...”，suggestion 必须给出“如何降级结果或补足支撑”。
"""


REVISE_CONTRACT_PROMPT = """\
【通用重写合同】
不要按某个具体症状打补丁。先识别每条问题属于哪类世界逻辑违规，再按类型修复：
- time_rule_violation：补时间差、准备/恢复/传递耗时、流程成本。
- space_rule_violation：补移动路径、携带/转移/留置方式、进入权限或空间代价。
- power_resource_violation：补双方资源差、制衡理由、违抗成本、局部胜利和后续反制。
- information_rule_violation：把强结论降级为疑点/假设/佐证，补来源、可接触性、可信度和替代解释。
- mechanism_rule_violation：补触发条件、成本、边界、副作用、冷却和反制方式。
- result_strength_violation：把全面胜利/定论/公开翻盘降级为暂缓、暗查、误导、半胜或后续线索。
- expression_contract_violation：替换重复动作词、调整对白身份/场合/时代/题材语感，避免口号式互怼。
重写必须保留主线目标，但允许降低单场结果强度，优先维护故事世界自洽。
"""


def contract_block(*, include_scene_fields: bool = False, include_writer: bool = False) -> str:
    """Compose a compact contract prompt block for generation paths."""
    parts = [WORLD_LOGIC_CONTRACT]
    if include_scene_fields:
        parts.append(SCENE_CONTRACT_FIELDS_PROMPT)
    if include_writer:
        parts.append(WRITER_CONTRACT_PROMPT)
    return "\n\n".join(parts)
