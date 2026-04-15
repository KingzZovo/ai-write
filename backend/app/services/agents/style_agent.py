"""
Style Agent (润色 Agent)

Applies stylistic transformation to raw chapter drafts.
Phase 1: Manual style description only.
Phase 2: Will support StyleProfile configs + few-shot samples from style library.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

STYLE_AGENT_SYSTEM = """你是一位专业的文学润色编辑。你的任务是对初稿进行风格润色，在保持剧情和逻辑不变的前提下，提升文学表现力。

润色原则：
1. 绝对不改变原文的剧情内容和逻辑
2. 绝对不增加或删除情节
3. 可以调整句式结构（长短句搭配）
4. 可以替换用词（更精准、更有表现力的词汇）
5. 可以调整段落节奏
6. 可以增强场景描写的感染力
7. 可以优化对话的自然度

输出要求：
- 直接输出润色后的正文
- 不要输出对比说明或修改标注
- 保持原文的段落结构"""


class StyleAgent:
    """Applies stylistic polishing to draft content."""

    def __init__(self):
        self.router = get_model_router()

    async def polish(
        self,
        draft_text: str,
        style_instruction: str = "",
        max_tokens: int = 4096,
    ) -> str:
        """
        Polish a draft with style transformation.

        Args:
            draft_text: Raw chapter draft from PlotAgent
            style_instruction: Style guidance (manual description in Phase 1)
            max_tokens: Maximum tokens for output

        Returns:
            Polished chapter text
        """
        messages = self._build_messages(draft_text, style_instruction)

        result = await self.router.generate(
            task_type="polishing",
            messages=messages,
            max_tokens=max_tokens,
        )
        return result.text

    async def polish_stream(
        self,
        draft_text: str,
        style_instruction: str = "",
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Polish with streaming output."""
        messages = self._build_messages(draft_text, style_instruction)

        async for chunk in self.router.generate_stream(
            task_type="polishing",
            messages=messages,
            max_tokens=max_tokens,
        ):
            yield chunk

    def _build_messages(self, draft_text: str, style_instruction: str) -> list[dict]:
        system = STYLE_AGENT_SYSTEM
        if style_instruction:
            system += f"\n\n【目标风格】\n{style_instruction}"

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"请对以下初稿进行风格润色：\n\n{draft_text}"},
        ]
