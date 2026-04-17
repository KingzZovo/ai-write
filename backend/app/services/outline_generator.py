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

BOOK_OUTLINE_SYSTEM = """你是一位经验丰富的小说策划师，同时也是一位文笔极佳的作者。你要生成一份完整的全书大纲。

【大纲要求】
用自然、流畅的叙述语言写大纲，不要用干巴巴的列表或模板。大纲本身就应该是一篇好看的文字。

必须包含以下内容（用你自己的方式组织，不要机械列举）：
1. 书名建议（2-3个）
2. 故事核心概念——用场景化的方式呈现，不要抽象概括
3. 主要角色（每人写出性格矛盾、目标、成长弧线、代表性对白）
4. 世界观设定（融入叙述，不要百科式罗列）
5. 分卷规划（每卷的核心冲突和关键转折）
6. 核心伏笔布局

【文字要求——极其重要】
- 像一个真正的小说作者在写策划案，而不是AI在填模板
- 禁止使用以下句式和词汇：
  · "值得注意的是""需要强调的是""不可忽视""毫无疑问""不言而喻"
  · "璀璨""瑰丽""油然而生""心潮澎湃""熠熠生辉""彰显""诠释"
  · "不禁""仿佛""宛如""犹如""缓缓""深深地""静静地"
  · 四字成语连续使用（如"波澜壮阔、气势恢宏、跌宕起伏"）
- 用具体的、有画面感的语言代替抽象概括
- 角色介绍要像在讲一个人的故事，不要像在填人物卡
- 句式长短交替，避免全部用相同结构的排比句
- 写出来的东西要让人觉得"这是一个真人写的"，而不是"这是AI生成的"

【严禁】
- 不要输出JSON格式
- 不要用"首先/其次/最后"的递进结构
- 不要在每段开头用相同的句式"""

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
                max_tokens=163840,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
            max_tokens=16384,
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
                max_tokens=163840,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
            max_tokens=16384,
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
                max_tokens=163840,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
            max_tokens=16384,
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
