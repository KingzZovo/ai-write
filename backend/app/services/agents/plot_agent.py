"""
Plot Agent (推演 Agent)

Generates plot-coherent first drafts without specific style.
Focuses on:
- Story continuity and logical progression
- Character consistency
- Outline adherence
- Natural plot development
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

PLOT_AGENT_SYSTEM = """你是一位专业的小说内容生成引擎。你的任务是根据提供的设定、大纲和上下文，生成剧情连贯、逻辑自洽的小说正文。

写作原则：
1. 严格遵循大纲中的剧情要点，确保每个要点都被覆盖
2. 角色的言行必须符合其人设和当前状态
3. 与前文自然衔接，保持叙事流畅
4. 情节推进要有节奏感，避免平铺直叙
5. 对话要符合角色性格，有区分度
6. 场景描写要适度，服务于剧情
7. 在合适的地方自然地埋入伏笔或呼应前文伏笔

输出要求：
- 直接输出小说正文，不要包含任何元信息、标题或章节编号
- 不要输出"以上是..."等总结性文字
- 保持自然的段落划分"""


class PlotAgent:
    """Generates plot-coherent chapter content."""

    def __init__(self):
        self.router = get_model_router()

    async def generate(
        self,
        context_messages: list[dict],
        max_tokens: int = 4096,
    ) -> str:
        """
        Generate a chapter draft.

        Args:
            context_messages: Pre-assembled context from ContextAssembler
            max_tokens: Maximum tokens for output

        Returns:
            Generated chapter text
        """
        # Inject plot agent's system instructions (from registry or fallback)
        system = await self._load_system_prompt()
        messages = self._inject_system(context_messages, system)

        result = await self.router.generate(
            task_type="generation",
            messages=messages,
            max_tokens=max_tokens,
        )
        return result.text

    async def generate_stream(
        self,
        context_messages: list[dict],
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Generate chapter draft with streaming output."""
        system = await self._load_system_prompt()
        messages = self._inject_system(context_messages, system)

        async for chunk in self.router.generate_stream(
            task_type="generation",
            messages=messages,
            max_tokens=max_tokens,
        ):
            yield chunk

    async def _load_system_prompt(self) -> str:
        """Load system prompt from registry with fallback."""
        from app.services.prompt_loader import load_prompt
        return await load_prompt("generation", fallback=PLOT_AGENT_SYSTEM)

    def _inject_system(self, messages: list[dict], system_override: str = "") -> list[dict]:
        """Prepend plot agent system prompt to existing context."""
        system = system_override or PLOT_AGENT_SYSTEM
        result = []
        for msg in messages:
            if msg["role"] == "system":
                result.append({
                    "role": "system",
                    "content": system + "\n\n" + msg["content"],
                })
            else:
                result.append(msg)

        if not any(m["role"] == "system" for m in result):
            result.insert(0, {"role": "system", "content": system})

        return result
