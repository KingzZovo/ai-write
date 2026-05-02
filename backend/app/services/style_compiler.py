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



def _runtime_rules_for(profile: "StyleProfile") -> list[dict]:
    """Return the rules_json as configured, with a runtime fallback that
    derives rules from ``config_json.dosage_profile`` when rules_json is
    empty.

    This guarantees that v8 dosage profiles (whose rules_json was historically
    left empty) still inject useful style guidance into the LLM prompt, even
    if the recompile-rules workflow has not yet been run on the DB row.
    """
    rules = list(profile.rules_json or [])
    if rules:
        return rules
    cfg = getattr(profile, "config_json", None) or {}
    dosage = cfg.get("dosage_profile") if isinstance(cfg, dict) else None
    if not isinstance(dosage, dict) or not dosage:
        return rules
    try:
        from app.services.dosage_to_rules import derive_rules_from_dosage
        derived, _anti_ai = derive_rules_from_dosage(
            dosage, profile_version=cfg.get("profile_version")
        )
        if derived:
            logger.info(
                "compile_style: using runtime-derived rules from dosage_profile (%d rules)",
                len(derived),
            )
        return derived
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("compile_style dosage fallback failed: %s", e)
        return rules


def compile_style(profile: StyleProfile) -> str:
    """Compile a StyleProfile into a prompt instruction string.

    Only injects WRITING STYLE rules (rhythm, tone, sentence patterns).
    Filters out STRUCTURAL rules (volume count, chapter naming, format).
    """
    sections: list[str] = []

    sections.append(f"写作风格参考：{profile.name}")

    # Filter rules: only keep style/rhythm/dialogue rules, NOT structure/format
    rules = _runtime_rules_for(profile)
    structure_keywords = ["卷", "册", "章节", "篇章", "标题", "命名", "结构", "分卷", "正传", "前传", "开场方式", "仪式化"]
    style_rules = []
    for r in rules:
        rule_text = r.get("rule", "")
        cat = r.get("category", "")
        # Skip structural rules
        if any(kw in rule_text for kw in structure_keywords):
            continue
        if cat in ("structure",) and any(kw in rule_text for kw in ["视角", "视点"]):
            style_rules.append(r)  # Narrative POV is style, keep it
        elif cat not in ("structure",):
            style_rules.append(r)

    if style_rules:
        sections.append("写作时参考以下风格特征（只影响文笔，不影响故事结构和卷数）：")
        for r in style_rules[:8]:
            rule_text = r.get("rule", "")
            # Clean JSON dicts that snuck into rule text
            if "{" in rule_text:
                # Extract just the key value, drop JSON structure
                import re
                rule_text = re.sub(r"\{[^}]*\}", "", rule_text).strip()
                rule_text = re.sub(r"\[[^\]]*\]", "", rule_text).strip()
            # Remove references to specific books/characters
            for ref in ["龙族", "加图索", "昂热", "恺撒", "路明非", "继承人"]:
                rule_text = rule_text.replace(ref, "")
            rule_text = rule_text.strip(" ：:，,。")
            if len(rule_text) > 5:
                sections.append(f"- {rule_text[:80]}")

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
