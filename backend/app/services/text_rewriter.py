"""
Text Rewriter Service

Handles inline text operations triggered by selecting text in the editor:
- Condense (缩写): Make text more concise
- Expand (扩写): Add more detail and description
- Restructure (重构): Keep meaning, change expression
- Continue (续写): Continue from selected point
- Custom: User-provided rewrite instruction
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

REWRITE_SYSTEM = """\u4f60\u662f\u4e00\u4f4d\u4e13\u4e1a\u7684\u5c0f\u8bf4\u6587\u672c\u7f16\u8f91\u3002\u8bf7\u6839\u636e\u6307\u4ee4\u5bf9\u6240\u9009\u6587\u672c\u8fdb\u884c\u6539\u5199\u3002

\u89c4\u5219\uff1a
1. \u4fdd\u6301\u539f\u6587\u7684\u53d9\u4e8b\u89c6\u89d2\u548c\u4eba\u79f0
2. \u4fdd\u6301\u89d2\u8272\u540d\u79f0\u548c\u5173\u952e\u60c5\u8282\u4e0d\u53d8
3. \u76f4\u63a5\u8f93\u51fa\u6539\u5199\u7ed3\u679c\uff0c\u4e0d\u8981\u89e3\u91ca"""


OPERATIONS = {
    "condense": "\u8bf7\u5c06\u4ee5\u4e0b\u6587\u672c\u7f29\u5199\uff0c\u538b\u7f29\u4e3a\u66f4\u7b80\u6d01\u7684\u8868\u8fbe\uff0c\u4fdd\u7559\u6838\u5fc3\u4fe1\u606f\uff1a\n\n",
    "expand": "\u8bf7\u5c06\u4ee5\u4e0b\u6587\u672c\u6269\u5199\uff0c\u589e\u52a0\u7ec6\u8282\u63cf\u5199\u3001\u5fc3\u7406\u6d3b\u52a8\u6216\u573a\u666f\u6c1b\u56f4\uff1a\n\n",
    "restructure": "\u8bf7\u5c06\u4ee5\u4e0b\u6587\u672c\u91cd\u6784\uff0c\u4fdd\u7559\u8bed\u4e49\u4f46\u6539\u53d8\u8868\u8fbe\u65b9\u5f0f\uff08\u53e5\u5f0f\u3001\u7528\u8bcd\u3001\u7ed3\u6784\uff09\uff1a\n\n",
    "continue": "\u8bf7\u4ece\u4ee5\u4e0b\u6587\u672c\u7684\u672b\u5c3e\u7ee7\u7eed\u5199\u4f5c\uff0c\u4fdd\u6301\u98ce\u683c\u548c\u8282\u594f\u4e00\u81f4\uff1a\n\n",
}


class TextRewriter:
    """Handles inline text rewrite operations."""

    def __init__(self):
        self.router = get_model_router()

    async def rewrite(
        self,
        selected_text: str,
        operation: str,
        custom_instruction: str = "",
        context_before: str = "",
        context_after: str = "",
        max_tokens: int = 2048,
    ) -> str:
        """
        Rewrite selected text with the given operation.

        Args:
            selected_text: The text selected by the user
            operation: One of: condense, expand, restructure, continue, custom
            custom_instruction: For 'custom' operation
            context_before: Text before the selection (for context)
            context_after: Text after the selection (for context)
            max_tokens: Max output tokens

        Returns:
            Rewritten text
        """
        prompt = self._build_prompt(
            selected_text, operation, custom_instruction,
            context_before, context_after,
        )

        result = await self.router.generate(
            task_type="polishing",
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
        )
        return result.text

    async def rewrite_stream(
        self,
        selected_text: str,
        operation: str,
        custom_instruction: str = "",
        context_before: str = "",
        context_after: str = "",
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Rewrite with streaming output."""
        prompt = self._build_prompt(
            selected_text, operation, custom_instruction,
            context_before, context_after,
        )

        async for chunk in self.router.generate_stream(
            task_type="polishing",
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
        ):
            yield chunk

    def _build_prompt(
        self,
        selected_text: str,
        operation: str,
        custom_instruction: str,
        context_before: str,
        context_after: str,
    ) -> str:
        parts: list[str] = []

        if context_before:
            parts.append(f"[\u524d\u6587]\n{context_before[-500:]}\n")

        if operation == "custom" and custom_instruction:
            parts.append(f"\u8bf7\u6309\u4ee5\u4e0b\u6307\u4ee4\u6539\u5199\u6587\u672c\uff1a{custom_instruction}\n\n{selected_text}")
        elif operation in OPERATIONS:
            parts.append(f"{OPERATIONS[operation]}{selected_text}")
        else:
            parts.append(f"\u8bf7\u6539\u5199\u4ee5\u4e0b\u6587\u672c\uff1a\n\n{selected_text}")

        if context_after:
            parts.append(f"\n[\u540e\u6587]\n{context_after[:500]}")

        return "\n".join(parts)
