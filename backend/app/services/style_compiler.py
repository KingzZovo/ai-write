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

# Backward-compat scrub entries (pre-generalization hardcoded 龙族 list).
_LEGACY_PROPER_NOUNS = ["龙族", "加图索", "昂热", "恺撒", "路明非", "继承人"]


def _profile_proper_nouns(profile: StyleProfile) -> list[str]:
    """Collect proper nouns to scrub from the profile's own source metadata.

    Sources: source_book title (with/without 《》), config_json character-name
    lists when present. Names shorter than 2 chars are skipped so the scrub
    never eats ordinary characters.
    """
    nouns: list[str] = []

    def _add(value) -> None:
        if isinstance(value, str):
            v = value.strip().strip("《》")
            if len(v) >= 2 and v not in nouns:
                nouns.append(v)

    source_book = getattr(profile, "source_book", None) or ""
    if source_book and not source_book.startswith("author:") and source_book != "text_detection":
        _add(source_book)
    config = getattr(profile, "config_json", None) or {}
    if isinstance(config, dict):
        for key in ("source_character_names", "character_names", "characters"):
            vals = config.get(key)
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, dict):
                        _add(v.get("name"))
                    else:
                        _add(v)
    return nouns


def compile_style(
    profile: StyleProfile,
    *,
    include_samples: bool = True,
    extra_proper_nouns: list[str] | None = None,
) -> str:
    """Compile a StyleProfile into a prompt instruction string.

    Injects ALL extracted rules — style/rhythm/dialogue AND chapter-structure
    habits (开场方式、章末钩子 etc.). The old structure-keyword blacklist
    deleted the chapter-hook rules and is intentionally gone.

    ``include_samples=False`` drops the raw sample_passages few-shot block —
    the production writer-injection path uses this (PR-NO-RAW-INJECT: raw
    reference passages must not reach generation prompts).
    """
    sections: list[str] = []

    sections.append(f"写作风格参考：{profile.name}")

    proper_nouns = list(_LEGACY_PROPER_NOUNS)
    for n in _profile_proper_nouns(profile) + list(extra_proper_nouns or []):
        if n and n not in proper_nouns:
            proper_nouns.append(n)

    rules = profile.rules_json or []
    if rules:
        sections.append("写作时参考以下风格特征（含文笔与章节节奏/钩子习惯，不复制原书剧情）：")
        for r in rules[:16]:
            rule_text = r.get("rule", "") if isinstance(r, dict) else str(r)
            # Clean JSON dicts/lists that snuck into rule text
            if "{" in rule_text or "[" in rule_text:
                import re
                rule_text = re.sub(r"\{[^}]*\}", "", rule_text).strip()
                rule_text = re.sub(r"\[[^\]]*\]", "", rule_text).strip()
                # Skip rules that became fragmentary after cleanup
                # (unbalanced brackets, dangling colons, or too short).
                if (
                    not rule_text
                    or rule_text.endswith(("[", "]", "{", "}", ":", "："))
                    or rule_text.count("[") != rule_text.count("]")
                    or rule_text.count("{") != rule_text.count("}")
                ):
                    continue
            # Remove references to the source book / its characters
            for ref in proper_nouns:
                rule_text = rule_text.replace(ref, "")
            rule_text = rule_text.strip(" ：:，,。")
            if len(rule_text) > 5:
                sections.append(f"- {rule_text[:160]}")

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

    # Sample passages (few-shot) — skipped on the production injection path.
    samples = (profile.sample_passages or []) if include_samples else []
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
