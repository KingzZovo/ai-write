"""
Hierarchical Outline Generator

Generates outlines at three levels:
1. Book-level: Overall plot arc, core characters, world-building, estimated scale
2. Volume-level: Per-volume conflicts, turning points, new/departing characters
3. Chapter-level: Per-chapter plot points, characters, emotional arc, transitions
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import AsyncIterator

from app.services.model_router import get_model_router
from app.services.narrative_contract import WORLD_LOGIC_CONTRACT

logger = logging.getLogger(__name__)


OUTLINE_LLM_CALL_TIMEOUT_SECONDS = 240
OUTLINE_FAST_CALL_TIMEOUT_SECONDS = 120
OUTLINE_TITLE_CHECK_TIMEOUT_SECONDS = 120
OUTLINE_VOLUME_MAX_TOKENS = 8192
OUTLINE_VOLUME_STAGED_TASK_TYPE = "outline_book"
BOOK_STAGE_MIN_CHARS = {
    "skeleton": 1200,
    "characters": 1200,
    "world": 1200,
}

OUTLINE_WRITING_QUALITY_PROMPT = """

【写作质量准则】
生成前先校准硬事实，再输出内容：人物身份、关系、动机、伤势、筹码、时间线不能自相矛盾。
不要用大量环境描写、华丽词藻、比喻修辞凑字数；扩写必须靠具体行动、选择代价、人物关系变化、任务进度和伏笔流转。
保持故事感和正在发生的动作感，语言直接自然，避免文艺腔、模板腔、机械总结。
每段只围绕本段职责展开；发现问题时只修正问题点，不推翻已合格内容。
"""


OUTLINE_BOOK_CONTRACT_PROMPT = f"""

{WORLD_LOGIC_CONTRACT}

【大纲层合同要求】
本合同必须上移到全书大纲，不允许只在正文阶段补救。全书大纲必须明确写出一组题材无关、但由当前项目动态推导出来的 world_logic_contract，至少包含：
- time_rules：本书时间成本、传讯/移动/准备/恢复/流程消耗规则。
- space_rules：人物、物件、消息、风险、资源的空间路径和进入门槛。
- power_resource_rules：主要势力/角色的权力、资源、能力、声望、关系、技术或制度权限差。
- information_rules：信息获得、验证、误判、证据强度和角色认知边界。
- mechanism_rules：能力、技术、法术、制度工具、金钱/人脉/证据等的触发条件、成本、边界、反制。
- result_strength_rules：什么支撑能达成强结果；支撑不足时必须降级为疑点、局部胜利、暂缓、误导、代价胜或后续线索。


- semantic_anchor_rules：若线索要躲过删除/抹除/审查机制，必须说明它为何是低语义、非直白文字、非数据库字段，以及如何被转译成图形、噪声、物理痕迹、数值或操作记录。
- faction_bargain_rules：高风险收容、担保、庇护或越权行动必须写清执行方获得的政治、技术、资源或制度筹码，不能只写善意保护。
- physical_constraint_rules：核心地点若长期停滞、无法拆除、无法封锁或天然成为盲区，必须给出可验证的物理/工程/空间原因和强行处理的后果。
- cross_volume_catalyst_rules：分卷之间从发现线索到高危行动，必须有外部压力、倒计时、证据衰变、追踪或资源窗口作为行动催化剂。
"""

OUTLINE_VOLUME_CONTRACT_PROMPT = """

【分卷层世界逻辑合同】
本卷大纲必须继承全书 world_logic_contract，并额外输出以下 JSON 字段，字段内容必须从当前题材/设定/全书大纲动态推导，禁止写固定题材补丁：
- volume_start_state：本卷开头的人物、地点、资源、信息、冲突状态。
- volume_end_state：本卷结束时允许达到的状态；必须与转折支撑相匹配。
- volume_power_resource_map：本卷主要冲突各方可调用的权力/资源/能力/关系/信息优势与代价。
- volume_information_map：本卷关键信息的持有者、传播路径、误判、验证方式。
- volume_mechanism_limits：本卷会影响局势的能力/技术/制度/资源工具的触发条件、成本、边界、反制。
- volume_result_strength_ladder：本卷结果强度阶梯，说明弱支撑只能导向疑点/局部胜利/暂缓，强支撑才能导向公开翻盘/定论/全面胜利。
- foreshadow_progression：本卷伏笔从埋→推→收的状态推进，不得跳级。
"""


OUTLINE_CHAPTER_CONTRACT_PROMPT = """

【章节大纲合同字段】
每个章节摘要/章节详细大纲都必须变成“可执行剧情状态机”，不是单纯剧情摘要。除原有字段外，必须输出：
- start_state：本章开头承接上一章的时间、空间、人物、物件、信息和冲突状态。
- end_state：本章结束时实际交付给下一章的状态。
- time_delta：距离上一章/上一关键场景过去多久，哪些流程/移动/准备消耗了时间。
- location_path：关键人物、物件、消息、风险从上一地点到本章地点的路径；无移动也要写“同地承接”。
- entity_transfers：关键人物、物件、尸体、文书、数据、法器、资源、消息等如何到场/转移/留置。
- information_state：本章开始和结束时各方已知/未知/误解的信息；弱信息只能推出疑点或假设。
- power_resource_map：本章冲突各方的权力/资源差、违抗成本、制衡条件。
- mechanism_limits：本章使用的能力、技术、法术、制度工具、证据、资源调用的条件、成本、边界、反制。
- result_strength：本章允许达成的结果强度；支撑不足时必须降级为疑点、局部胜利、暂缓、误导、代价胜或后续线索。
- handoff_to_next：本章如何把时间、空间、人物、物件、信息状态交接给下一章。
- transition_bridge：本章结尾到下一章开头之间必须显化的过桥过程。必须写清时间流逝、空间移动、人物/物件/消息交接、信息可见性和未解决风险；不得只写“进入下一章/继续追查”这类抽象钩子。
"""

BOOK_OUTLINE_SYSTEM = """你是小说策划师。直接输出大纲内容，不要说任何多余的话。

禁止输出以下内容：
- 任何开场白、自我介绍、"好的"、"下面给出"、"如果你愿意"、"希望对你有帮助"之类的废话
- 任何Markdown格式（不要用#、**、-、>、```、---）
- 任何对用户说的话，你不是在对话，你是在输出一份文档

大纲必须包含以下九个部分（用空行和段落标题分隔）：

一、书名与核心概念
简短有力的书名，一句话概括故事内核。

二、主要角色与小传（至少5人）
每个角色单独一段，信息必须齐全：
- 基本信息：姓名、年龄、性别、外貌（一两句抓特征）、身份标签
- 出身与成长：家世、童年环境、关键童年事件、求学/师承
- 性格与动机：内核性格、表面伪装、所渴望之物、所恐惧之物、最不能碰的底线
- 关键创伤：塑造他的一次具体事件（必须有时间、地点、人物、冲击）
- 言语风格：1-2句代表性对白，口吻独特
- 核心关系：与另外两三个主要角色的羁绊

三、主角能力成长表
用一张清晰的表列出主角的能力变化，每卷一行，字段包括：
- 卷数
- 修为/境界/关键能力
- 关键道具/武器/功法
- 触发升级的事件
- 副作用或代价
表格形式：卷数 | 境界能力 | 道具 | 触发事件 | 代价

四、角色关系网
明确写出主要角色之间的关系：谁和谁是敌人、盟友、师徒、恋人、竞争者。用"A → B：关系描述"的格式。

五、势力格局
列出3-5个主要势力/阵营，每个势力的核心利益、代表人物、与主角的关系。

六、世界观设定集
系统罗列，每个子项单独一段，内容必须具体不可空泛：
- 地理：大陆/国家/城邦/特殊地貌，给出主要名称和位置关系
- 历史：前朝/大战/神话断代，标记至少两个对当下有持续影响的历史事件
- 种族与势力：人族/妖族/异族/教派，各自特征与彼此的宿怨
- 宗教与神话：信仰体系、主神/异神、禁忌
- 政治与经济：权力结构、货币、贸易路线、阶级流动性
- 文化与日常：服饰、饮食、节庆、婚丧、礼仪
- 力量体系：境界或魔法分级、获取方式、能做到的事、代价与反噬、禁忌
- 特殊物品：重要法宝/神器/秘术，来源和传说

七、分卷规划
根据创意体量、节奉需求自由决定卷数（一般 2-8 卷，不必拘泥于 3 卷）。每卷写出核心冲突、关键转折、主角状态变化。
在本段末尾，额外输出一个结构化卷规划块（供程序解析），严格使用以下格式（不要包含 markdown 代码块标记）：
<volume-plan>
[
  {"idx": 1, "title": "卷名", "theme": "本卷主题", "core_conflict": "本卷核心冲突", "est_chapters": 8},
  {"idx": 2, "title": "卷名", "theme": "本卷主题", "core_conflict": "本卷核心冲突", "est_chapters": 12}
]
</volume-plan>

八、核心伏笔
3-5条贯穿全书的伏笔线，标明埋设时机和预期消解条件。

九、基调与类型标签
用 3-5 个短标签表达整体叙事基调（如"冷峻悬疑""贵族权谋""灾异意象"），再用一句话说明要避开哪些常见套路。

文字风格：句子长短不一，段落开头各不相同，像一个老编辑在给新人讲故事方案，口语化但专业。"""

# v1.4.2 — staged book-outline prompts. The full BOOK_OUTLINE_SYSTEM asks a
# model to emit all nine sections in one response, which routinely exceeds 10k
# Chinese chars (~15-18k tokens) and hits the long-output quality cliff. We
# split the 9 sections across 3 calls, each staying under ~4k tokens so every
# response sits in the model's safe output zone.
BOOK_OUTLINE_SKELETON_SYSTEM = """你是小说策划师。只输出骨架五段（一、三、七、八、九），不要废话，不要 Markdown。

这是分阶段生成的第一阶段，后续阶段会补上二、四、五、六段，所以这里必须保留“一、”“三、”“七、”“八、”“九、”这些原编号。

一、书名与核心概念
简短有力的书名，一句话概括故事内核。

三、主角能力成长表
用表格列出主角每卷的能力变化，字段：卷数 | 修为境界/关键能力 | 道具武器功法 | 触发升级的事件 | 代价或反噬。至少 5 行。

七、分卷规划
根据创意体量、节奉需求自由决定卷数（一般 2-8 卷，不必拘泙于 3 卷）。每卷写清核心冲突、关键转折、主角状态变化。
在本段末尾，额外输出一个结构化卷规划块（供程序解析），严格使用以下格式（不要包含 markdown 代码块标记）：
<volume-plan>
[
  {"idx": 1, "title": "卷名", "theme": "本卷主题", "core_conflict": "本卷核心冲突", "est_chapters": 8},
  {"idx": 2, "title": "卷名", "theme": "本卷主题", "core_conflict": "本卷核心冲突", "est_chapters": 12}
]
</volume-plan>

八、核心伏笔
3–5 条贯穿全书的伏笔线，每条写明埋设时机、预期消解条件。

九、基调与类型标签
3–5 个短标签表达整体敘事基调（如“冷峻悬疑”“贵族权谋”“灾异意象”），再用一句话说明要避开哪些常见套路。

文字风格：句子长短不一，像老编辑在给新人讲故事方案，口语化但专业。"""

BOOK_OUTLINE_CHARACTERS_SYSTEM = """你是小说策划师。只输出角色与关系三段（二、四、五），不要废话，不要 Markdown。

必须基于用户创意和已生成的骨架（见 user 消息）保持一致。段落标题必须保留“二、”“四、”“五、”原编号，方便后续拼接。

二、主要角色与小传（至少 5 人）
每个角色单独一段，信息必须齐全：
- 基本信息：姓名、年龄、性别、外貌（一两句抓特征）、身份标签
- 出身与成长：家世、童年环境、关键童年事件、求学/师承
- 性格与动机：内核性格、表面伪装、所渴望之物、所恐惧之物、最不能碰的底线
- 关键创伤：塑造他的一次具体事件（必须有时间、地点、人物、冲击）
- 言语风格：1-2 句代表性对白，口吻独特
- 核心关系：与另外两三个主要角色的羁绊

四、角色关系网
明确写出主要角色之间的关系：谁和谁是敌人、盟友、师徒、恋人、竞争者。用“A → B：关系描述”的格式。

五、势力格局
3-5 个主要势力/阵营，每个势力的核心利益、代表人物、与主角的关系。

文字风格：像老编辑讲角色方案，具体不空泛。"""

BOOK_OUTLINE_WORLD_SYSTEM = """你是小说策划师。只输出世界观设定集（六），不要废话，不要 Markdown。

必须基于用户创意和已生成的骨架保持设定一致。段落标题保持“六、”原编号。

六、世界观设定集
系统罗列以下 8 个子项，每个子项单独成段，内容必须具体，不可空泛：
- 地理：大陆/国家/城邦/特殊地貌，给出主要名称和位置关系
- 历史：前朝/大战/神话断代，标记至少两个对当下有持续影响的历史事件
- 种族与势力：人族/妖族/异族/教派，各自特征与彼此的宿怨
- 宗教与神话：信仰体系、主神/异神、禁忌
- 政治与经济：权力结构、货币、贸易路线、阶级流动性
- 文化与日常：服饰、饮食、节庆、婚丧、礼仪
- 力量体系：境界或魔法分级、获取方式、能做到的事、代价与反噬、禁忌
- 特殊物品：重要法宝/神器/秘术，来源和传说

文字风格：像老编辑写设定集，具体可落笔，避免“大致”“大概”之类虚词。"""

VOLUME_OUTLINE_SYSTEM = """你是一位经验丰富的小说策划师。根据全书大纲和指定的卷号，生成该卷的详细大纲。

要求输出 JSON 格式：
{
  "volume_idx": 卷号,
  "title": "卷名",
  "core_conflict": "本卷核心冲突",
  "turning_points": ["转折点1", "转折点2"],
  "new_characters": [
    {"name": "角色名", "identity": "身份", "role": "作用"}
  ],
  "departing_characters": ["退场角色名"],
  "foreshadows": {
    "planted": [{"description": "新埋伏笔", "resolve_conditions": ["条件"]}],
    "resolved": ["本卷消解的伏笔描述"]
  },
  "emotional_arc": "本卷情感基调变化",
  "chapter_count": 本卷预计章数,
  "chapter_summaries": [
    {
      "chapter_idx": 1,
      "title": "章名",
      "summary": "本章过程概要（160-240字）",
      "key_events": ["事件"]
    }
  ],
  "transition_to_next": "与下一卷的衔接"
}

输出纯 JSON，不要包含 markdown 代码块标记"""

# v1.4.2 Task C — split volume outline into meta + batched chapters.
VOLUME_META_SYSTEM = """你是一位经验丰富的小说策划师。根据全书大纲和指定的卷号，生成该卷的元信息。

要求输出 JSON 格式：
{
  "volume_idx": 卷号,
  "title": "卷名",
  "core_conflict": "本卷核心冲突",
  "turning_points": ["转折点1", "转折点2"],
  "new_characters": [
    {"name": "角色名", "identity": "身份", "role": "作用"}
  ],
  "departing_characters": ["退场角色名"],
  "foreshadows": {
    "planted": [{"description": "新埋伏笔", "resolve_conditions": ["条件"]}],
    "resolved": ["本卷消解的伏笔描述"]
  },
  "emotional_arc": "本卷情感基调变化",
  "chapter_count": 整数,
  "transition_to_next": "与下一卷的衔接"
}

chapter_count 必须是一个整数，不要输出 chapter_summaries 字段。

【深度硬约束】
- core_conflict 不少于 180 个中文字符，必须写清本卷开局状态、对抗双方、资源差、误判来源、阶段性胜负条件。
- turning_points 至少 5 个；每个转折点不少于 80 个中文字符，必须包含“触发事件 + 参与人物 + 付出代价 + 导向的新局面”。
- emotional_arc 不少于 120 个中文字符，必须描述至少 3 次情绪/立场变化，不得只写一种基调。
- transition_to_next 不少于 120 个中文字符，必须写清本卷结尾留下的未解决动作、信息差、资源变化和下一卷入口。
- volume_start_state、volume_end_state、volume_power_resource_map、volume_information_map、volume_mechanism_limits、volume_result_strength_ladder、foreshadow_progression 都必须作为 JSON 字段输出；每个字段不少于 100 个中文字符。
- 不允许用“略”“待补充”“继续推进”“逐步展开”“一系列事件”等空泛词代替具体剧情设计。

【命名与质量要求（PR-OL11 + PR-AI1）】
- title 为本卷卷名，4-8 字诗意短词（如《潮起》《骨灯》《城下》）。平铺直白的主题词不可代替卷名。
- core_conflict、turning_points、foreshadows 都要仅限本卷，到本卷末必须交代清楚。
- new_characters、departing_characters 不要重复全书主角名单，仅列本卷首次出场或退场的人。
- 所有自创器物/术语使用现代汉语真实词汇或含义可推测的复合词（如「血玉牌」「潮汐罗盘」），禁止生造单字或拼凑不可读词（如「怎表」「屃门」）。

输出纯 JSON，不要包含 markdown 代码块标记。"""

VOLUME_CHAPTERS_SYSTEM = """你是一位经验丰富的小说策划师。根据卷元信息和已生成的上文章节摘要，批量生成指定区间的章节摘要。

要求输出 JSON 格式：
{
  "batch": [
    {
      "chapter_idx": 整数,
      "title": "章名（见下方【章名规则】）",
      "summary": "本章过程概要（160-240 字）",
      "main_progress": "本章主线推进点（60-100 字）",
      "side_progress": "本章支线/暗线推进点（40-80 字，本章无可填 无）",
      "foreshadow_state": "本章埋下/活动/消解的伏笔，名称+状态“埋/推/收”，40-80 字，本章无可填 无",
      "key_scene": "本章关键场景（60-100 字，交代“在哪里 + 发生什么 + 主要人物 + 场景结果”）",
      "characters_present": ["本章出场主要人物"],
      "key_events": ["本章关键事件一", "..."]
    }
  ]
}

【章名规则·硬约束】
1) 必须符合普通中文语感与主谓逻辑：
   - 物体（钟、灯、债、山门、剑、印）不能作为施动者去做需要意识的抽象动作。
     反例：「债认账」「山门还债」「钟声判罪」「剑悔过」。
     正例：「认债」「山门点灯」「剑出鞘」「钟声起」「掌灯人来」。
   - 动宾要可还原成人话：「A 做 B」要能解释成「谁/什么 在做/经历 什么」。
2) 第二人称「你」允许使用，但必须符合中文语言习惯：
   - 允许：作为人物对话引语 / 作为本章视角对象的指称，如「你别回头」「你欠的债」「你说的那夜」「认你」。
   - 不允许：让标题本身像在向读者喊话或解释剧情，如「你把他写得太干净」「你应该知道的那件事」「你一直忽略的真相」。
3) 不允许：「第N章」/纯数字/重复本卷卷名/纯抽象空词（恐惚/争锄/虚妄/混沌 此类）/中文全角冒号「：」/现代化或工程类词汇（流程/工序/系统/数据/版本/SOP）/生造单字或拼凑不可读词。
4) 优先选择本章主事件的关键具象意象、关键道具、关键人物动作，或章末状态的诗化短句。
5) 字数以短为主、可长可短，不需全卷统一；只要避免把一句概述性句子当成标题（顶多不要超过 14 个汉字）。不要为凑字数而生造，也不要为凑短而丢主谓。

【其它质量硬约束】
- summary 严格 160-240 字，必须同时交代 人物+场景+事件过程+转折+本章末状态。
- main_progress、side_progress、foreshadow_state、key_scene 必须是可执行剧情设计，不得只写短标签。
- chapter_idx 从用户指定的 start 开始，连续递增到 end，不跳号不超出区间。
- main_progress / side_progress / foreshadow_state / key_scene 都不可留空；本章未使用可填「无」。
- 所有自创器物/术语使用现代汉语真实词汇或含义可推测的复合词。
- 同一伏笔 foreshadow_state 在本卷内状态只能是 埋→推→收 递进，不可后退。

输出纯 JSON，不要包含 markdown 代码块标记。"""

# v1.4.2 Task C hardening — the full VOLUME_CHAPTERS_SYSTEM plus the
# chapter contract is useful for one-off detailed chapter outlines, but it is
# too heavy for the 10-at-a-time volume skeleton path. That path first needs a
# stable, complete set of chapter anchors; detailed state-machine fields are
# generated later by generate_chapter_outline.
VOLUME_CHAPTERS_SKELETON_SYSTEM = """你是一位经验丰富的中文长篇小说策划师。你的任务是批量生成分卷章节骨架，必须稳定、完整、可保存。

只输出一个 JSON 对象，不要 markdown，不要解释，不要代码块：
{
  "batch": [
    {
      "chapter_idx": 整数,
      "title": "章名",
      "summary": "本章过程概要，80-140 个中文字符",
      "key_events": ["关键事件一", "关键事件二"]
    }
  ]
}

硬性要求：
- batch 数组长度必须等于用户要求的章节数。
- chapter_idx 必须从 start 连续递增到 end，不跳号、不重复、不超出范围。
- 每个 summary 用 80-140 个中文字符写清：人物、地点、动作、转折、章末状态。
- 每章至少 2 个 key_events，每个事件必须是具体剧情动作，不要写“推进剧情”“揭开秘密”等空泛词。
- title 使用 2-8 个汉字，优先选择本章关键人物动作、具象意象或章末状态；不要使用“第N章”、纯数字、冒号、工程化词汇或不可读生造词。
- 只做骨架，不要输出 start_state、end_state、time_delta 等详细章节合同字段。
"""

# PR-OL17 + PR-OL18: SSE chapter outline path now produces a process-narrative
# 300-500 字 summary instead of the legacy keyword schema, parity with
# chapter_outline_expander.SYSTEM_PROMPT so newly created projects’ cascade
# generation matches the AI-扩写 button output.
CHAPTER_OUTLINE_SYSTEM = """你是一位高质量中文小说大纲作者。你要生成一份本章「过程性详细大纲」，只输出 JSON 本身，不要 markdown 代码块或辅助说明。

【过程性章节大纲是什么】
summary 必须 300–500 字，是「去了修辞、去了环境描写、去了心理渲染，但过程不丢」的陈述句：人物进场顺序、关键对话要点、动作与结果、状态转折、到头状态都要讲清楚。

反例（错误，不要这样写）：A 和 B 结婚。
正例（正确，过程性陈述）：九点 A 在礼堂等待。誓词中手微抖，但仍平静说出「愿意」。仪式尾声一名黑帽人递上一张空白请柬，B 未看见。A 当众接过后不动声色，餐会后独自看请柬背面的印记，决定夜里独自出门。

【为什么要这么写】
summary 在后续章节中会被反复检索。如果你只写「A 和 B 结婚」，后续章节只能记住「他们结婚」，不记得仪式上发生了什么。过程性 summary 能让后续章节准确复述本章事件链，不会丢掉「黑帽人递请柬」这种后面才兑现的伏笔。

【输出 schema（严格遵守）】
{
  "chapter_idx": 章号,
  "title": "本章标题 3–8 字，不同于卷名",
  "summary": "本章过程性叙事 300–500 字。含进入状态·中段关键场景与对话要点·转折·到头状态。去修辞但过程完整。\\n 表示换行。",
  "key_events": ["事件 1", "事件 2", "… 3–6 条，作为 summary 的结构索引（不是 summary 本身）"],
  "prev_chapter_threads": ["本章需接住的上章未完动作 / 冲突 / 悬念"],
  "state_changes": {
    "characters": [{"name": "角色名", "change": "本章末与本章始相比的具体状态转换"}],
    "items": [{"name": "道具名", "change": "出现 / 转手 / 丢失 / 含义变化"}],
    "relationships": [{"from": "A", "to": "B", "change": "关系状态变化"}]
  },
  "foreshadows_planted": [
    {"description": "本章埋下的伏笔描述", "resolve_conditions": "未来某章兌现条件"}
  ],
  "foreshadows_resolved": ["本章兌现之前某伏笔的描述"],
  "next_chapter_hook": "本章末尾留给下章的明确动作·冲突·问题。不可为空。",
  "word_count_target": 4000
}

【额外要求】
- summary 必须 300–500 字。低于 300 字会被拒收。
- summary 中不要出现「如上所述」「总之」「本章讲了 X」这类总结词，直接叙述过程。
- key_events 是 summary 的结构骨架，不能代替 summary。只写 key_events 会被拒收。
- foreshadows / state_changes 可为 [] 但不能缺字段。首章时 prev_chapter_threads 可为 []。
- 不要创作未在全书大纲 / 本卷大纲 / 上章中出现的人物 / 设定 / 道具。
- 输出纯 JSON，不要包含 markdown 代码块标记。
"""


# ----------------------------------------------------------------------
# PR-OL10 — word-count -> chapters -> volumes auto-sizing
# ----------------------------------------------------------------------
#
# A web novel chapter is typically ~4000 Chinese chars; a volume is
# typically 100-200 chapters (i.e. 400k-800k chars). We translate the
# user-supplied target_word_count into hard numerical constraints and
# inject them into the outline prompts so the LLM stops picking "3 卷"
# regardless of book scale.

DEFAULT_CHAPTER_WORDS = 4000
DEFAULT_CHAPTERS_PER_VOLUME_MIN = 100
DEFAULT_CHAPTERS_PER_VOLUME_MAX = 200
DEFAULT_CHAPTERS_PER_VOLUME_TARGET = 150


def compute_scale(
    target_word_count: int | None,
    *,
    chapter_words: int = DEFAULT_CHAPTER_WORDS,
    chapters_per_volume_min: int = DEFAULT_CHAPTERS_PER_VOLUME_MIN,
    chapters_per_volume_max: int = DEFAULT_CHAPTERS_PER_VOLUME_MAX,
    chapters_per_volume_target: int = DEFAULT_CHAPTERS_PER_VOLUME_TARGET,
) -> dict | None:
    """Translate target word count into a chapter/volume plan.

    Returns ``None`` when ``target_word_count`` is missing or non-positive,
    so callers fall back to the legacy free-form prompt ("一般 2-8 卷").

    The returned dict is suitable for ``_apply_scale_to_prompt``::

        {
          "target_word_count": 2000000,
          "n_chapters": 500,
          "n_volumes": 3,                 # ∈ [ceil(N/200), floor(N/100)]
          "chapters_per_volume": 167,
          "chapter_words": 4000,
        }
    """
    if not target_word_count or int(target_word_count) <= 0:
        return None
    twc = int(target_word_count)
    cw = max(1, int(chapter_words))
    n_ch = max(1, math.ceil(twc / cw))

    cmin = max(1, int(chapters_per_volume_min))
    cmax = max(cmin, int(chapters_per_volume_max))
    ctgt = max(cmin, min(cmax, int(chapters_per_volume_target)))

    n_vol_target = max(1, round(n_ch / ctgt))
    n_vol_lo = max(1, math.ceil(n_ch / cmax))
    n_vol_hi = max(1, math.floor(n_ch / cmin)) if n_ch >= cmin else 1

    if n_vol_hi < n_vol_lo:
        # Total chapter count < cmin -- tiny project, single volume.
        n_vol = max(1, n_vol_target)
    else:
        n_vol = max(n_vol_lo, min(n_vol_hi, n_vol_target))

    cpv = max(1, round(n_ch / n_vol))
    return {
        "target_word_count": twc,
        "n_chapters": n_ch,
        "n_volumes": n_vol,
        "chapters_per_volume": cpv,
        "chapter_words": cw,
    }


def resolve_chapters_per_volume_pref(settings_json: dict | None) -> tuple[int, int] | None:
    """Resolve an explicit chapters-per-volume (lo, hi) range from settings_json.

    Reads the keys the project-settings API stores (and api/generate.py's
    compute_scale path already consumes): ``chapters_per_volume_min`` /
    ``chapters_per_volume_max`` / ``chapters_per_volume_target``. Values are
    clamped to [1, 50]; non-numeric / non-positive values are ignored. Returns
    ``None`` when no key carries a usable value — callers must then keep the
    legacy behaviour (plan-driven counts, 10-chapter fallback).
    """
    if not isinstance(settings_json, dict):
        return None

    def _clamped(key: str) -> int | None:
        try:
            value = int(settings_json.get(key))
        except (TypeError, ValueError):
            return None
        if value < 1:
            return None
        return min(value, 50)

    lo = _clamped("chapters_per_volume_min")
    hi = _clamped("chapters_per_volume_max")
    tgt = _clamped("chapters_per_volume_target")
    candidates = [v for v in (lo, hi, tgt) if v is not None]
    if not candidates:
        return None
    lo = lo if lo is not None else min(candidates)
    hi = hi if hi is not None else max(candidates)
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _format_scale_directive(scale: dict) -> str:
    """Render a hard-constraint paragraph from a compute_scale() result."""
    twc = scale["target_word_count"]
    n_ch = scale["n_chapters"]
    n_vol = scale["n_volumes"]
    cpv = scale["chapters_per_volume"]
    cw = scale["chapter_words"]
    return (
        f"本书总字数目标 {twc:,} 字，由后端根据《{cw} 字/章 / 100–200 章/卷》"
        f"推算得到：必须输出 {n_vol} 卷规划，每卷 {cpv} 章左右，每章 {cw} 字左右。"
        f"总章数目标 {n_ch} 章。不允许返回其他卷数。"
    )


# ----------------------------------------------------------------------
# PR-OL11 — expose per-chapter breakdown from a saved volume outline
# ----------------------------------------------------------------------
#
# The staged volume path persists ``chapter_summaries`` into
# ``Outline.content_json`` for level="volume" rows. PR-OL12 needs to feed
# the entry for chapter N (and the previous chapter's summary) into the
# chapter-outline generator so adjacent chapter outlines stay coherent.
# This helper accepts either the raw content_json or just the parsed
# ``chapter_summaries`` list and indexes it by chapter_idx.

def extract_chapter_breakdown(volume_outline: dict | list | None) -> dict[int, dict]:
    """Return ``{chapter_idx: chapter_summary_dict}`` from a volume outline.

    Empty dict on missing/malformed input. Accepts:
      - The whole ``Outline.content_json`` dict (looks for nested
        ``chapter_summaries`` or ``volume_outline.chapter_summaries``).
      - The parsed list of chapter summaries directly.
      - A staged-mode dict carrying ``chapter_summaries`` at top level.

    Each value is left as-is (dict with at minimum a ``chapter_idx`` and
    ``summary`` field after PR-OL11 prompt strengthening).
    """
    if volume_outline is None:
        return {}
    summaries: list | None = None
    if isinstance(volume_outline, list):
        summaries = volume_outline
    elif isinstance(volume_outline, dict):
        for key in ("chapter_summaries", "chapters", "batch"):
            v = volume_outline.get(key)
            if isinstance(v, list):
                summaries = v
                break
        if summaries is None:
            inner = volume_outline.get("volume_outline") or volume_outline.get("meta")
            if isinstance(inner, dict):
                v = inner.get("chapter_summaries")
                if isinstance(v, list):
                    summaries = v
    if not summaries:
        return {}
    out: dict[int, dict] = {}
    for i, item in enumerate(summaries, start=1):
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("chapter_idx") or i)
        except (TypeError, ValueError):
            idx = i
        out[idx] = item
    return out


# PR-CH-OUTLINE-ENRICH (2026-06-29): generating a level=chapter outline used to
# write only the ``outlines`` table; ``chapters.outline_json`` (what the prose
# generator + readiness gate actually read) kept the thin volume-batch skeleton.
# This splices the freshly generated per-chapter content INTO that skeleton so
# prose sees the rich outline, while keeping the skeleton's authoritative
# chapter_idx and never regressing a populated field back to empty/garbage.
def merge_chapter_outline_enrichment(skeleton: dict | None, generated: dict | None) -> dict:
    """Merge a freshly generated chapter outline into the existing skeleton.

    Rules:
      - Start from the skeleton (the row already stored on ``chapters``).
      - ``chapter_idx`` is always taken from the skeleton (LLM must not renumber).
      - A generated field overwrites the skeleton ONLY when it carries real
        content (non-empty str / non-empty list-or-dict / non-None scalar).
      - ``_parse_error`` payloads and the bare ``raw_text`` blob are ignored so
        a parse failure never poisons a usable skeleton.
    """
    merged: dict = dict(skeleton) if isinstance(skeleton, dict) else {}
    if not isinstance(generated, dict) or generated.get("_parse_error"):
        return merged
    _skeleton_idx = merged.get("chapter_idx")
    for key, value in generated.items():
        if key in ("chapter_idx", "_parse_error", "raw_text"):
            continue
        if isinstance(value, str):
            if value.strip():
                merged[key] = value
        elif isinstance(value, (list, dict)):
            if value:
                merged[key] = value
        elif value is not None:
            merged[key] = value
    if _skeleton_idx is not None:
        merged["chapter_idx"] = _skeleton_idx
    return merged


class OutlineGenerator:
    """Generates hierarchical outlines: book → volume → chapter."""

    def __init__(self, project_id: str | None = None, chapter_naming_directive: str = ""):
        self.router = get_model_router()
        # PR-CHAPTER-NAMING: optional directive describing the desired chapter
        # naming style; appended to volume/chapter outline system prompts when
        # non-empty. Loaded from style_profile.config_json.chapter_naming_style
        # by api/generate.py.
        self.chapter_naming_directive = chapter_naming_directive or ""
        # PR-USAGE-LOGMETA: when caller (api/generate.py) supplies project_id,
        # every router.generate_stream/generate call below threads _log_meta
        # so llm_call_logger.log_llm_call writes one row per LLM call AND
        # llm_call_logger's PR-USAGE-SYNC finally-block bumps usage_quotas.
        # Without this, outline streams skipped the logger entirely.
        self.project_id = str(project_id) if project_id else None

    def _log_meta(self, task_type: str) -> dict | None:
        """PR-USAGE-LOGMETA helper: emit dict for model_router._log_meta.

        Returns None when no project_id is bound, preserving the legacy
        (no-logging) call path for ad-hoc invocations.
        """
        if not self.project_id:
            return None
        return {"project_id": self.project_id, "task_type": task_type}

    async def _load_chapters_per_volume_pref(self) -> tuple[int, int] | None:
        """Read the project's chapters-per-volume preference from settings_json.

        E2E defect (2026-07-26): a project configured for 3-4 chapters/volume
        still got 10-chapter staged volume outlines because this path never
        consulted settings. Reads the same keys api/generate.py's compute_scale
        already uses (``chapters_per_volume_min`` / ``chapters_per_volume_max``
        / ``chapters_per_volume_target``). Returns a clamped (lo, hi) range, or
        None when the project carries no explicit setting so legacy behaviour
        (model/plan-driven counts) is preserved. Never raises.
        """
        if not self.project_id:
            return None
        try:
            from app.db.session import async_session_factory
            from app.models.project import Project

            async with async_session_factory() as db:
                proj = await db.get(Project, self.project_id)
                proj_settings = proj.settings_json if proj is not None else None
            if not isinstance(proj_settings, dict):
                return None
            return resolve_chapters_per_volume_pref(proj_settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chapters-per-volume setting load failed: %s", exc)
            return None


    @staticmethod
    def _apply_scale_to_prompt(prompt: str, scale: dict | None) -> str:
        """PR-OL10: replace the legacy "2-8 卷 free-choice" sentence with a
        hard numeric directive when the project carries a target_word_count.

        No-op when ``scale`` is None (legacy behaviour preserved).
        """
        if not scale or not isinstance(scale, dict):
            return prompt
        directive = _format_scale_directive(scale)
        # Match either flavor of the legacy sentence (2-8 卷 / 拘泥于 3 卷)
        # plus the typo variant (拘泙). Prefix the hard directive in front
        # of "每卷写出/写清" so downstream JSON block instructions stay intact.
        legacy_pattern = re.compile(
            r"根据创意体量、节奏需求自由决定卷数（一般\s*2-8\s*卷，不必拘(?:泥|泙)于\s*3\s*卷）。"
        )
        if legacy_pattern.search(prompt):
            return legacy_pattern.sub(directive, prompt, count=1)
        # Fallback: prepend directive to 「七、分卷规划」 header line.
        if "七、分卷规划" in prompt:
            return prompt.replace(
                "七、分卷规划",
                "七、分卷规划\n" + directive,
                1,
            )
        return prompt

    async def generate_book_outline(
        self,
        user_input: str,
        stream: bool = False,
        staged: bool = True,
        scale: dict | None = None,
    ) -> dict | AsyncIterator[str]:
        """Generate a book-level outline from user's creative input.

        v1.4.2 default (``staged=True``): splits the 9-section outline into
        three sequential LLM calls so no single response has to cover 10k+
        Chinese chars. Stages B and C run in parallel since they both depend
        only on stage A's skeleton. Avoids the long-output quality cliff.

        ``stream=True`` keeps the legacy single-call behavior; staged mode
        is not yet exposed over SSE.
        """
        from app.services.prompt_loader import load_prompt
        system = await load_prompt("outline_book", fallback=BOOK_OUTLINE_SYSTEM)
        if not self._looks_like_full_book_outline_prompt(system):
            logger.warning(
                "outline_book PromptRegistry prompt is legacy/short; using built-in full outline contract"
            )
            system = BOOK_OUTLINE_SYSTEM
        # PR-OL10: replace free-form volume-count guidance with hard directive.
        system = self._apply_scale_to_prompt(system, scale)
        system = system + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"请根据以下创意生成全书大纲：\n\n{user_input}"},
        ]

        if stream and staged:
            # v1.4.2 Task B: structured staged SSE stream.
            # Emits stage_start / stage_chunk / stage_end / done dicts
            # that api/generate.py serializes over SSE.
            return self._generate_book_outline_staged_stream(user_input, scale=scale)

        if stream:
            return self.router.generate_stream(
                task_type="outline_book",
                messages=messages,
                _log_meta=self._log_meta("outline_book"),
            )

        if staged:
            return await self._generate_book_outline_staged(user_input, scale=scale)

        result = await self.router.generate(
            task_type="outline_book",
            messages=messages,
            _log_meta=self._log_meta("outline_book"),
        )
        return self._parse_json(result.text)

    @staticmethod
    def _looks_like_full_book_outline_prompt(prompt: str) -> bool:
        """Detect whether a registry prompt carries the full book-outline contract."""
        if not prompt:
            return False
        required = ("九", "主要角色与小传", "世界观设定集", "<volume-plan>")
        return all(token in prompt for token in required)

    # ------------------------------------------------------------------
    # v1.4.2 — staged book outline implementation
    # ------------------------------------------------------------------
    _SECTION_NUMS = ("一", "二", "三", "四", "五", "六", "七", "八", "九")

    @staticmethod
    def _fallback_book_skeleton(user_input: str, scale: dict | None = None) -> str:
        """Deterministic stage-A skeleton used when the model returns an empty success.

        Canon-neutral: it echoes the caller's ``user_input`` (which carries the
        authoritative premise / fact-lock) and lays down a generic structural
        scaffold. It must NOT hardcode any book-specific protagonist name,
        world noun, or plot device — doing so would contradict the project's
        own facts (and corrupt every other project that hits this path when the
        endpoint is slow).
        """
        expected = int(scale.get("n_volumes") or 5) if scale else 5
        chapters_per_volume = int(scale.get("chapters_per_volume") or 150) if scale else 150
        total_chapters = int(scale.get("n_chapters") or expected * chapters_per_volume) if scale else expected * chapters_per_volume
        volume_plan: list[dict] = []
        remaining = total_chapters
        for idx in range(1, expected + 1):
            left = expected - idx
            est_chapters = remaining if left == 0 else min(chapters_per_volume, remaining - left)
            remaining -= est_chapters
            volume_plan.append(
                {
                    "idx": idx,
                    "title": f"第{idx}卷",
                    "theme": "身份追索、证据推进与代价升级",
                    "core_conflict": "主角用行动和代价推进一个可验证目标，并留下进入下一卷的债务或伏笔。",
                    "est_chapters": est_chapters,
                }
            )
        return (
            "一、作品定位与核心卖点\n"
            f"项目基础（须严格遵守，不得改写其中的人物名、地名与设定）：\n{user_input}\n"
            "主角不是靠奇遇一路碾压，而是在每次取证、救人、交易和战斗中付出清晰代价，"
            "逐步逼近上面项目基础设定的核心真相。核心卖点是力量的代价、证据链、"
            "人物关系的利益变化，以及每卷任务推进带来的真相揭示。\n\n"
            "三、主线剧情总览\n"
            "开局主角因一桩与自身相关的异常被卷入冲突，为保住身边人与线索，必须拿到第一份关键证据。"
            "中段主角从局部冲突进入更高层级，逐步发现敌对方不是单点作恶，而是一整套用合法名义维持的结构。"
            "后段他需要在公开真相、保护盟友和保留自身力量之间选择，最终把私人动机推进为更大的对抗。"
            "每一卷都要完成一个可验证任务，并留下下一卷必须偿还的代价或伏笔。\n\n"
            "七、分卷规划\n"
            "<volume-plan>\n"
            f"{json.dumps(volume_plan, ensure_ascii=False, indent=2)}\n"
            "</volume-plan>\n"
            "各卷依次建立危机、暴露盟友风险、扩展冲突范围、把多方关系推到公开冲突、完成终局对抗；"
            "具体人物、地点与机制一律以上面的项目基础设定为准。\n\n"
            "八、长期伏笔与回收计划\n"
            "伏笔围绕项目基础中给出的核心谜团（如失踪、身份、被改写的记录、导师手法与终局对手的合法性）展开。"
            "早期每个线索必须对应后续行动，并在指定卷数回收。\n\n"
            "九、主题与情感曲线\n"
            "主题不是抽象成长，而是主角从只想保住家人，走到愿意承担公开真相的代价。情感曲线依次经历互相利用、证据共享、代价分担、立场冲突和共同承担。"
        )

    @staticmethod
    def _fallback_small_stage_text(stage_name: str, user_input: str) -> str:
        """Canon-neutral fallback for the B2/B4/B5/C book-outline sub-stages.

        These fire only when the model endpoint is slow/empty. They must NOT
        hardcode a specific protagonist, ally, faction or world noun — the
        earlier version baked in a stale 神裔 conception (沈砚 / 血印 / 王朝档案司
        / 祭台) that contradicted the project's own canonical-facts lock and
        produced an outline naming two different protagonists. Instead we emit
        a structural scaffold that explicitly defers every concrete name and
        device to the project's ``user_input``.
        """
        anchor = (
            "（注意：以下为结构骨架，所有具体人物名、地名、组织名与力量机制，"
            "必须严格采用本项目设定，不得另造名字。本项目设定如下）\n"
            f"{user_input}\n"
        )
        scaffolds = {
            "B2/characters": (
                "二、主要角色与小传\n" + anchor +
                "请按上述设定给出：主角（动机、初始处境、力量及其代价、核心局限）；"
                "关键救助者/引路人（身份、目标、与主角的筹码交换、首次登场情境）；"
                "导师型角色（提供的能力或方法、附带的代价测试）；"
                "阶段性敌人与终局对手（各自掌握的资源/权力、与主角冲突的根源）。"
            ),
            "B4/relationships": (
                "四、主要矛盾与关系网络\n" + anchor +
                "请按上述设定写清：主角与救助者/引路人之间的信任如何在共同承担代价中建立与裂变；"
                "主角与导师的带条件关系（每次获得能力要放弃什么）；"
                "主角与阶段敌人、终局对手之间的追逐/受害-维护关系，"
                "以及这些关系如何承载核心真相的回收。"
            ),
            "B5/factions": (
                "五、势力格局\n" + anchor +
                "请按上述设定列出：主导秩序的官方/机构方（掌握的记录与权力、公开目标与隐藏筹码）；"
                "筛选或评估主角的组织（评估标准、代表人物）；受压迫的弱势群体（保存的线索）；"
                "终局制度敌人；以及灰色中介网络（既可助力也会抬高代价）。"
            ),
            "C/world": (
                "六、世界观设定集\n" + anchor +
                "请按上述设定写清：地理与信息/追捕的流动方式（时间成本如何影响行动结果）；"
                "历史与力量体系的来源、能力边界与代价；政治秩序由哪几方共同维护，"
                "任何单方结论只能形成疑点、必须交叉验证才能推动强结果。世界观必须服务人物行动与伏笔回收。"
            ),
        }
        return scaffolds.get(stage_name, "")

    async def _generate_book_outline_staged(self, user_input: str, *, scale: dict | None = None) -> dict:
        """Three-call staged book outline (A skeleton → B/C in parallel).

        Each stage stays below ~4k tokens so output quality stays in the
        safe zone for every model tier. The three stage outputs are
        reassembled into a single 9-section document in canonical order.
        """
        # Stage A — skeleton (一、三、七、八、九)
        # PR-OL10: inject hard volume-count directive into skeleton prompt.
        skeleton_system = self._apply_scale_to_prompt(BOOK_OUTLINE_SKELETON_SYSTEM, scale)
        skeleton_system = skeleton_system + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT
        skeleton_msgs = [
            {"role": "system", "content": skeleton_system},
            {"role": "user", "content": f"创意：\n{user_input}\n\n请生成骨架五段。"},
        ]
        skeleton_text = ""
        for attempt in range(1, 3):
            try:
                skeleton_result = await asyncio.wait_for(
                    self.router.generate(
                        task_type="outline_book",
                        messages=skeleton_msgs,
                        stream=False,
                        request_timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                    ),
                    timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                )
                skeleton_text = (getattr(skeleton_result, "text", "") or "").strip()
                if skeleton_text:
                    break
                logger.warning("Staged outline: stage A attempt %s returned empty text", attempt)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Staged outline: stage A attempt %s failed: %s", attempt, exc)
        if not skeleton_text:
            logger.warning("Staged outline: stage A returned empty text; using local structured fallback")
            skeleton_text = self._fallback_book_skeleton(user_input, scale=scale)

        shared_context = (
            f"创意：\n{user_input}\n\n已生成的骨架：\n{skeleton_text}\n"
        )
        def _fallback_small_stage(stage_name: str) -> str:
            return self._fallback_small_stage_text(stage_name, user_input)

        async def _generate_small_stage(stage_name: str, messages: list[dict], min_chars: int = 0) -> str:
            last_text = ""
            for attempt in range(1, 3):
                try:
                    retry_messages = messages
                    if attempt > 1:
                        retry_messages = [dict(item) for item in messages]
                        retry_messages.append({
                            "role": "user",
                            "content": "上一轮返回为空或过短。请降低修辞密度，直接输出具体人物、目标、代价、关系变化、任务推进和伏笔流转；不要解释原因。",
                        })
                    result = await asyncio.wait_for(
                        self.router.generate(
                            task_type="outline_book",
                            messages=retry_messages,
                            stream=False,
                            request_timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                        ),
                        timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Staged outline: stage %s attempt %s failed: %s", stage_name, attempt, exc)
                    continue
                last_text = (getattr(result, "text", "") or "").strip()
                if last_text and (not min_chars or len(last_text) >= min_chars):
                    return last_text
                logger.warning(
                    "Staged outline: stage %s attempt %s returned short text, length=%s",
                    stage_name,
                    attempt,
                    len(last_text),
                )
            fallback_text = _fallback_small_stage(stage_name)
            if fallback_text:
                logger.warning("Staged outline: stage %s using local structured fallback", stage_name)
                return fallback_text
            return last_text

        characters_system = BOOK_OUTLINE_CHARACTERS_SYSTEM + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT
        world_system = BOOK_OUTLINE_WORLD_SYSTEM + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT

        section_two = await _generate_small_stage(
            "B2/characters",
            [
                {"role": "system", "content": characters_system},
                {
                    "role": "user",
                    "content": shared_context
                    + "\n请只生成二、主要角色与小传。必须以“二、主要角色与小传”开头。"
                    + "\n至少写 5 个关键角色；每个角色必须包含目标、代价、关系牵引、行动压力和后续伏笔，不要靠环境描写或华丽词藻凑字。"
                    + "\n本段不少于 1000 个中文字符。",
                },
            ],
            min_chars=1000,
        )
        section_four = await _generate_small_stage(
            "B4/relationships",
            [
                {"role": "system", "content": characters_system},
                {
                    "role": "user",
                    "content": shared_context
                    + f"\n已生成的角色段：\n{section_two}\n"
                    + "\n请只生成四、主要矛盾与关系网络。必须以“四、主要矛盾与关系网络”开头。"
                    + "\n写清谁推动谁、谁付出代价、关系如何从利用/信任/背叛/同盟发生变化，并标明哪些关系承担伏笔流转。"
                    + "\n本段不少于 500 个中文字符。",
                },
            ],
            min_chars=500,
        )
        section_five = await _generate_small_stage(
            "B5/factions",
            [
                {"role": "system", "content": characters_system},
                {
                    "role": "user",
                    "content": shared_context
                    + f"\n已生成的角色与关系段：\n{section_two}\n\n{section_four}\n"
                    + "\n请只生成五、势力格局。必须以“五、势力格局”开头。"
                    + "\n列出 3-5 个主要势力，写清核心利益、代表人物、可动用资源、制衡关系、与主角目标的冲突或交易。"
                    + "\n本段不少于 500 个中文字符。",
                },
            ],
            min_chars=500,
        )
        world_text = await _generate_small_stage(
            "C/world",
            [
                {"role": "system", "content": world_system},
                {
                    "role": "user",
                    "content": shared_context
                    + "\n请只生成六、世界观设定集。必须以“六、世界观设定集”开头。"
                    + "\n世界观设定必须服务人物行动、代价、资源限制、任务推进和伏笔回收，不要写与剧情无关的风景说明。",
                },
            ],
            min_chars=900,
        )
        characters_text = "\n\n".join(
            text for text in (section_two, section_four, section_five) if text
        )

        combined = self._reassemble_sections(
            skeleton_text, characters_text, world_text
        )
        _book_buckets = self._split_book_sections(
            skeleton_text, characters_text, world_text
        )
        volume_plan = self._extract_volume_plan(combined)
        combined = self._strip_volume_plan_tags(combined)
        _structured = self._build_book_structured(_book_buckets)
        return {
            "raw_text": combined,
            "volume_plan": volume_plan,
            "structured": _structured,
            "_staged": True,
            "_stage_lengths": {
                "skeleton": len(skeleton_text),
                "characters": len(characters_text),
                "world": len(world_text),
            },
            "_stages": {
                "skeleton": len(skeleton_text) >= BOOK_STAGE_MIN_CHARS["skeleton"],
                "characters": len(characters_text) >= BOOK_STAGE_MIN_CHARS["characters"],
                "world": len(world_text) >= BOOK_STAGE_MIN_CHARS["world"],
            },
        }

    def _reassemble_sections(
        self,
        skeleton_text: str,
        characters_text: str,
        world_text: str,
    ) -> str:
        """Split each stage's text by 一..九 headers and emit in canonical order.

        Each section is owned by exactly one stage (A: 一、三、七、八、九;
        B: 二、四、五; C: 六), so we let the first occurrence win
        when a stray stage happens to emit a section it doesn't own.
        """
        buckets: dict[str, str] = {}
        for text in (skeleton_text, characters_text, world_text):
            for num, body in self._iter_sections(text):
                buckets.setdefault(num, body)
        ordered: list[str] = []
        for num in self._SECTION_NUMS:
            body = buckets.get(num)
            if body:
                ordered.append(body.strip())
        return "\n\n".join(ordered)

    # PR-OUTLINE-STAGED-PERSIST-STRUCT — derive structured payload from
    # the staged Markdown so book outlines persist main_plot / characters
    # / world_setting / sections, not just raw_text.
    def _split_book_sections(
        self,
        skeleton_text: str,
        characters_text: str,
        world_text: str,
    ) -> dict[str, str]:
        buckets: dict[str, str] = {}
        for text in (skeleton_text, characters_text, world_text):
            for num, body in self._iter_sections(text):
                buckets.setdefault(num, body.strip())
        return buckets

    def _build_book_structured(self, buckets: dict[str, str]) -> dict:
        def _join(*nums: str) -> str:
            parts = [buckets.get(n, "").strip() for n in nums]
            return "\n\n".join(p for p in parts if p)
        out = {
            "main_plot": _join("一", "八", "九"),
            "characters": _join("二", "三", "四"),
            "world_setting": _join("五", "六"),
            "chapter_naming_style": buckets.get("七", "").strip(),
            "sections": dict(buckets),
        }
        return {k: v for k, v in out.items() if v}

    def _iter_sections(self, text: str):
        """Yield (section_num, full_section_text) pairs for 一..九 headers."""
        if not text:
            return
        pattern = re.compile(
            rf"(?m)^(?P<num>[{''.join(self._SECTION_NUMS)}])、"
        )
        matches = list(pattern.finditer(text))
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            yield m.group("num"), text[start:end]

    # ------------------------------------------------------------------
    # PR-OL1 — extract structured volume plan from outline text
    # ------------------------------------------------------------------
    def _extract_volume_plan(self, text: str) -> list[dict] | None:
        """Parse the <volume-plan>[...]</volume-plan> JSON block.

        Returns the parsed list-of-dicts on success, or None if missing/
        malformed. Tolerates leading/trailing whitespace and stray ```json
        fences a model might emit. Caller treats None as "fall back to
        legacy detect-N-from-text" so a parse failure never breaks the
        outline pipeline.
        """
        if not text:
            return None
        matches = re.findall(r"<volume-plan>\s*(.+?)\s*</volume-plan>", text, re.DOTALL)
        if not matches:
            return None
        last_error: Exception | None = None
        for raw_body in reversed(matches):
            body = raw_body.strip()
            # Strip stray ```json ... ``` fences
            body = re.sub(r"^```(?:json)?\s*", "", body)
            body = re.sub(r"\s*```$", "", body)
            try:
                data = json.loads(body)
            except Exception as exc:
                last_error = exc
                continue
            if not isinstance(data, list) or not data:
                continue
            out: list[dict] = []
            for i, item in enumerate(data, start=1):
                if not isinstance(item, dict):
                    continue
                out.append({
                    "idx": int(item.get("idx") or i),
                    "title": str(item.get("title") or f"第{i}卷"),
                    "theme": str(item.get("theme") or ""),
                    "core_conflict": str(item.get("core_conflict") or ""),
                    "est_chapters": int(item.get("est_chapters") or 10),
                })
            if out:
                return out
        if last_error:
            logger.warning("_extract_volume_plan: JSON parse failed: %s", last_error)
        return None

    # ------------------------------------------------------------------
    # PR-OL15 — strip <volume-plan>...</volume-plan> tags from raw text.
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_volume_plan_tags(text: str) -> str:
        if not text:
            return text
        return re.sub(r"<volume-plan>.+?</volume-plan>\s*", "", text, flags=re.DOTALL)

    # ------------------------------------------------------------------
    # v1.4.2 Task B — staged book-outline SSE stream
    # ------------------------------------------------------------------
    async def _generate_book_outline_staged_stream(
        self, user_input: str, *, scale: dict | None = None
    ):
        """Stream the staged book outline as structured SSE-ready events.

        Yields dicts with one of the following ``event`` values:

        - ``stage_start``: {stage, label, index, total}
        - ``stage_chunk``: {stage, delta}
        - ``stage_end``:   {stage, full_text}
        - ``error``:       {stage, message}
        - ``done``:        {full_outline, _stages}

        Stage A (skeleton) is streamed first and must complete before B/C
        start. Stages B (characters) and C (world) run concurrently and
        interleave their chunks by arrival order via an ``asyncio.Queue``.
        """
        # Stage A — skeleton (一、三、七、八、九).
        yield {
            "event": "stage_start",
            "stage": "A",
            "label": "骨架",
            "index": 1,
            "total": 3,
        }
        a_buf: list[str] = []
        # PR-OL10: inject hard volume-count directive into skeleton prompt.
        skeleton_system = self._apply_scale_to_prompt(BOOK_OUTLINE_SKELETON_SYSTEM, scale)
        skeleton_system = skeleton_system + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT
        skeleton_msgs = [
            {"role": "system", "content": skeleton_system},
            {
                "role": "user",
                "content": f"创意：\n{user_input}\n\n请生成骨架五段。",
            },
        ]
        try:
            async for delta in self.router.generate_stream(
                task_type="outline_book",
                messages=skeleton_msgs,
                _log_meta=self._log_meta("outline_book_skeleton"),
            ):
                if not delta:
                    continue
                a_buf.append(delta)
                yield {"event": "stage_chunk", "stage": "A", "delta": delta}
        except Exception as exc:  # noqa: BLE001 — surface as structured event
            logger.warning("Staged stream: stage A failed: %s", exc)
            yield {"event": "error", "stage": "A", "message": str(exc)}
            a_buf = [self._fallback_book_skeleton(user_input, scale=scale)]

        skeleton_text = "".join(a_buf).strip()
        if not skeleton_text:
            logger.warning("Staged stream: stage A returned empty text; using local structured fallback")
            skeleton_text = self._fallback_book_skeleton(user_input, scale=scale)
            yield {"event": "stage_chunk", "stage": "A", "delta": skeleton_text}
        yield {"event": "stage_end", "stage": "A", "full_text": skeleton_text}

        shared_context = (
            f"创意：\n{user_input}\n\n已生成的骨架：\n{skeleton_text}\n"
        )
        stages = [
            {
                "code": "B",
                "label": "角色",
                "index": 2,
                "msgs": [
                    {
                        "role": "system",
                        "content": BOOK_OUTLINE_CHARACTERS_SYSTEM + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": shared_context
                        + "\n请生成二、四、五三段。必须包含：二、主要角色与小传；四、主要矛盾与关系网络；五、势力格局。"
                        + "\n每一段都要可执行、具体，三段合计不少于 1800 个中文字符；扩写必须靠人物目标、选择代价、关系变化、任务推进和伏笔流转，不要只写提纲标题。",
                    },
                ],
            },
            {
                "code": "C",
                "label": "世界观",
                "index": 3,
                "msgs": [
                    {
                        "role": "system",
                        "content": BOOK_OUTLINE_WORLD_SYSTEM + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": shared_context + "\n请生成六、世界观设定集。世界观设定必须服务人物行动、代价、资源限制、任务推进和伏笔回收。",
                    },
                ],
            },
        ]

        queue: asyncio.Queue = asyncio.Queue()
        buffers: dict[str, list[str]] = {"B": [], "C": []}
        DONE_MARK = object()

        async def _worker(stage: dict) -> None:
            code = stage["code"]
            await queue.put(
                {
                    "event": "stage_start",
                    "stage": code,
                    "label": stage["label"],
                    "index": stage["index"],
                    "total": 3,
                }
            )
            try:
                async for delta in self.router.generate_stream(
                    task_type="outline_book",
                    messages=stage["msgs"],
                    _log_meta=self._log_meta(f"outline_book_stage_{code}"),
                ):
                    if not delta:
                        continue
                    buffers[code].append(delta)
                    await queue.put(
                        {"event": "stage_chunk", "stage": code, "delta": delta}
                    )
                current_text = "".join(buffers[code]).strip()
                min_chars = (
                    BOOK_STAGE_MIN_CHARS["characters"]
                    if code == "B"
                    else BOOK_STAGE_MIN_CHARS["world"]
                )
                if len(current_text) < min_chars:
                    logger.warning(
                        "Staged stream: stage %s returned too little content (%d < %d); retrying non-stream",
                        code,
                        len(current_text),
                        min_chars,
                    )
                    fallback = await asyncio.wait_for(
                        self.router.generate(
                            task_type="outline_book",
                            messages=stage["msgs"],
                            _log_meta=self._log_meta(f"outline_book_stage_{code}_fallback"),
                            stream=False,
                            request_timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                        ),
                        timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                    )
                    text = (fallback.text or "").strip()
                    if len(text) > len(current_text):
                        buffers[code] = [text]
                        await queue.put(
                            {"event": "stage_chunk", "stage": code, "delta": text}
                        )
                await queue.put(
                    {
                        "event": "stage_end",
                        "stage": code,
                        "full_text": "".join(buffers[code]).strip(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Staged stream: stage %s failed: %s", code, exc
                )
                await queue.put(
                    {"event": "error", "stage": code, "message": str(exc)}
                )
            finally:
                await queue.put(DONE_MARK)

        tasks = [asyncio.create_task(_worker(s)) for s in stages]
        remaining = len(tasks)
        try:
            while remaining > 0:
                item = await queue.get()
                if item is DONE_MARK:
                    remaining -= 1
                    continue
                yield item
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        characters_text = "".join(buffers["B"]).strip()
        world_text = "".join(buffers["C"]).strip()
        # PR-OUTLINE-STAGED-PERSIST-STRUCT: split first, then reassemble +
        # build structured payload so persist_outline_now can promote
        # main_plot / characters / world_setting / sections to top-level.
        _book_buckets = self._split_book_sections(
            skeleton_text, characters_text, world_text
        )
        combined = self._reassemble_sections(
            skeleton_text, characters_text, world_text
        )
        # PR-OL1: extract structured volume plan for downstream wizard.
        volume_plan = self._extract_volume_plan(combined)
        # PR-OL15: strip <volume-plan> tags from combined so the user-visible
        # raw_text never leaks the LLM-internal control tag.
        combined = self._strip_volume_plan_tags(combined)
        _structured = self._build_book_structured(_book_buckets)
        yield {
            "event": "done",
            "full_outline": combined,
            "volume_plan": volume_plan,
            "structured": _structured,
            "_stage_lengths": {
                "skeleton": len(skeleton_text),
                "characters": len(characters_text),
                "world": len(world_text),
            },
            "_stages": {
                "skeleton": len(skeleton_text) >= BOOK_STAGE_MIN_CHARS["skeleton"],
                "characters": len(characters_text) >= BOOK_STAGE_MIN_CHARS["characters"],
                "world": len(world_text) >= BOOK_STAGE_MIN_CHARS["world"],
            },
        }

    async def generate_volume_outline(
        self,
        book_outline: dict,
        volume_idx: int,
        user_notes: str = "",
        stream: bool = False,
        staged: bool = True,
    ) -> dict | AsyncIterator[str]:
        """Generate a volume-level outline from the book outline."""
        context = (
            f"全书大纲：\n{json.dumps(book_outline, ensure_ascii=False, indent=2)}\n\n"
            f"请生成第 {volume_idx} 卷的详细大纲。"
        )
        if user_notes:
            context += f"\n\n用户补充说明：{user_notes}"

        _vol_sys = VOLUME_OUTLINE_SYSTEM + OUTLINE_VOLUME_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT
        if self.chapter_naming_directive:
            _vol_sys = _vol_sys + "\n\n" + self.chapter_naming_directive
        messages = [
            {"role": "system", "content": _vol_sys},
            {"role": "user", "content": context},
        ]

        if stream and staged:
            return self._generate_volume_outline_staged_text_stream(
                book_outline=book_outline,
                volume_idx=volume_idx,
                user_notes=user_notes,
            )

        if stream:
            # Legacy single-call streaming path. Kept for callers that
            # explicitly disable staged mode.
            return self.router.generate_stream(
                task_type="outline_volume",
                messages=messages,
                _log_meta=self._log_meta("outline_volume"),
            )

        if staged:
            # v1.4.2 Task C: meta + batched chapter summaries.
            return await self._generate_volume_outline_staged(
                book_outline=book_outline,
                volume_idx=volume_idx,
                user_notes=user_notes,
            )

        result = await self.router.generate(
            task_type="outline_volume",
            messages=messages,
        )
        return self._parse_json(result.text)

    async def _generate_volume_outline_staged_text_stream(
        self,
        book_outline: dict,
        volume_idx: int,
        user_notes: str = "",
    ) -> AsyncIterator[str]:
        """SSE-compatible wrapper for the staged volume-outline pipeline."""
        result = await self._generate_volume_outline_staged(
            book_outline=book_outline,
            volume_idx=volume_idx,
            user_notes=user_notes,
        )
        yield json.dumps(result, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # v1.4.2 Task C — staged volume outline
    # ------------------------------------------------------------------
    async def _generate_volume_outline_staged(
        self,
        book_outline: dict,
        volume_idx: int,
        user_notes: str = "",
    ) -> dict:
        """Generate a volume outline in two stages to avoid long-output cliff.

        Stage V1: meta only (no chapter_summaries). chapter_count is an int.
        Stage V2: loop ceil(chapter_count/5) batches, each returning at most
        5 chapter summaries. Each batch sees V1 meta + the last 2 summaries
        from the previous batch so adjacent batches stay consistent.

        Returns the merged dict with the same shape the legacy call produced:
        ``{...meta, "chapter_summaries": [...]}``.
        """
        # E2E fix (2026-07-26): honor the project's explicit chapters-per-volume
        # setting. Threaded into BOTH the meta prompt (hard instruction) and the
        # fallback meta (parse-failure path), so the count no longer silently
        # reverts to the 10-chapter default.
        cpv_pref = await self._load_chapters_per_volume_pref()
        preferred_chapter_count: int | None = None
        cpv_directive = ""
        if cpv_pref is not None:
            cpv_lo, cpv_hi = cpv_pref
            preferred_chapter_count = cpv_hi
            if cpv_lo == cpv_hi:
                cpv_directive = f"本卷章数（chapter_count）必须等于 {cpv_lo}。"
            else:
                cpv_directive = (
                    f"本卷章数（chapter_count）必须在 {cpv_lo}-{cpv_hi} 之间。"
                )
            cpv_directive = (
                "\n\n【章数硬约束（项目设置）】"
                + cpv_directive
                + "此约束优先于全书大纲中的任何章数建议。"
            )

        # Stage V1 — meta.
        meta_ctx = (
            f"全书大纲：\n{json.dumps(book_outline, ensure_ascii=False, indent=2)}\n\n"
            f"请生成第 {volume_idx} 卷的元信息（不包含章节摘要）。"
        )
        meta_ctx += cpv_directive
        if user_notes:
            meta_ctx += f"\n\n用户补充说明：{user_notes}"

        try:
            meta_result = await asyncio.wait_for(
                self.router.generate(
                    task_type=OUTLINE_VOLUME_STAGED_TASK_TYPE,
                    messages=[
                        {"role": "system", "content": VOLUME_META_SYSTEM + OUTLINE_VOLUME_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT + (("\n\n" + self.chapter_naming_directive) if self.chapter_naming_directive else "")},
                        {"role": "user", "content": meta_ctx},
                    ],
                    max_tokens=4096,
                ),
                timeout=OUTLINE_LLM_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Staged volume outline: meta call failed: %r", exc)
            return {"_parse_error": True, "raw_text": str(exc)}

        meta = self._parse_json(meta_result.text)
        if not isinstance(meta, dict) or meta.get("_parse_error"):
            logger.warning("Staged volume outline: meta parse failed; using fallback meta")
            meta = self._fallback_volume_meta(
                book_outline,
                volume_idx,
                user_notes,
                preferred_chapter_count=preferred_chapter_count,
            )

        # Normalize chapter_count to int; fall back gracefully.
        # PR-OUTLINE-JSON-REPAIR (2026-07-04): json_repair can now recover a
        # PARTIAL meta from a truncated stream (e.g. only volume_idx/title/
        # core_conflict, no chapter_count). Such a meta lacks chapter_count but
        # is NOT flagged _parse_error, so it previously fell through here, hit
        # int(None), and aborted V2 with 0 chapters — worse than a clean
        # fallback. Backfill the count (and any other missing meta fields) from
        # _fallback_volume_meta while keeping the recovered on-genre fields.
        raw_cc = meta.get("chapter_count")
        try:
            chapter_count = int(raw_cc)
        except (TypeError, ValueError):
            logger.warning(
                "Staged volume outline: missing/invalid chapter_count=%r; "
                "backfilling meta from fallback",
                raw_cc,
            )
            fallback = self._fallback_volume_meta(
                book_outline,
                volume_idx,
                user_notes,
                preferred_chapter_count=preferred_chapter_count,
            )
            for key, value in fallback.items():
                if not meta.get(key):
                    meta[key] = value
            try:
                chapter_count = int(meta.get("chapter_count"))
            except (TypeError, ValueError):
                meta.setdefault("chapter_summaries", [])
                return meta
        meta["chapter_count"] = chapter_count

        if chapter_count <= 0:
            meta["chapter_summaries"] = []
            return meta

        # Stage V2 — batched chapter summaries.
        # Five chapters per model call is slower than ten, but much more
        # resilient for 150-chapter volumes: failed batches are cheaper to
        # retry and the model is less likely to return empty JSON.
        BATCH = 5
        batches = math.ceil(chapter_count / BATCH)
        meta_for_ctx = {
            k: v for k, v in meta.items() if k != "chapter_summaries"
        }
        def _compact(value, limit: int = 420) -> str:
            if isinstance(value, str):
                text = value
            else:
                text = json.dumps(value, ensure_ascii=False)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:limit]

        compact_meta_for_batch = {
            "volume_idx": meta_for_ctx.get("volume_idx", volume_idx),
            "title": _compact(meta_for_ctx.get("title", f"第{volume_idx}卷"), 80),
            "core_conflict": _compact(meta_for_ctx.get("core_conflict", "")),
            "turning_points": _compact(meta_for_ctx.get("turning_points", [])),
            "emotional_arc": _compact(meta_for_ctx.get("emotional_arc", ""), 260),
            "transition_to_next": _compact(meta_for_ctx.get("transition_to_next", ""), 260),
        }
        # PR-VOL-PREMISE-ANCHOR (2026-06-29): the batch prompt MUST carry the
        # book premise. When V1 meta JSON fails to parse, meta degrades to an
        # empty fallback (_meta_fallback) — and a batch prompt that sees only
        # that empty meta lets the model drift completely off-genre (live: a
        # 近未来科幻 book produced generic 武侠 chapter summaries). Anchor each
        # batch to a compact book premise so genre/characters stay fixed.
        book_premise_anchor = _compact(self._book_premise_digest(book_outline), 600)
        all_summaries: list[dict] = []

        for b in range(batches):
            start = b * BATCH + 1
            end = min((b + 1) * BATCH, chapter_count)
            items: list | None = None
            for attempt in range(1, 4):
                logger.info(
                    "Staged volume outline: batch %d/%d chapters %d-%d attempt %d start",
                    b + 1,
                    batches,
                    start,
                    end,
                    attempt,
                )
                tail = [
                    {
                        "chapter_idx": item.get("chapter_idx"),
                        "title": item.get("title"),
                        "summary": _compact(item.get("summary", ""), 120),
                    }
                    for item in all_summaries[-2:]
                    if isinstance(item, dict)
                ]
                tail_str = (
                    json.dumps(tail, ensure_ascii=False, indent=2)
                    if tail
                    else "（无）"
                )
                retry_note = "" if attempt == 1 else "\n上一次返回格式不合格。请只返回 JSON，顶层必须包含 batch 数组，且数组长度必须准确。"
                batch_ctx = (
                    f"全书前提（必须严格遵守，章节必须发生在这个世界观/题材/人物范围内）：\n{book_premise_anchor}\n\n"
                    f"卷元信息（已压缩）：\n{json.dumps(compact_meta_for_batch, ensure_ascii=False, indent=2)}\n\n"
                    f"已生成的最近几章摘要：\n{tail_str}\n\n"
                    f"start={start}, end={end}, count={end - start + 1}。"
                    f"请生成第 {start} 章到第 {end} 章的章节骨架。"
                    f"chapter_idx 必须从 {start} 连续到 {end}。"
                    f"\n必须返回：{{\"batch\": [{{...}}]}}，batch 数组长度必须是 {end - start + 1}。"
                    f"{retry_note}"
                )
                try:
                    batch_result = await asyncio.wait_for(
                        self.router.generate(
                            task_type=OUTLINE_VOLUME_STAGED_TASK_TYPE,
                            messages=[
                                {"role": "system", "content": VOLUME_CHAPTERS_SKELETON_SYSTEM + (("\n\n" + self.chapter_naming_directive) if self.chapter_naming_directive else "")},
                                {"role": "user", "content": batch_ctx},
                            ],
                            max_tokens=4096,
                        ),
                        timeout=OUTLINE_LLM_CALL_TIMEOUT_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Staged volume outline: batch %d/%d attempt %d failed: %r",
                        b + 1,
                        batches,
                        attempt,
                        exc,
                    )
                    continue

                parsed = self._parse_json(batch_result.text)
                if isinstance(parsed, dict):
                    raw_items = (
                        parsed.get("batch")
                        or parsed.get("chapter_summaries")
                        or parsed.get("chapters")
                    )
                elif isinstance(parsed, list):
                    raw_items = parsed
                else:
                    raw_items = None
                expected_len = end - start + 1
                if isinstance(raw_items, list) and len(raw_items) >= expected_len:
                    items = raw_items[:expected_len]
                    logger.info(
                        "Staged volume outline: batch %d/%d accepted %d items",
                        b + 1,
                        batches,
                        len(items),
                    )
                    break
                logger.warning(
                    "Staged volume outline: batch %d/%d attempt %d returned invalid shape/count",
                    b + 1,
                    batches,
                    attempt,
                )

            if not isinstance(items, list):
                logger.warning(
                    "Staged volume outline: batch %d/%d exhausted retries",
                    b + 1,
                    batches,
                )
                return {
                    "_parse_error": True,
                    "raw_text": f"volume batch {b + 1}/{batches} failed for chapters {start}-{end}",
                    "meta": meta_for_ctx,
                    "chapter_summaries": all_summaries,
                }

            # Normalize chapter_idx in case the model drifts.
            expected = start
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("chapter_idx") != expected:
                    item["chapter_idx"] = expected
                all_summaries.append(item)
                expected += 1
                if expected > end:
                    break

        if len(all_summaries) != chapter_count:
            logger.warning(
                "Staged volume outline: expected %d chapter summaries, got %d",
                chapter_count,
                len(all_summaries),
            )
            return {
                "_parse_error": True,
                "raw_text": f"expected {chapter_count} chapter summaries, got {len(all_summaries)}",
                "meta": meta_for_ctx,
                "chapter_summaries": all_summaries,
            }

        # PR-TITLE-Q1: rule-based title check + batch LLM rewrite for any
        # violators so the persisted vol outline never carries sloppy
        # titles. Failure here is non-fatal; original titles survive.
        try:
            from app.services.title_quality_checker import (
                check_and_rewrite_in_place as _tq_check,
            )
            _tq_stats = await asyncio.wait_for(
                _tq_check(
                    all_summaries,
                    volume_meta=meta_for_ctx,
                    project_id=self.project_id,
                ),
                timeout=OUTLINE_TITLE_CHECK_TIMEOUT_SECONDS,
            )
            logger.info(
                "Staged volume outline: title quality check %s", _tq_stats,
            )
        except Exception as _tq_err:  # noqa: BLE001
            logger.warning(
                "Staged volume outline: title quality check failed: %s",
                _tq_err,
            )
        merged = dict(meta_for_ctx)
        merged["chapter_summaries"] = all_summaries
        return merged

    async def generate_chapter_outline(
        self,
        book_outline: dict,
        volume_outline: dict,
        chapter_idx: int,
        previous_chapter_summary: str = "",
        user_notes: str = "",
        stream: bool = False,
    ) -> dict | AsyncIterator[str]:
        """Generate a chapter-level outline from the volume outline."""
        context = (
            f"全书大纲摘要：\n{json.dumps({'title': book_outline.get('title'), 'main_plot': book_outline.get('main_plot')}, ensure_ascii=False)}\n\n"
            f"本卷大纲：\n{json.dumps(volume_outline, ensure_ascii=False, indent=2)}\n\n"
            f"请生成第 {chapter_idx} 章的详细大纲。"
        )
        if previous_chapter_summary:
            context += f"\n\n上一章摘要：{previous_chapter_summary}"
        if user_notes:
            context += f"\n\n用户补充说明：{user_notes}"

        _ch_sys = CHAPTER_OUTLINE_SYSTEM + OUTLINE_CHAPTER_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT
        if self.chapter_naming_directive:
            _ch_sys = _ch_sys + "\n\n" + self.chapter_naming_directive
        messages = [
            {"role": "system", "content": _ch_sys},
            {"role": "user", "content": context},
        ]

        if stream:
            return self.router.generate_stream(
                task_type="outline_chapter",
                messages=messages,
                _log_meta=self._log_meta("outline_chapter"),
            )

        result = await self.router.generate(
            task_type="outline_chapter",
            messages=messages,
            _log_meta=self._log_meta("outline_chapter"),
        )
        return self._parse_json(result.text)

    def _parse_json(self, text: str) -> dict:
        """Parse JSON from LLM response, handling markdown code blocks."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:]  # remove opening ```json
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]  # remove closing ```
            cleaned = "\n".join(lines)
        cleaned = self._repair_json_text(cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # PR-OUTLINE-JSON-REPAIR (2026-07-04): the relay frequently returns
            # truncated / unterminated JSON for a single volume batch (hit
            # max_tokens or mid-stream cut). The hand-rolled regex repair above
            # only fixes delimiter slips, not unclosed strings/brackets, so the
            # whole volume was voided (_parse_error → 0 chapters materialized).
            # Fall back to json_repair (already used by prompt_registry) which
            # closes dangling structures, recovering the batch instead of
            # discarding it. Empty/unrecoverable results still fail cleanly.
            try:
                from json_repair import loads as _repair_loads
                repaired = _repair_loads(cleaned)
                if repaired not in ("", None, [], {}):
                    return repaired
            except Exception:  # noqa: BLE001
                pass
            preview = cleaned[:500].replace("\n", " ")
            logger.warning("Failed to parse outline JSON: %s; preview=%s", exc, preview)
            return {"raw_text": text, "_parse_error": True}

    @staticmethod
    def _repair_json_text(text: str) -> str:
        """Repair common LLM JSON slips without changing prose content."""
        if not text:
            return text
        start_candidates = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
        if start_candidates:
            start = min(start_candidates)
            end = max(text.rfind("}"), text.rfind("]"))
            if end > start:
                text = text[start:end + 1]
        text = re.sub(r'(?<=[}\]"\d])，(?=\s*["{\[])', ',', text)
        text = re.sub(r'(?<=")：(?=\s*)', ':', text)
        text = re.sub(r',\s*([}\]])', r'\1', text)
        return text

    @staticmethod
    def _book_premise_digest(book_outline: dict) -> str:
        """Extract a compact, genre/character-bearing premise from the book
        outline for anchoring volume batch prompts.

        The book outline can be either a structured dict (title/core_concept/
        main_plot/...) or a thin ``{"raw_text": "..."}`` blob (staged stream
        fallback). Pull whatever premise-bearing fields exist; fall back to the
        raw text head so the anchor is never empty.
        """
        if not isinstance(book_outline, dict):
            return str(book_outline or "")
        parts: list[str] = []
        for key in ("title", "core_concept", "core_idea", "genre", "main_plot",
                    "premise", "logline", "themes"):
            val = book_outline.get(key)
            if not val:
                continue
            if isinstance(val, str):
                parts.append(val.strip())
            else:
                parts.append(json.dumps(val, ensure_ascii=False))
        digest = " / ".join(p for p in parts if p)
        if digest.strip():
            return digest
        # Thin outline: defer to raw_text head (carries the user's premise).
        return str(book_outline.get("raw_text") or "")

    @staticmethod
    def _fallback_volume_meta(
        book_outline: dict,
        volume_idx: int,
        user_notes: str = "",
        preferred_chapter_count: int | None = None,
    ) -> dict:
        plan_item: dict | None = None
        raw_plan = book_outline.get("volume_plan") if isinstance(book_outline, dict) else None
        if isinstance(raw_plan, list):
            for item in raw_plan:
                if isinstance(item, dict) and int(item.get("idx") or 0) == volume_idx:
                    plan_item = item
                    break

        def _note_value(label: str) -> str:
            match = re.search(rf"{re.escape(label)}：(.+?)。", user_notes or "")
            return match.group(1).strip() if match else ""

        title = _note_value("本卷标题") or (str(plan_item.get("title")) if plan_item and plan_item.get("title") else f"第{volume_idx}卷")
        theme = _note_value("本卷主题") or (str(plan_item.get("theme")) if plan_item and plan_item.get("theme") else "")
        conflict = _note_value("本卷核心冲突") or (str(plan_item.get("core_conflict")) if plan_item and plan_item.get("core_conflict") else theme)
        chapter_match = re.search(r"chapter_count=(\d+)", user_notes or "")
        # Precedence: explicit user_notes count > project setting
        # (chapters_per_volume_* in settings_json, resolved by the caller) >
        # book-outline plan estimate > legacy default 10.
        if chapter_match:
            chapter_count = int(chapter_match.group(1))
        elif preferred_chapter_count is not None:
            chapter_count = int(preferred_chapter_count)
        elif plan_item:
            chapter_count = int(plan_item.get("est_chapters") or 10)
        else:
            chapter_count = 10
        return {
            "volume_idx": volume_idx,
            "title": title,
            "core_conflict": conflict,
            "turning_points": [conflict] if conflict else [],
            "new_characters": [],
            "departing_characters": [],
            "foreshadows": {"planted": [], "resolved": []},
            "emotional_arc": theme,
            "chapter_count": chapter_count,
            "transition_to_next": "承接下一卷计划。",
            "_meta_fallback": True,
        }

def format_chapter_naming_directive(config_json: dict | None) -> str:
    """PR-CHAPTER-NAMING: render style_profile.config_json[chapter_naming_style]
    into a directive block for outline LLM prompts.

    Returns empty string when config_json has no chapter_naming_style.
    """
    if not config_json or not isinstance(config_json, dict):
        return ""
    cns = config_json.get("chapter_naming_style")
    if not isinstance(cns, dict):
        return ""
    parts: list[str] = ["【参考作者「章节命名风格」】 以下是从参考书学到的命名原则，生成 title 时应严格遵循："]
    principles = cns.get("overall_principles") or []
    if isinstance(principles, list) and principles:
        parts.append("【总则】")
        for p in principles[:8]:
            if isinstance(p, str) and p.strip():
                parts.append(f"· {p.strip()}")
    patterns = cns.get("naming_patterns") or []
    if isinstance(patterns, list) and patterns:
        parts.append("\n【可选命名路径】")
        for pat in patterns[:6]:
            if not isinstance(pat, dict):
                continue
            name = (pat.get("name") or "").strip()
            desc = (pat.get("description") or "").strip()
            samples = pat.get("examples") or pat.get("sample_titles") or []
            sample_str = "、".join([str(x) for x in samples[:3] if x])
            line = f"· {name}: {desc}"
            if sample_str:
                line += f" （如：{sample_str}）"
            parts.append(line)
    relations = cns.get("title_content_relations") or []
    if isinstance(relations, list) and relations:
        parts.append("\n【title 与本章内容的关联手法（优先选一种）】")
        for rel in relations[:5]:
            if not isinstance(rel, dict):
                continue
            name = (rel.get("name") or "").strip()
            desc = (rel.get("description") or "").strip()
            parts.append(f"· {name}: {desc}")
    avoid = cns.get("avoid_patterns") or []
    if isinstance(avoid, list) and avoid:
        parts.append("\n【忌论】")
        for a in avoid[:6]:
            if isinstance(a, str) and a.strip():
                parts.append(f"· {a.strip()}")
    examples = cns.get("example_titles") or []
    if isinstance(examples, list) and examples:
        parts.append("\n【高质量范例】")
        rendered_count = 0
        for ex in examples:
            if rendered_count >= 12:
                break
            if not isinstance(ex, dict):
                continue
            t = (ex.get("title") or "").strip()
            if not t:
                continue
            tech = (ex.get("technique") or "").strip()
            cs = (ex.get("content_summary") or "").strip()
            line = f"· 「{t}」"
            if tech:
                line += f" [{tech}]"
            if cs:
                line += f" — {cs}"
            parts.append(line)
            rendered_count += 1
    parts.append("\n【硬约束】title 不允许为：“第N章” / 纯数字 / 重复卷名 / 存纯抽象名词（如《恍惚》《争锉》这类空词）。优先选“本章主事件的关键具象意象”或“章末状态的诗化意象句”作为章名。")
    return "\n".join(parts)

