"""BVSR — Blind Variation and Selective Retention.

Generates multiple variant passages for the same plot point,
allowing the user (or automated scoring) to pick the best one.

"抽卡"机制：生成3-5个版本 → 评分 → 保留最优。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)


@dataclass
class Variant:
    index: int
    text: str
    score: float = 0.0
    feedback: str = ""


async def generate_variants(
    prompt: str,
    system_prompt: str = "",
    count: int = 3,
    max_tokens: int = 2048,
    task_type: str = "generation",
) -> list[Variant]:
    """Generate multiple variants of the same content.

    Args:
        prompt: The user prompt (plot point / scene description)
        system_prompt: System instructions
        count: Number of variants to generate (1-5)
        max_tokens: Max tokens per variant
        task_type: Model routing task type

    Returns:
        List of Variant objects (unsorted, unscored)
    """
    count = min(max(count, 1), 5)
    router = get_model_router()

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    async def gen_one(idx: int) -> Variant:
        try:
            # Use slightly different temperature for variety
            temp = 0.7 + (idx * 0.08)
            result = await router.generate(
                task_type=task_type,
                messages=messages,
                temperature=min(temp, 1.0),
                max_tokens=max_tokens,
            )
            return Variant(index=idx, text=result.text)
        except Exception as e:
            return Variant(index=idx, text=f"生成失败: {e}")

    variants = await asyncio.gather(*[gen_one(i) for i in range(count)])
    return list(variants)


async def score_variants(variants: list[Variant]) -> list[Variant]:
    """Score variants using LLM-as-judge. Returns sorted by score (best first)."""
    import json
    router = get_model_router()

    for v in variants:
        if v.text.startswith("生成失败"):
            v.score = 0
            continue
        try:
            result = await router.generate(
                task_type="evaluation",
                messages=[
                    {"role": "system", "content": "你是文学评分专家。评估以下文本质量，输出JSON: {\"score\": 0-10, \"feedback\": \"一句话评价\"}"},
                    {"role": "user", "content": v.text[:2000]},
                ],
                max_tokens=100,
            )
            text = result.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(text)
            v.score = float(data.get("score", 0))
            v.feedback = data.get("feedback", "")
        except Exception:
            v.score = 5.0  # Default

    variants.sort(key=lambda v: v.score, reverse=True)
    return variants
