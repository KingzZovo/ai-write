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

BOOK_OUTLINE_SYSTEM = """你是一位经验丰富的小说策划师。根据用户提供的创意和设定，生成一份完整的全书大纲。

要求输出 JSON 格式，包含以下字段：
{
  "title": "小说标题",
  "genre": "题材类型",
  "total_volumes": 预估总卷数(整数),
  "total_chapters_estimate": 预估总章数(整数),
  "total_words_estimate": 预估总字数(整数),
  "premise": "一句话概述核心剧情",
  "main_plot": "主线剧情脉络（300-500字详述起承转合）",
  "core_characters": [
    {
      "name": "角色名",
      "identity": "身份",
      "personality": "性格特征",
      "role_in_story": "在故事中的作用",
      "arc": "角色成长弧线"
    }
  ],
  "world_building": {
    "setting": "世界观背景",
    "power_system": "力量体系（如有）",
    "key_locations": ["重要地点"],
    "key_rules": ["核心规则/禁忌"]
  },
  "major_foreshadows": [
    {
      "description": "伏笔描述",
      "type": "major/minor",
      "resolve_conditions": ["消解条件"]
    }
  ],
  "volume_summaries": [
    {
      "volume_idx": 1,
      "title": "卷名",
      "summary": "本卷概要（50-100字）",
      "key_events": ["关键事件"]
    }
  ]
}

注意：
- 剧情要有起伏，避免平铺直叙
- 伏笔要自然合理，留有消解空间
- 角色关系要有张力和变化
- 输出纯 JSON，不要包含 markdown 代码块标记"""

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
        messages = [
            {"role": "system", "content": BOOK_OUTLINE_SYSTEM},
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
