"""
Hierarchical Outline Generator

Generates outlines at three levels:
1. Book-level: Overall plot arc, core characters, world-building, estimated scale
2. Volume-level: Per-volume conflicts, turning points, new/departing characters
3. Chapter-level: Per-chapter plot points, characters, emotional arc, transitions
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

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
每卷写出核心冲突、关键转折、主角状态变化。

八、核心伏笔
3-5条贯穿全书的伏笔线，标明埋设时机和预期消解条件。

九、基调与类型标签
用 3-5 个短标签表达整体叙事基调（如"冷峻悬疑""贵族权谋""灾异意象"），再用一句话说明要避开哪些常见套路。

文字风格：句子长短不一，段落开头各不相同，像一个老编辑在给新人讲故事方案，口语化但专业。"""

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
      "summary": "本章概要（30-50字）",
      "key_events": ["事件"]
    }
  ],
  "transition_to_next": "与下一卷的衔接"
}

输出纯 JSON，不要包含 markdown 代码块标记"""

CHAPTER_OUTLINE_SYSTEM = """你是一位经验丰富的小说策划师。根据卷大纲和指定的章号，生成该章的详细大纲。

要求输出 JSON 格式：
{
  "chapter_idx": 章号,
  "title": "章名",
  "plot_points": [
    "剧情要点1（具体描述本章要发生什么）",
    "剧情要点2",
    "剧情要点3"
  ],
  "characters_present": ["出场角色"],
  "locations": ["场景地点"],
  "emotional_curve": {
    "opening": "开头情绪基调",
    "development": "发展过程情绪变化",
    "ending": "结尾情绪基调"
  },
  "word_count_target": 目标字数(整数),
  "foreshadow_notes": "本章伏笔相关提示（如有）",
  "transition_from_previous": "与上一章的衔接",
  "transition_to_next": "与下一章的衔接"
}

输出纯 JSON，不要包含 markdown 代码块标记"""


class OutlineGenerator:
    """Generates hierarchical outlines: book → volume → chapter."""

    def __init__(self):
        self.router = get_model_router()

    async def generate_book_outline(
        self,
        user_input: str,
        stream: bool = False,
    ) -> dict | AsyncIterator[str]:
        """Generate a book-level outline from user's creative input."""
        from app.services.prompt_loader import load_prompt
        system = await load_prompt("outline_book", fallback=BOOK_OUTLINE_SYSTEM)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"请根据以下创意生成全书大纲：\n\n{user_input}"},
        ]

        if stream:
            return self.router.generate_stream(
                task_type="outline",
                messages=messages,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
        )
        return self._parse_json(result.text)

    async def generate_volume_outline(
        self,
        book_outline: dict,
        volume_idx: int,
        user_notes: str = "",
        stream: bool = False,
    ) -> dict | AsyncIterator[str]:
        """Generate a volume-level outline from the book outline."""
        context = (
            f"全书大纲：\n{json.dumps(book_outline, ensure_ascii=False, indent=2)}\n\n"
            f"请生成第 {volume_idx} 卷的详细大纲。"
        )
        if user_notes:
            context += f"\n\n用户补充说明：{user_notes}"

        messages = [
            {"role": "system", "content": VOLUME_OUTLINE_SYSTEM},
            {"role": "user", "content": context},
        ]

        if stream:
            return self.router.generate_stream(
                task_type="outline",
                messages=messages,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
        )
        return self._parse_json(result.text)

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

        messages = [
            {"role": "system", "content": CHAPTER_OUTLINE_SYSTEM},
            {"role": "user", "content": context},
        ]

        if stream:
            return self.router.generate_stream(
                task_type="outline",
                messages=messages,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
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
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Failed to parse outline JSON, returning raw text")
            return {"raw_text": text, "_parse_error": True}
