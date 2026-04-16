"""StyleCompiler: Compiles style profile rules into prompt instructions.

Converts a StyleProfile's rules_json into structured prompt text that can be
injected into the generation system prompt. Rules are weighted:
  - weight >= 0.85: "必须保持" (must maintain)
  - weight >= 0.65: "优先保持" (preferably maintain)
  - weight < 0.65:  "参考" (reference)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.project import StyleProfile

logger = logging.getLogger(__name__)


def compile_style(profile: StyleProfile) -> str:
    """Compile a StyleProfile into a prompt instruction string."""
    sections: list[str] = []

    # Header
    sections.append(f"【写法指导：{profile.name}】")
    if profile.description:
        sections.append(profile.description)

    # Weighted rules
    rules = profile.rules_json or []
    if rules:
        must_rules = [r for r in rules if r.get("weight", 0.5) >= 0.85]
        prefer_rules = [r for r in rules if 0.65 <= r.get("weight", 0.5) < 0.85]
        ref_rules = [r for r in rules if r.get("weight", 0.5) < 0.65]

        if must_rules:
            sections.append("\n【必须保持】")
            for r in must_rules:
                sections.append(f"- {r.get('rule', '')}")

        if prefer_rules:
            sections.append("\n【优先保持】")
            for r in prefer_rules:
                sections.append(f"- {r.get('rule', '')}")

        if ref_rules:
            sections.append("\n【参考风格】")
            for r in ref_rules:
                sections.append(f"- {r.get('rule', '')}")

    # Anti-AI rules
    anti_ai = profile.anti_ai_rules or []
    if anti_ai:
        sections.append("\n【Anti-AI 规则】")
        for rule in anti_ai:
            pattern = rule.get("pattern", "")
            replacement = rule.get("replacement", "")
            if replacement:
                sections.append(f"- 避免使用「{pattern}」，改用「{replacement}」")
            else:
                sections.append(f"- 避免使用「{pattern}」")

    # Tone keywords
    keywords = profile.tone_keywords or []
    if keywords:
        sections.append(f"\n【风格关键词】{', '.join(keywords)}")

    # Sample passages (few-shot)
    samples = profile.sample_passages or []
    if samples:
        sections.append("\n【风格参考样本】")
        for i, sample in enumerate(samples[:3], 1):
            text = sample if isinstance(sample, str) else sample.get("text", "")
            sections.append(f"样本{i}：\n{text[:500]}")

    return "\n".join(sections)


def compile_anti_ai_instructions(profile: StyleProfile) -> str:
    """Compile only the Anti-AI rules into a concise instruction."""
    anti_ai = profile.anti_ai_rules or []
    if not anti_ai:
        return ""

    avoid_words = []
    replacements = []
    for rule in anti_ai:
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        if rule.get("autoRewrite") and replacement:
            replacements.append(f"「{pattern}」→「{replacement}」")
        elif pattern:
            avoid_words.append(f"「{pattern}」")

    parts = []
    if avoid_words:
        parts.append(f"禁止使用以下词汇：{' '.join(avoid_words)}")
    if replacements:
        parts.append(f"自动替换：{' | '.join(replacements)}")

    return "\n".join(parts)
