"""
Style Agent (润色 Agent)

Applies stylistic transformation to raw chapter drafts.
Phase 1: Manual style description only.
Phase 2: Supports StyleProfile configs + few-shot samples from style library.
"""

from __future__ import annotations

import json
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


def _build_style_instruction_from_profile(style_profile: dict) -> str:
    """Build a detailed style instruction string from a StyleProfile config dict."""
    parts: list[str] = []

    # Vocabulary guidance
    whitelist = style_profile.get("vocab_whitelist", [])
    if whitelist:
        parts.append(f"【偏好用词】请优先使用以下词汇：{', '.join(whitelist[:20])}")

    blacklist = style_profile.get("vocab_blacklist", [])
    if blacklist:
        parts.append(f"【避免用词】请避免使用以下词汇：{', '.join(blacklist[:20])}")

    # Sentence length ratios
    sentence_ratio = style_profile.get("sentence_ratio", {})
    if sentence_ratio:
        short_r = sentence_ratio.get("short", 0)
        medium_r = sentence_ratio.get("medium", 0)
        long_r = sentence_ratio.get("long", 0)
        parts.append(
            f"【句式比例】短句约{int(short_r * 100)}%，中句约{int(medium_r * 100)}%，"
            f"长句约{int(long_r * 100)}%"
        )

    # Dialogue ratio
    dialogue_ratio = style_profile.get("dialogue_ratio")
    if dialogue_ratio is not None:
        parts.append(f"【对话比例】对话内容约占{int(dialogue_ratio * 100)}%")

    # Rhetoric profile
    rhetoric = style_profile.get("rhetoric_profile", {})
    if rhetoric:
        rhetoric_parts = []
        for name, freq in rhetoric.items():
            if freq > 0.05:
                rhetoric_parts.append(f"{name}(频率{freq:.2f})")
        if rhetoric_parts:
            parts.append(f"【修辞手法】适当使用：{', '.join(rhetoric_parts)}")

    # Paragraph rhythm
    rhythm = style_profile.get("paragraph_rhythm_pattern", "")
    if rhythm:
        parts.append(f"【段落节奏】参考节奏型：{rhythm}")

    # POV type
    pov = style_profile.get("pov_type", "")
    pov_map = {
        "first_person": "第一人称",
        "third_person": "第三人称",
        "mixed": "混合视角",
        "omniscient": "全知视角",
    }
    if pov:
        parts.append(f"【叙述视角】{pov_map.get(pov, pov)}")

    return "\n".join(parts)


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

    async def polish_with_profile(
        self,
        draft_text: str,
        style_profile: dict,
        sample_texts: list[str] | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Polish a draft using a StyleProfile config and few-shot samples.

        Args:
            draft_text: Raw chapter draft from PlotAgent
            style_profile: StyleProfile.config_json from DB
            sample_texts: Few-shot sample texts to include as examples
            max_tokens: Maximum tokens for output

        Returns:
            Polished chapter text
        """
        messages = self._build_profile_messages(
            draft_text, style_profile, sample_texts
        )

        result = await self.router.generate(
            task_type="polishing",
            messages=messages,
            max_tokens=max_tokens,
        )
        return result.text

    async def polish_with_profile_stream(
        self,
        draft_text: str,
        style_profile: dict,
        sample_texts: list[str] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Polish with StyleProfile config and few-shot samples, streaming output."""
        messages = self._build_profile_messages(
            draft_text, style_profile, sample_texts
        )

        async for chunk in self.router.generate_stream(
            task_type="polishing",
            messages=messages,
            max_tokens=max_tokens,
        ):
            yield chunk

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_messages(self, draft_text: str, style_instruction: str) -> list[dict]:
        system = STYLE_AGENT_SYSTEM
        if style_instruction:
            system += f"\n\n【目标风格】\n{style_instruction}"

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"请对以下初稿进行风格润色：\n\n{draft_text}"},
        ]

    def _build_profile_messages(
        self,
        draft_text: str,
        style_profile: dict,
        sample_texts: list[str] | None = None,
    ) -> list[dict]:
        """Build messages with detailed style profile and few-shot examples."""
        style_instruction = _build_style_instruction_from_profile(style_profile)

        system = STYLE_AGENT_SYSTEM
        if style_instruction:
            system += f"\n\n【目标风格详细配置】\n{style_instruction}"

        messages: list[dict] = [{"role": "system", "content": system}]

        # Add few-shot sample texts as assistant examples (2-3 samples)
        samples = (sample_texts or [])[:3]
        if samples:
            for i, sample in enumerate(samples, 1):
                # Trim each sample to a reasonable length
                trimmed = sample[:1500] if len(sample) > 1500 else sample
                messages.append({
                    "role": "user",
                    "content": f"以下是风格参考范文{i}，请学习其写作风格：",
                })
                messages.append({
                    "role": "assistant",
                    "content": (
                        f"我已学习范文{i}的风格特征，包括用词习惯、句式节奏和修辞手法。"
                        f"范文内容：\n{trimmed}"
                    ),
                })

        # Final user message with the draft to polish
        messages.append({
            "role": "user",
            "content": f"请参照以上风格配置和范文，对以下初稿进行风格润色：\n\n{draft_text}",
        })

        return messages
