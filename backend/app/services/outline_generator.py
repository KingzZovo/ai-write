"""
Hierarchical Outline Generator

Generates outlines at three levels:
1. Book-level: Overall plot arc, core characters, world-building, estimated scale
2. Volume-level: Per-volume conflicts, turning points, new/departing characters
3. Chapter-level: Per-chapter plot points, characters, emotional arc, transitions
"""

from __future__ import annotations

from contextlib import aclosing

from app.services.outline_authority import outline_for_generation

import asyncio
import json
import logging
import math
import re
from typing import AsyncIterator

from app.services.outline_prompt_runner import OutlinePromptRunner
from app.services.chapter_target_words import (
    resolve_chapters_per_volume_pref as _resolve_cpv_pref,
    resolve_volume_chapter_range as _resolve_volume_cpv_range,
)
from app.services.narrative_contract import WORLD_LOGIC_CONTRACT

logger = logging.getLogger(__name__)


OUTLINE_LLM_CALL_TIMEOUT_SECONDS = 240
OUTLINE_FAST_CALL_TIMEOUT_SECONDS = 120
OUTLINE_TITLE_CHECK_TIMEOUT_SECONDS = 120
OUTLINE_VOLUME_MAX_TOKENS = 8192
BOOK_STAGE_MIN_CHARS = {
    "skeleton": 1200,
    "characters": 1200,
    "world": 1200,
}

def _book_stage_min_chars(scale: dict | None, stage: str) -> int:
    """Scale the incomplete-response guard; length is not literary acceptance."""
    default = BOOK_STAGE_MIN_CHARS[stage]
    if not scale or not scale.get("target_word_count"):
        return default
    return min(default, max(120, int(scale["target_word_count"]) // 20))


OUTLINE_WRITING_QUALITY_PROMPT = """

【写作质量准则】
生成前先校准硬事实，再输出内容：人物身份、关系、动机、伤势、筹码、时间线不能自相矛盾。
不要用大量环境描写、华丽词藻、比喻修辞凑字数；扩写必须靠具体行动、选择代价、人物关系变化、任务进度和伏笔流转。
保持故事感和正在发生的动作感，语言直接自然，避免文艺腔、模板腔、机械总结。
每段只围绕本段职责展开；发现问题时只修正问题点，不推翻已合格内容。
"""


OUTLINE_BOOK_CONTRACT_PROMPT = f"""

{WORLD_LOGIC_CONTRACT}

【世界设定段的规则汇总】
仅在六、世界观设定集末尾汇总一次 world_logic_contract，其他段落写实际剧情，不重复抄写规则。
只写会影响本书行动或结局的规则，沿用以下适用字段；无关字段直接省略，不逐项写“不涉及”来占位：
- time_rules：实际期限、先后与必要的时间消耗。
- space_rules：人物和重要物品的所在、转移和最终保管安排。
- power_resource_rules：当前人物或组织真实可用的资源与权限。
- information_rules：已知事实、推测、未知与确认依据。
- mechanism_rules：本书实际使用的技术、能力或制度的条件和边界。
- result_strength_rules：具体行动能兑现什么结果，哪些仍留待后续；已有充分依据的结果可以完成。
只有故事确实用到特殊机制或跨卷转折时，再说明相应的 semantic_anchor_rules、faction_bargain_rules、physical_constraint_rules 或 cross_volume_catalyst_rules。不要为填写这些字段创造审查、阴谋、代价或额外卷数。
"""

OUTLINE_VOLUME_CONTRACT_PROMPT = """

【分卷层世界逻辑合同】
本卷大纲必须继承全书 world_logic_contract，并额外输出以下 JSON 字段，字段内容必须从当前题材/设定/全书大纲动态推导，禁止写固定题材补丁：
- volume_start_state：本卷开头的人物、地点、资源、信息、冲突状态。
- volume_end_state：本卷结束时实际达到的状态，包括承诺兑现和重要物品去向；已充分铺垫的结果可以完成。
- volume_power_resource_map：本卷人物实际可用的工具、时间、关系或能力，以及已有的限制；不强设敌对双方或代价。
- volume_information_map：本卷关键信息由谁知道、如何得知；没有误判时不另造误判。
- volume_mechanism_limits：本卷实际用到的能力、技术或工具能做什么、不能做什么；无特殊机制时说明相关日常限制。
- volume_result_strength_ladder：本卷行动与结果的因果依据；按当前题材说明，不套用翻盘、胜负或升级阶梯。
- foreshadow_progression：已有伏笔的铺垫和兑现；没有伏笔时写“本卷无伏笔”，不为填字段增加秘密。
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
- power_resource_map：本章人物实际可用的时间、工具、关系与限制；不强设敌我或违抗代价。
- mechanism_limits：本章使用的能力、技术、法术、制度工具、证据、资源调用的条件、成本、边界、反制。
- result_strength：本章行动和结果之间的依据；已充分铺垫的结果可以完成，未获依据的结论不能凭空成立。
- handoff_to_next：本章如何把时间、空间、人物、物件、信息状态交接给下一章。
- transition_bridge：有后章时说明实际需要的时间流逝、移动、物件与信息交接；终章说明收束。没有移动或未解决风险时不为填栏创造。
"""

# Shared sections keep single-shot and staged planning on the same author contract.
_BOOK_SECTIONS = {
    "一": """一、书名与核心概念
书名与一句话故事内核，保留作者明确的题材、人物和结局安排。""",
    "二": """二、主要角色与小传
只写实际承担剧情的角色，人数由故事需要决定。说明身份、当下处境、愿望、顾虑、行为习惯与重要关系。
背景经历只补充能解释本书行动的部分；不要求人人有创伤、秘密或性格伪装。次要人物可以简写，不为填栏目虚构家世和童年。
保留已给定的性别、称谓与关系；未命名的家人可沿用称谓。需要时给出体现具体口吻的短对白。""",
    "三": """三、主角变化与关键选择
按实际剧情阶段写初始处境、关键选择、行动结果及情绪或关系变化。阶段数量服从篇幅，不凑行数。
成长可以是判断、手艺、认识或关系的变化；只有题材确有能力升级时才写境界、装备、修炼规则及实际限制。平常的选择不必附加惩罚或反噬。""",
    "四": """四、主要矛盾与关系网络
写清谁希望什么、彼此如何影响，以及具体事件如何改变关系。既可有冲突，也可有合作、日常照料和坦诚沟通。
人物不必各自成为敌人或盟友；避免把所有关系写成交易，不为推动剧情强加误会。""",
    "五": """五、势力格局
按题材写实际参与的组织、家庭、工作关系或社会环境及其作用。没有阵营斗争时，简述相关现实关系即可。
只保留影响人物选择的资源、权责或利益条件；不为凑数新增势力，不把客户与家人硬分成对抗阵营。""",
    "六": """六、世界观设定集
只交代本故事行动所需的时代、地点、生活环境、技术或制度条件及重要物品。
现实题材使用符合生活的称呼，写清工作方式、时间和物品归属即可。幻想或科幻题材按创意补充实际存在的种族、信仰、特殊能力或技术规则。
未使用的设定不展开，不写一串“没有魔法/没有神器”的占位说明。背景详略服务当前篇幅，不编百科全书。""",
    "七": """七、分卷规划
依作者明确的篇幅和卷章安排规划；未指定时根据故事容量决定，一卷短篇也是完整作品。
每卷写清开端、关键行动、因果转折、人物变化与结局兑现。短篇可细到章；长篇避免在总纲重复每个场景。
正文中的承诺应有具体落实过程，必要的铺垫与衔接写清，有意留白保留给作者。不强加新危机、隐藏阴谋或下一卷悬念。
本段末尾输出 <volume-plan> 包围的 JSON 数组，按实际卷数增减元素。下面的一项仅展示字段，不规定卷数或章数：
<volume-plan>
[{"idx": 1, "title": "卷名", "theme": "本卷主题", "core_conflict": "本卷核心问题", "est_chapters": 3}]
</volume-plan>""",
    "八": """八、核心伏笔
列出实际需要铺垫并兑现的承诺、细节或伏笔，说明出现和落实时机。日常故事可写情感和行动的呼应，不必制造谜团。
数量服从剧情；没有独立伏笔时说明如何完成主要承诺即可。""",
    "九": """九、基调与类型标签
简述叙述视角、语言气质和情绪走向，遵循作者指定的文风。允许平实或细腻表达，不靠固定对白、比喻、句长配额制造风格。""",
}
_BOOK_PLANNING_GUIDANCE = """你是中文小说编辑，帮助作者形成可写成小说的方案。直接输出大纲，使用编号标题和自然段。
保持作者指定的题材、故事规模、叙述视角、人物关系和结局。详写影响选择的行动与因果，简写无关背景；不靠重复事实或新增支线凑字数。
大纲是创作依据，不能只写办事清单，也不能用抽象主题代替人物实际经历。表述自然清楚，保留作品应有的语气。
区分不可改动的既定事实与尚待创作的日常细节。为兑现作者指定的事件，可以设计合理的连接动作、收存安排和人物反应；不应以“未指定”为由跳过关键物品去向或承诺结果。补充必须与已有事实相容，不引入改变主线的资源、身份或奇迹。
规则说明集中在世界设定段，骨架和人物段只写它们对行动的具体影响。
"""
BOOK_OUTLINE_SYSTEM = _BOOK_PLANNING_GUIDANCE + "\n\n".join(_BOOK_SECTIONS.values())
BOOK_OUTLINE_SKELETON_SYSTEM = (_BOOK_PLANNING_GUIDANCE
    + "只输出一、三、七、八、九五段，保留原编号，其余段落由后续阶段补全。\n"
    + "\n\n".join(_BOOK_SECTIONS[key] for key in ("一", "三", "七", "八", "九")))
BOOK_OUTLINE_CHARACTERS_SYSTEM = (_BOOK_PLANNING_GUIDANCE
    + "基于已给出的骨架，只输出二、四、五三段，保留原编号。\n"
    + "\n\n".join(_BOOK_SECTIONS[key] for key in ("二", "四", "五")))
BOOK_OUTLINE_WORLD_SYSTEM = (_BOOK_PLANNING_GUIDANCE
    + "基于已给出的骨架，只输出六，保留原编号。\n" + _BOOK_SECTIONS["六"])

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
  "transition_to_next": "有下一卷时写具体交接；全书终卷写明全书完结及收束状态，不新增续卷入口"
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
  "transition_to_next": "有下一卷时写具体交接；全书终卷写明全书完结及收束状态，不新增续卷入口"
}

chapter_count 必须是一个整数，不要输出 chapter_summaries 字段。

【剧情展开要求】
- core_conflict 写清开局处境、人物想做什么、实际困难以及本卷如何解决。冲突可以是日常选择，不必有敌人、误判或胜负。
- turning_points 按实际剧情列出推动行动或改变选择的关键节点，至少一个；数量与篇幅相称，不为凑数增加争执、意外或代价。每点交代触发、行动和结果。
- emotional_arc 写出由具体经历引起的人物变化；可以细微、平缓，不强制多次反转。
- transition_to_next 仅在全书已规划下一卷时交代承接。独立短篇或全书最后一卷须落实既定结局，写明全书完结，不强留悬念、未完成任务或新增卷数。
- volume_start_state、volume_end_state、volume_power_resource_map、volume_information_map、volume_mechanism_limits、volume_result_strength_ladder、foreshadow_progression 均须输出，按全书既定事实简明填写，无相关特殊设定或伏笔时说明没有。字段不设字数配额，不反复扩写同一事实。
- 重要物品的持有、交付、保管和带离须前后相接；可补足与既定事实相容的日常动作，不擅改归属、人物关系或已约定的结局。
- 不允许用“略”“待补充”“继续推进”“逐步展开”“一系列事件”等空泛词代替具体剧情设计。

【命名与质量要求（PR-OL11 + PR-AI1）】
- title 为贴合本卷内容的自然中文卷名，简洁易懂；不强求诗意或规定字数。
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

# Both chapter-outline entry points preserve a process narrative; its depth
# follows the actual chapter budget rather than a fixed synopsis length.
CHAPTER_OUTLINE_SYSTEM = """你是一位高质量中文小说大纲作者。你要生成一份本章「过程性详细大纲」，只输出 JSON 本身，不要 markdown 代码块或辅助说明。

【过程性章节大纲是什么】
summary 应以清楚交代本章过程为准，是「去了修辞、去了环境描写、去了心理渲染，但过程不丢」的陈述句：人物进场顺序、关键对话要点、动作与结果、状态转折、到头状态都要讲清楚。

例如，既定计划若是“修好家谱并交还客户”，应交代谁完成剩余修补、交付前核对什么、客户怎样接走、桌上还留下什么；不要为展示转折另添陌生人、秘密或意外。

细纲会被后续章节检索，应保留足以准确复述事件过程的事实，不以关键词清单代替过程。

【输出 schema（严格遵守）】
{
  "chapter_idx": 章号,
  "title": "本章标题 3–8 字，不同于卷名",
  "summary": "本章过程性叙事。含进入状态·中段关键场景与对话要点·转折·到头状态。去修辞但过程完整。\\n 表示换行。",
  "key_events": ["事件 1", "事件 2", "按实际事件列出，作为 summary 的结构索引（不是 summary 本身）"],
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
  "next_chapter_hook": "有下章时写具体衔接；全书终章写明完结及收束状态，不新造钩子。"
}

【额外要求】
- summary 的详略服从本章实际篇幅，不为凑字数增加剧情。
- summary 中不要出现「如上所述」「总之」「本章讲了 X」这类总结词，直接叙述过程。
- key_events 是 summary 的结构骨架，不能代替 summary。只写 key_events 会被拒收。
- foreshadows / state_changes 可为 [] 但不能缺字段。首章时 prev_chapter_threads 可为 []。
- 保留全书/分卷已定的人物关系、物品归属和剧情边界；可补足相容的日常动作，不新增改变主线的角色、设定或关键道具。
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

    实现在 ``app.services.chapter_target_words.resolve_chapters_per_volume_pref``——
    书纲规模(compute_scale)与卷纲硬约束现在共用同一套读取规则,不再各读各键。

    ``chapters_per_volume_min`` / ``_max`` / ``_target`` 夹到 [1, 50];简写键
    ``chapters_per_volume`` 不夹取(它是每卷章数的真值,如 100),只补缺失的槽位。
    Returns ``None`` when no key carries a usable value — callers must then keep
    the legacy behaviour (plan-driven counts, 10-chapter fallback).
    """
    return _resolve_cpv_pref(settings_json)


def _format_scale_directive(scale: dict) -> str:
    """Render a hard-constraint paragraph from a compute_scale() result."""
    twc = scale["target_word_count"]
    n_ch = scale["n_chapters"]
    n_vol = scale["n_volumes"]
    cpv = scale["chapters_per_volume"]
    cw = scale["chapter_words"]
    return (
        f"本书总字数目标 {twc:,} 字，按项目设定每章约 {cw} 字"
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
    volume_outline = outline_for_generation(volume_outline)
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
    duplicate_indices = set()
    for item in summaries:
        if not isinstance(item, dict):
            continue
        idx = item.get('chapter_idx')
        if type(idx) is not int or not 0 < idx <= 2147483647:
            continue
        if idx in out or idx in duplicate_indices:
            duplicate_indices.add(idx)
            out.pop(idx, None)
            continue
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


_MAX_VOLUME_CHAPTERS = 200


def _clamp_chapter_count(value) -> int:
    """单卷章数上限(2026-11-17):超限按上限处理,非法按 1。

    上限取 200:远超正常单卷规模,但足以挡住"模型给出 5000 章"导致的批量调用爆炸。
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        logger.warning("outline chapter_count unusable: %r; fallback to 1", value)
        return 1
    if count <= 0:
        logger.warning("outline chapter_count non-positive: %s; fallback to 1", count)
        return 1
    if count > _MAX_VOLUME_CHAPTERS:
        logger.warning("outline chapter_count %s exceeds cap %s; clamped",
                       count, _MAX_VOLUME_CHAPTERS)
        return _MAX_VOLUME_CHAPTERS
    return count


_CHAPTER_PROMPT_BOOK_KEYS = ("main_plot", "characters", "world_setting")


def _slim_for_chapter_prompt(
    book_outline: dict, volume_outline: dict, chapter_idx: int, *, window: int = 2
) -> tuple[dict, dict]:
    """章节视角的投影:生成某一章章纲时,不该把整份书纲 + 整卷 100 章都塞进 prompt。

    实测(2026-11-21,百万字项目):未裁剪时单章 prompt ≈ 99,701 字符(约 6.6 万 tokens);
    按 500 章算要烧掉三千多万输入 tokens,长上下文里模型对"本章该写什么"也更糊。

    但裁剪**不能丢事实**:既有契约要求章纲 prompt 保留书纲事实(它们常写在
    ``raw_text`` 里)、保留 ``volume_plan``(判断"是不是全书最后一章"要用)、
    并保留改稿后的新正文。所以:

    * ``raw_text`` **始终保留**(截断到 8000 字),它是事实来源;
    * ``volume_plan`` 精简为 idx/est_chapters 后保留;
    * 结构化字段只取内核/人物/世界观;
    * 卷纲只留卷级设定 + 本章前后各 ``window`` 章;
    * 任何一步拿不准(不是 dict、投影后为空)就**原样返回**,宁可不裁也不丢。
    """
    if not isinstance(book_outline, dict) or not isinstance(volume_outline, dict):
        return book_outline, volume_outline

    book: dict = {}
    for key in ("title", "premise"):
        if book_outline.get(key):
            book[key] = book_outline[key]
    if isinstance(book_outline.get("volume_plan"), list):
        book["volume_plan"] = [
            {k: item[k] for k in ("idx", "est_chapters") if k in item}
            for item in book_outline["volume_plan"] if isinstance(item, dict)
        ]
    structured = book_outline.get("structured")
    if isinstance(structured, dict):
        for key in _CHAPTER_PROMPT_BOOK_KEYS:
            if structured.get(key):
                book[key] = structured[key]
    raw = book_outline.get("raw_text")
    if raw:
        book["raw_text"] = str(raw)[:8000]
    if not book:
        return book_outline, volume_outline

    volume: dict = {}
    nearby: list = []
    for key, value in volume_outline.items():
        if key in ("sections", "raw_text", "batch", "chapters", "chapter_summaries"):
            continue
        if key.startswith("_") or value in (None, "", [], {}):
            continue
        volume[key] = value
    chapters = (volume_outline.get("chapter_summaries")
                or volume_outline.get("batch")
                or volume_outline.get("chapters") or [])
    for item in chapters:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("chapter_idx") or 0)
        except (TypeError, ValueError):
            continue
        if idx and abs(idx - int(chapter_idx)) <= window:
            nearby.append(item)
    if nearby:
        # 键名保持 chapter_summaries,兼容既有消费方(chapter_scope_directive 等)
        volume["chapter_summaries"] = nearby
    if not volume:
        return book_outline, volume_outline
    return book, volume


class OutlineGenerator:
    """Generates hierarchical outlines: book → volume → chapter."""

    def __init__(self, project_id: str | None = None, chapter_naming_directive: str = ""):
        self.router = OutlinePromptRunner(project_id)
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
        """Read the project's per-volume chapter range from settings_json.

        E2E defect (2026-07-26): a project configured for 3-4 chapters/volume
        still got 10-chapter staged volume outlines because this path never
        consulted settings.

        E2E defect (2026-11-21): the same settings were read by two different
        rules — ``compute_scale`` used the raw values (“每卷 100 章” → 5 卷 × 100
        章) while this path applied the legacy [1, 50] clamp and turned it into
        “本卷必须 50 章”, halving the confirmed book plan and doubling every
        chapter's word budget (卷目标字数 ÷ 50 = 4000). Resolved through
        ``chapter_target_words.resolve_volume_chapter_range`` so both stages read
        the setting the same way. Returns None when the project carries no
        explicit setting so legacy behaviour (model/plan-driven counts) is
        preserved. Never raises.
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
            return _resolve_volume_cpv_range(proj_settings)
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

    def _validate_book_stage(self, text: str, stage: str, min_chars: int, scale: dict | None = None) -> None:
        required = {"A": "一三七八九", "B": "二四五", "C": "六",
                    "B2/characters": "二", "B4/relationships": "四",
                    "B5/factions": "五", "C/world": "六"}[stage]
        sections = dict(self._iter_sections(text))
        missing = [num for num in required if not sections.get(num, "").partition("\n")[2].strip()]
        if len(text.strip()) < min_chars or missing:
            raise ValueError(f"全书大纲阶段 {stage} 不完整：长度 {len(text.strip())}/{min_chars}，缺少正文段落 {','.join(missing) or '无'}")
        if stage == "A":
            plan = self._extract_volume_plan(text) or []
            if not plan:
                raise ValueError("全书大纲阶段 A 缺少有效分卷计划")
            # PR-OL10 补:模型偶尔无视 scale 指令只规划 1~2 卷,体积不足的分卷计划
            # 会让第 2 卷起永远生成不了(`_target` 要求卷号 1..N 连续)。这里把
            # 「卷数不足」判定为 A 阶段不合格 → 触发既有重试;两次都不足时由
            # 调用方用 scale 补齐(见 _top_up_volume_plan),不会静默留下残卷。
            expected = int((scale or {}).get("n_volumes") or 0)
            if expected and len(plan) < expected:
                raise ValueError(
                    f"全书大纲阶段 A 分卷计划不足:模型给出 {len(plan)} 卷,项目规模要求 {expected} 卷"
                )
            # 卷号必须是 1..N:下游 volume_creation._target 与 book_outline_save
            # 都硬要求连续。缺号/逾号若留到保存阶段才暴露,三次模型调用已经白烧。
            indices = sorted(int(item.get("idx") or 0) for item in plan)
            if indices != list(range(1, len(plan) + 1)):
                raise ValueError(
                    f"全书大纲阶段 A 分卷计划卷号不连续:实际 {indices},要求 1..{len(plan)}"
                )

    async def _generate_book_outline_staged(self, user_input: str, *, scale: dict | None = None) -> dict:
        """Three-call staged book outline (A skeleton → B/C in parallel).

        Each stage stays below ~4k tokens so output quality stays in the
        safe zone for every model tier. The three stage outputs are
        reassembled into a single 9-section document in canonical order.
        """
        # Stage A — skeleton (一、三、七、八、九)
        # PR-OL10: inject hard volume-count directive into skeleton prompt.
        skeleton_system = self._apply_scale_to_prompt(BOOK_OUTLINE_SKELETON_SYSTEM, scale)
        skeleton_system = skeleton_system + WORLD_LOGIC_CONTRACT + OUTLINE_WRITING_QUALITY_PROMPT
        skeleton_msgs = [
            {"role": "system", "content": skeleton_system},
            {"role": "user", "content": f"创意:\n{user_input}\n\n请依次生成下面五段,一段都不能少,保留编号:一、书名与核心概念;三、主角变化与关键选择;七、分卷规划(末尾必须带 <volume-plan> 分卷 JSON);八、核心伏笔;九、基调与类型标签。"},
        ]
        skeleton_text = ""
        skeleton_last_text = ""
        for attempt in range(1, 3):
            try:
                skeleton_result = await asyncio.wait_for(
                    self.router.generate(
                        task_type="outline_book_skeleton",
                        messages=skeleton_msgs,
                        stream=False,
                        request_timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                    ),
                    timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                )
                skeleton_text = (getattr(skeleton_result, "text", "") or "").strip()
                self._validate_book_stage(
                    skeleton_text, "A", _book_stage_min_chars(scale, "skeleton"), scale
                )
                if skeleton_text:
                    break
                logger.warning("Staged outline: stage A attempt %s returned empty text", attempt)
            except Exception as exc:  # noqa: BLE001
                skeleton_last_text = skeleton_text or skeleton_last_text
                skeleton_text = ""
                logger.warning("Staged outline: stage A attempt %s failed: %s", attempt, exc)
        if not skeleton_text and skeleton_last_text:
            # 两次重试都因分卷计划不足而不合格时,保留模型写好的骨架正文,
            # 用项目规模补齐缺失卷号(确定性兜底),而不是让整份书纲失败。
            try:
                skeleton_text = self._top_up_volume_plan(skeleton_last_text, scale)
                logger.warning("Staged outline: stage A volume plan topped up from project scale")
            except Exception as exc:  # noqa: BLE001
                # 兜底本身失败时,不能把可读的"A 阶段不完整"换成裸 ValueError
                # (实测 est_chapters="约100章" 会在这里冒出 int() 的报错)。
                logger.warning("Staged outline: stage A top-up failed: %s", exc)
                skeleton_text = ""
        if not skeleton_text:
            raise ValueError("全书骨架生成失败或不完整,两次尝试均未通过校验;未生成替代模板")

        shared_context = (
            f"创意：\n{user_input}\n\n已生成的骨架：\n{skeleton_text}\n"
        )
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
                            task_type="outline_book_world" if stage_name == "C/world" else "outline_book_characters",
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
                try:
                    self._validate_book_stage(last_text, stage_name, min_chars)
                    return last_text
                except ValueError:
                    pass
                logger.warning(
                    "Staged outline: stage %s attempt %s returned short text, length=%s",
                    stage_name,
                    attempt,
                    len(last_text),
                )
            raise ValueError(f"全书大纲阶段 {stage_name} 两次尝试后仍不完整；未生成替代模板")

        characters_system = BOOK_OUTLINE_CHARACTERS_SYSTEM + WORLD_LOGIC_CONTRACT + OUTLINE_WRITING_QUALITY_PROMPT
        world_system = BOOK_OUTLINE_WORLD_SYSTEM + OUTLINE_BOOK_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT

        section_two = await _generate_small_stage(
            "B2/characters",
            [
                {"role": "system", "content": characters_system},
                {
                    "role": "user",
                    "content": shared_context
                    + "\n请只生成二、主要角色与小传。必须以“二、主要角色与小传”开头。"
                    + "\n按实际需要写角色，解释当下动机、行为与关系；次要人物简写，不强制创伤、秘密或人物数量。"
                    + "\n篇幅与全书规模相称，写清必要信息即可。",
                },
            ],
            min_chars=min(1000, _book_stage_min_chars(scale, "characters")),
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
                    + "\n写清人物如何影响彼此，哪些行动带来关系和情绪变化；保留合作、照料和坦诚沟通的可能。"
                    + "\n篇幅与全书规模相称，避免重复角色小传。",
                },
            ],
            min_chars=min(500, _book_stage_min_chars(scale, "characters")),
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
                    + "\n只写实际影响剧情的组织或社会关系，没有阵营斗争时简述相关现实条件即可。"
                    + "\n篇幅与全书规模相称，避免重复角色小传。",
                },
            ],
            min_chars=min(500, _book_stage_min_chars(scale, "characters")),
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
            min_chars=_book_stage_min_chars(scale, "world"),
        )
        characters_text = "\n\n".join(
            text for text in (section_two, section_four, section_five) if text
        )

        for stage, text, budget_key in (("A", skeleton_text, "skeleton"),
                                        ("B", characters_text, "characters"), ("C", world_text, "world")):
            self._validate_book_stage(text, stage, _book_stage_min_chars(scale, budget_key))
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
                "skeleton": len(skeleton_text) >= _book_stage_min_chars(scale, "skeleton"),
                "characters": len(characters_text) >= _book_stage_min_chars(scale, "characters"),
                "world": len(world_text) >= _book_stage_min_chars(scale, "world"),
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
        B: 二、四、五; C: 六). Ignore sections emitted by the wrong stage
        so skeleton drift cannot replace the dedicated character/world plan.
        """
        buckets = self._split_book_sections(skeleton_text, characters_text, world_text)
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
        for text, owned in ((skeleton_text, "一三七八九"), (characters_text, "二四五"), (world_text, "六")):
            for num, body in self._iter_sections(text):
                if num in owned:
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
            rf"(?m)^[ \t]*(?:#{{1,6}}[ \t]+)?(?P<num>[{''.join(self._SECTION_NUMS)}])、"
        )
        matches = list(pattern.finditer(text))
        for i, m in enumerate(matches):
            # Normalize only the heading prefix; preserve the full body.
            start = m.start("num")
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            yield m.group("num"), text[start:end]

    def _top_up_volume_plan(self, skeleton_text: str, scale: dict | None) -> str:
        """把骨架正文里的分卷计划补齐到项目规模要求的卷数。

        模型偶尔无视 scale 指令只规划少量卷,而缺卷会让第 2 卷起的卷纲永远
        生成不了(``volume_creation._target`` 要求卷号 1..N 连续)。这里保留模型
        已给出的卷,只为缺失卷号追加显式占位(序号 + 章数),按既有
        ``<volume-plan>`` 格式追加;该标签在落库前由 ``_strip_volume_plan_tags``
        从用户可见正文里剥掉,不会泄漏给读者。
        """
        if not scale or not isinstance(scale, dict):
            return skeleton_text
        expected = int(scale.get("n_volumes") or 0)
        chapters_per_volume = int(scale.get("chapters_per_volume") or 0)
        if expected <= 0:
            return skeleton_text
        existing = self._extract_volume_plan(skeleton_text) or []
        have = {
            int(item.get("idx") or 0)
            for item in existing
            if isinstance(item, dict)
        }
        if not existing:
            # 一个卷都没有的情况由 stage A 校验直接判不合格,不在这里凭空造卷。
            return skeleton_text
        missing = [idx for idx in range(1, expected + 1) if idx not in have]
        if not missing:
            return skeleton_text
        filler = [
            {
                "idx": idx,
                "title": f"第{idx}卷",
                "theme": "",
                "core_conflict": "",
                "est_chapters": chapters_per_volume or 100,
            }
            for idx in missing
        ]
        appended = json.dumps(filler, ensure_ascii=False)
        return f"{skeleton_text}\n\n<volume-plan>{appended}</volume-plan>"

    # ------------------------------------------------------------------
    # PR-OL1 — extract structured volume plan from outline text
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_volume_plan(text: str) -> list[dict] | None:
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
        # 2026-11-21 修复:此前这里是 `for raw_body in reversed(matches)`,即只取**最后**
        # 一个能解析的 <volume-plan> 块。实测 v4-flash 会把分卷计划分多块输出(每卷一块),
        # 于是只剩最后一个块 → book_outline["volume_plan"] 只有 1 条(如只有 idx=5)。
        # 后果是连锁的:生成第 2 卷时 snapshot_creation 校验"全书分卷计划卷号不完整"
        # 直接拒绝,卷纲永远无法为第 2 卷及以后生成。
        # 改为**合并所有块**(按 idx 去重、先到先得、按卷号排序),块级容错不变。
        merged_volumes: dict[int, dict] = {}
        for raw_body in matches:
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
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("idx"))
                except (TypeError, ValueError):
                    idx = 0
                if idx <= 0:
                    # 无 idx / 非数字:按已合并条数顺延。此前每块都从 enumerate 的 1
                    # 起算,模型"每卷一整块、块内不带 idx"时所有块都挤到 idx=1,
                    # 合并后只剩 1 条 —— 正是本次要修的 v4-flash 输出形状。
                    idx = len(merged_volumes) + 1
                entry = merged_volumes.setdefault(idx, {"idx": idx})
                # 块之间字段互补时逐缺补全(先前 setdefault 先到先得,后续块里
                # 更完整的 est_chapters / title 会被整条丢掉)。
                for key in ("title", "theme", "core_conflict"):
                    if not str(entry.get(key) or "").strip() and str(item.get(key) or "").strip():
                        entry[key] = str(item[key]).strip()
                try:
                    est = int(item.get("est_chapters"))
                except (TypeError, ValueError):
                    # 模型偶尔写 "约100章" 这类非数字;以前 int() 直接在解析层抛,
                    # 冒出去会盖掉"A 阶段不完整"这种可读错误。
                    est = 0
                if est > 0 and not entry.get("est_chapters"):
                    entry["est_chapters"] = est
        for idx, entry in merged_volumes.items():
            entry.setdefault("title", f"第{idx}卷")
            entry.setdefault("theme", "")
            entry.setdefault("core_conflict", "")
            entry.setdefault("est_chapters", 10)
        if merged_volumes:
            return [merged_volumes[key] for key in sorted(merged_volumes)]
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
        skeleton_system = skeleton_system + WORLD_LOGIC_CONTRACT + OUTLINE_WRITING_QUALITY_PROMPT
        skeleton_msgs = [
            {"role": "system", "content": skeleton_system},
            {
                "role": "user",
                "content": f"创意:\n{user_input}\n\n请依次生成下面五段,一段都不能少,保留编号:一、书名与核心概念;三、主角变化与关键选择;七、分卷规划(末尾必须带 <volume-plan> 分卷 JSON);八、核心伏笔;九、基调与类型标签。",
            },
        ]
        try:
            async with aclosing(self.router.generate_stream(task_type='outline_book_skeleton', messages=skeleton_msgs, _log_meta=self._log_meta('outline_book_skeleton'))) as owned_stream:
                async for delta in owned_stream:
                    if not delta:
                        continue
                    a_buf.append(delta)
                    yield {"event": "stage_chunk", "stage": "A", "delta": delta}
        except Exception as exc:  # noqa: BLE001 — surface as structured event
            logger.warning("Staged stream: stage A failed: %s", exc)
            yield {"event": "error", "stage": "A", "message": str(exc)}
            raise ValueError("全书骨架流式生成失败，未保存不完整大纲") from exc

        skeleton_text = "".join(a_buf).strip()
        try:
            self._validate_book_stage(
                skeleton_text, "A", _book_stage_min_chars(scale, "skeleton"), scale
            )
        except ValueError as _stage_a_exc:
            # 流式路径没有重试循环。这里**只**为「卷数不足」补齐卷号;
            # 结构缺失(如漏写七、八、九)必须照旧失败 —— 用补齐去掩盖只会让
            # 残缺大纲以「已保存」的面目流到下游。
            if "分卷计划不足" not in str(_stage_a_exc):
                raise
            _topped = self._top_up_volume_plan(skeleton_text, scale)
            if not _topped or not self._extract_volume_plan(_topped):
                raise
            logger.warning("Staged stream: stage A volume plan topped up (%s)", _stage_a_exc)
            skeleton_text = _topped
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
                        "content": BOOK_OUTLINE_CHARACTERS_SYSTEM + WORLD_LOGIC_CONTRACT + OUTLINE_WRITING_QUALITY_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": shared_context
                        + "\n请生成二、四、五三段。必须包含：二、主要角色与小传；四、主要矛盾与关系网络；五、势力格局。"
                        + "\n各段说明实际人物及其关系对剧情的作用，篇幅与故事规模相称；不为扩写新增人物背景或重复同一事实。",
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
        failed_stages: set[str] = set()

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
                async with aclosing(self.router.generate_stream(task_type='outline_book_characters' if code == 'B' else 'outline_book_world', messages=stage['msgs'], _log_meta=self._log_meta(f'outline_book_stage_{code}'))) as owned_stream:
                    async for delta in owned_stream:
                        if not delta:
                            continue
                        buffers[code].append(delta)
                        await queue.put(
                            {"event": "stage_chunk", "stage": code, "delta": delta}
                        )
                current_text = "".join(buffers[code]).strip()
                min_chars = (
                    _book_stage_min_chars(scale, "characters")
                    if code == "B"
                    else _book_stage_min_chars(scale, "world")
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
                            task_type="outline_book_characters" if code == "B" else "outline_book_world",
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
                failed_stages.add(code)
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

        if failed_stages:
            raise ValueError(f"全书大纲阶段 {', '.join(sorted(failed_stages))} 失败，未保存不完整结果")
        characters_text = "".join(buffers["B"]).strip()
        world_text = "".join(buffers["C"]).strip()
        # PR-OUTLINE-STAGED-PERSIST-STRUCT: split first, then reassemble +
        # build structured payload so persist_outline_now can promote
        # main_plot / characters / world_setting / sections to top-level.
        _book_buckets = self._split_book_sections(
            skeleton_text, characters_text, world_text
        )
        for stage, text, budget_key in (("A", skeleton_text, "skeleton"),
                                        ("B", characters_text, "characters"), ("C", world_text, "world")):
            self._validate_book_stage(text, stage, _book_stage_min_chars(scale, budget_key))
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
                "skeleton": len(skeleton_text) >= _book_stage_min_chars(scale, "skeleton"),
                "characters": len(characters_text) >= _book_stage_min_chars(scale, "characters"),
                "world": len(world_text) >= _book_stage_min_chars(scale, "world"),
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
        book_outline = outline_for_generation(book_outline)
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

        if staged:
            return await self._generate_volume_outline_staged(
                book_outline=book_outline, volume_idx=volume_idx, user_notes=user_notes,
            )

        from app.services.outline_volume_single import generate_single_volume
        chapter_range = await self._load_chapters_per_volume_pref()
        messages[1]["content"] += "\n卷纲按实际篇幅包含有效剧情节点，不凑转折数量；每章至少两个有效 key_events，章号从1连续排列，条目总数等于 chapter_count。"
        if chapter_range is not None:
            messages[1]["content"] += f"\n项目章数硬约束：chapter_count 必须在 {chapter_range[0]}-{chapter_range[1]} 之间，优先于其他估算。"
        kwargs = dict(router=self.router, messages=messages, parse_json=self._parse_json,
                      validate_batch=self._validate_volume_batch, volume_idx=volume_idx,
                      chapter_range=chapter_range, timeout=OUTLINE_LLM_CALL_TIMEOUT_SECONDS,
                      stream=stream)
        if stream:
            async def validated_stream():
                result = await generate_single_volume(**kwargs)
                if result.get("_parse_error"):
                    raise ValueError(result.get("raw_text") or "卷纲生成失败")
                # Buffer until validation, like the staged streaming wrapper:
                # partial malformed JSON must not become a saveable outline.
                yield json.dumps(result, ensure_ascii=False)
            return validated_stream()
        return await generate_single_volume(**kwargs)

    @staticmethod
    def _validate_volume_batch(items, start: int, end: int) -> list[dict]:
        if not isinstance(items, list) or len(items) != end - start + 1:
            raise ValueError(f"batch 必须准确包含 {end - start + 1} 章")
        for expected, item in zip(range(start, end + 1), items):
            if not isinstance(item, dict):
                raise ValueError(f"第 {expected} 章必须为对象")
            if type(item.get("chapter_idx")) is not int or item["chapter_idx"] != expected:
                raise ValueError(f"chapter_idx 必须按原顺序连续排列，第 {expected} 章章号错误")
            for field in ("title", "summary"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ValueError(f"第 {expected} 章缺少有效 {field}")
            events = item.get("key_events")
            if not isinstance(events, list) or len(events) < 2 or any(
                not isinstance(event, str) or not event.strip() for event in events
            ):
                raise ValueError(f"第 {expected} 章至少需要两个有效 key_events")
        return items

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
        if result.get("_parse_error") or result.get("parse_error"):
            raise ValueError(result.get("raw_text") or "分卷大纲生成失败，未保存不完整结果")
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
        # The project's chapter range is both a prompt instruction and a
        # validated output constraint; invalid meta never reaches batching.
        book_outline = outline_for_generation(book_outline)
        cpv_pref = await self._load_chapters_per_volume_pref()
        cpv_directive = ""
        if cpv_pref is not None:
            cpv_lo, cpv_hi = cpv_pref
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

        from app.services.outline_contract import validate_volume_meta
        meta_messages = [
            {"role": "system", "content": VOLUME_META_SYSTEM + OUTLINE_VOLUME_CONTRACT_PROMPT + OUTLINE_WRITING_QUALITY_PROMPT + (("\n\n" + self.chapter_naming_directive) if self.chapter_naming_directive else "")},
            {"role": "user", "content": meta_ctx},
        ]
        meta = None
        for attempt in range(2):
            meta_result = None
            try:
                meta_result = await asyncio.wait_for(
                    self.router.generate(task_type="outline_volume_meta", messages=meta_messages, max_tokens=4096),
                    timeout=OUTLINE_LLM_CALL_TIMEOUT_SECONDS,
                )
                meta = self._parse_json(meta_result.text)
                validate_volume_meta(meta, volume_idx, cpv_pref)
                break
            except ValueError as exc:
                if attempt == 1 or meta_result is None:
                    return {"_parse_error": True, "raw_text": str(exc), "meta": meta, "chapter_summaries": []}
                # Preserve recovered on-genre content as repair input, never
                # substitute a generic local plot or inferred chapter count.
                meta_messages = meta_messages + [
                    {"role": "assistant", "content": meta_result.text},
                    {"role": "user", "content": f"元信息校验失败：{exc}。保留已有有效剧情与项目约束，补齐并返回完整元信息 JSON，不要生成章节数组。"},
                ]
            except Exception as exc:
                logger.warning("Staged volume outline: meta call failed: %r", exc)
                return {"_parse_error": True, "raw_text": str(exc), "chapter_summaries": []}
        # 2026-11-17:模型给的章数此前只校验 >0。一个 chapter_count=5000 的响应会让
        # 下面按 5 章一批串行发起 1000 次付费调用(每次 240s 超时),而"章数不符"
        # 要到后面才判错 —— 钱和时间先被烧光。这里按单卷上限直接夹住。
        chapter_count = _clamp_chapter_count(meta.get("chapter_count"))
        meta["chapter_count"] = chapter_count

        # Stage V2 — batched chapter summaries.
        # Five chapters per model call is slower than ten, but much more
        # resilient for 150-chapter volumes: failed batches are cheaper to
        # retry and the model is less likely to return empty JSON.
        BATCH = 5
        batches = math.ceil(chapter_count / BATCH)
        meta_for_ctx = {
            k: v for k, v in meta.items() if k != "chapter_summaries"
        }
        # Volume metadata is already bounded by the planning call. Preserve
        # every returned contract/turn/payoff instead of taking six fields and
        # slicing the rest away. Chapter summaries are excluded above.
        volume_contract = json.dumps(meta_for_ctx, ensure_ascii=False, separators=(",", ":"))
        book_premise_anchor = json.dumps(book_outline, ensure_ascii=False, separators=(",", ":"))
        all_summaries: list[dict] = []

        for b in range(batches):
            start = b * BATCH + 1
            end = min((b + 1) * BATCH, chapter_count)
            items: list | None = None
            batch_error = ""
            for attempt in range(1, 4):
                logger.info(
                    "Staged volume outline: batch %d/%d chapters %d-%d attempt %d start",
                    b + 1,
                    batches,
                    start,
                    end,
                    attempt,
                )
                tail_str = json.dumps(all_summaries[-2:], ensure_ascii=False, separators=(",", ":"))
                # Older chapter events remain a planning ledger: they are not
                # canonical story facts until the chapters are actually written.
                earlier_plan = [{"chapter_idx": item["chapter_idx"], "title": item["title"],
                                 "key_events": item["key_events"]} for item in all_summaries[:-2]]
                earlier_plan_str = json.dumps(earlier_plan, ensure_ascii=False, separators=(",", ":"))
                retry_note = "" if attempt == 1 else f"\n上一次返回未通过校验：{batch_error}。请修复本批全部章节，保留剧情细节。仅返回 JSON，batch 数量、顺序和字段必须准确。"
                batch_ctx = (
                    f"全书前提（必须严格遵守，章节必须发生在这个世界观/题材/人物范围内）：\n{book_premise_anchor}\n\n"
                    f"本卷完整规划合同（必须承接；不得遗漏转折、状态边界与伏笔推进）：\n{volume_contract}\n\n"
                    f"本卷较早章节规划记录（不是已发生事实；不得重复安排同一事件）：\n{earlier_plan_str}\n\n"
                    f"最近两章完整规划（接住未完动作、信息变化和章末状态）：\n{tail_str}\n\n"
                    f"start={start}, end={end}, count={end - start + 1}。"
                    f"请生成第 {start} 章到第 {end} 章的章节骨架。"
                    f"chapter_idx 必须从 {start} 连续到 {end}。"
                    f"\n必须返回：{{\"batch\": [{{...}}]}}，batch 数组长度必须是 {end - start + 1}。"
                    f"{retry_note}"
                )
                try:
                    batch_result = await asyncio.wait_for(
                        self.router.generate(
                            task_type="outline_volume_batch",
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
                if isinstance(parsed, dict) and not parsed.get("_parse_error") and not parsed.get("parse_error"):
                    raw_items = (
                        parsed.get("batch")
                        or parsed.get("chapter_summaries")
                        or parsed.get("chapters")
                    )
                elif isinstance(parsed, list):
                    raw_items = parsed
                else:
                    raw_items = None
                try:
                    items = self._validate_volume_batch(raw_items, start, end)
                    break
                except ValueError as exc:
                    batch_error = str(exc)
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

            # Preserve validated identity and complete entries; never silently
            # renumber, truncate or skip malformed model chapters.
            all_summaries.extend(items)

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
        target_word_count: int | None = None,
    ) -> dict | AsyncIterator[str]:
        """Generate a chapter-level outline from the volume outline."""
        book_outline = outline_for_generation(book_outline)
        volume_outline = outline_for_generation(volume_outline)
        # 章节视角裁剪:整份书纲 + 整卷 100 章塞进 prompt 会把单章输入顶到约 6.6 万 tokens
        # (2026-11-21 实测);裁剪后只留内核/人物/世界观 + 卷级设定 + 本章前后各 2 章。
        book_slim, volume_slim = _slim_for_chapter_prompt(
            book_outline, volume_outline, chapter_idx)
        context = (
            f"全书大纲:\n{json.dumps(book_slim, ensure_ascii=False, indent=2)}\n\n"
            f"本卷大纲:\n{json.dumps(volume_slim, ensure_ascii=False, indent=2)}\n\n"
            f"请生成第 {chapter_idx} 章的详细大纲。"
        )
        if previous_chapter_summary:
            context += f"\n\n上一章摘要：{previous_chapter_summary}"
        if user_notes:
            context += f"\n\n用户补充说明：{user_notes}"

        from app.services.outline_contract import chapter_scope_directive
        context += "\n\n" + chapter_scope_directive(book_slim, volume_slim, chapter_idx, target_word_count)
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
        book_outline = outline_for_generation(book_outline)
        if not isinstance(book_outline, dict):
            return str(book_outline or "")
        if book_outline.get("raw_text") and not any(book_outline.get(key) for key in ("core_concept", "core_idea", "main_plot", "premise", "logline", "themes")):
            return str(book_outline["raw_text"])
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
