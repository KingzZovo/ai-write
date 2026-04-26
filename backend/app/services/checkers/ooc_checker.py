"""
Out-of-Character (OOC) Checker

Detects when characters behave or speak in ways that contradict their
established personality, background, and behavioral patterns.
"""

from __future__ import annotations

import json
import logging
import re

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack

logger = logging.getLogger(__name__)

# Common OOC indicators by character archetype
ARCHETYPE_SPEECH_PATTERNS: dict[str, dict[str, list[str]]] = {
    "冷酷": {
        "avoid": ["哈哈", "嘻嘻", "好开心", "太棒了", "可爱"],
        "expected": ["哼", "无所谓", "弱者"],
    },
    "热血": {
        "avoid": ["算了", "无所谓", "随便"],
        "expected": ["一定", "绝不", "我要"],
    },
    "腹黑": {
        "avoid": ["我坦白说", "直说吧", "我没有心机"],
        "expected": ["有趣", "呵", "果然"],
    },
    "天真": {
        "avoid": ["阴谋", "利用", "算计"],
        "expected": ["真的吗", "好厉害", "为什么"],
    },
    "老练": {
        "avoid": ["怎么办", "我不知道", "我害怕"],
        "expected": ["不急", "看情况", "老夫"],
    },
}


class OOCChecker(BaseChecker):
    """Character Out-of-Character detection.

    Checks whether character dialogue and behavior match their established
    personality, background, and mental state. Uses both rule-based pattern
    matching and LLM-assisted analysis.
    """

    name = "ooc"

    async def check(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> CheckResult:
        result = CheckResult(checker_name=self.name)

        if not chapter_text.strip() or context is None or not context.character_cards:
            return result

        # 1. Extract dialogue per character
        dialogues = self._extract_character_dialogues(chapter_text, context)

        # 2. Rule-based OOC check
        self._rule_based_check(dialogues, context, result)

        # 3. Behavioral consistency check
        self._check_behavior_consistency(chapter_text, context, result)

        # 4. Mental state consistency
        self._check_mental_state(chapter_text, context, result)

        # 5. LLM-assisted deep OOC check (for important characters)
        important_chars = [c for c in context.character_cards if c.relationships]
        if important_chars:
            await self._llm_ooc_check(chapter_text, important_chars, dialogues, result)

        result.score = self._compute_score(result)
        return result

    def _extract_character_dialogues(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> dict[str, list[str]]:
        """Extract dialogue lines attributed to each character.

        Handles common Chinese dialogue patterns:
        - "XXX说/道/喊/笑道：......"
        - XXX："......"
        """
        dialogues: dict[str, list[str]] = {}
        char_names = [c.name for c in context.character_cards]

        if not char_names:
            return dialogues

        # Build regex for dialogue extraction
        # Pattern 1: 角色名 + 说/道/喊等 + ：/: + 对话内容
        speech_verbs = "说|道|喊|叫|笑道|冷笑道|怒道|问|答|回答|嘀咕|低声道|沉声道|大喝|厉声道"
        names_pattern = "|".join(re.escape(name) for name in char_names)

        # Pattern: NAME + verb + "dialogue"
        pattern1 = rf'({names_pattern})\s*(?:{speech_verbs})\s*[：:]\s*[""「](.*?)[""」]'
        # Pattern: NAME + verb + ，+ "dialogue"
        pattern2 = rf'({names_pattern})\s*(?:{speech_verbs})\s*[，,]\s*[""「](.*?)[""」]'
        # Pattern: "dialogue" + NAME + verb
        pattern3 = rf'[""「](.*?)[""」]\s*({names_pattern})\s*(?:{speech_verbs})'

        for match in re.finditer(pattern1, chapter_text, re.DOTALL):
            name = match.group(1)
            dialogue = match.group(2)
            dialogues.setdefault(name, []).append(dialogue)

        for match in re.finditer(pattern2, chapter_text, re.DOTALL):
            name = match.group(1)
            dialogue = match.group(2)
            dialogues.setdefault(name, []).append(dialogue)

        for match in re.finditer(pattern3, chapter_text, re.DOTALL):
            dialogue = match.group(1)
            name = match.group(2)
            dialogues.setdefault(name, []).append(dialogue)

        return dialogues

    def _rule_based_check(
        self,
        dialogues: dict[str, list[str]],
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Rule-based OOC detection using archetype speech patterns."""
        for card in context.character_cards:
            if card.name not in dialogues:
                continue

            char_dialogues = dialogues[card.name]
            mental_state = card.mental_state.lower() if card.mental_state else ""

            # Check against known archetypes
            for archetype, patterns in ARCHETYPE_SPEECH_PATTERNS.items():
                if archetype not in mental_state and archetype not in str(card.relationships):
                    continue

                for dialogue in char_dialogues:
                    for avoid_word in patterns["avoid"]:
                        if avoid_word in dialogue:
                            result.add_issue(
                                type="ooc_dialogue",
                                severity="medium",
                                location=f"角色: {card.name}",
                                description=(
                                    f"{card.name}({archetype}类型)的对话中出现了"
                                    f"不符合性格的用语'{avoid_word}': "
                                    f"'{dialogue[:30]}...'"
                                ),
                                suggestion=(
                                    f"调整对话用语，使其更符合{card.name}的"
                                    f"{archetype}性格特征"
                                ),
                            )

        # Check dialogue style samples
        for card in context.character_cards:
            if card.name not in dialogues:
                continue

            samples = context.dialogue_samples.get(card.name, [])
            if not samples:
                continue

            # Compute average dialogue length from samples vs current
            sample_avg_len = sum(len(s) for s in samples) / len(samples) if samples else 0
            current_avg_len = (
                sum(len(d) for d in dialogues[card.name]) / len(dialogues[card.name])
                if dialogues[card.name]
                else 0
            )

            if sample_avg_len > 0 and current_avg_len > 0:
                ratio = current_avg_len / sample_avg_len
                if ratio > 3.0:
                    result.add_issue(
                        type="ooc_dialogue_length",
                        severity="low",
                        location=f"角色: {card.name}",
                        description=(
                            f"{card.name}的对话长度(平均{current_avg_len:.0f}字)"
                            f"显著长于其典型风格(平均{sample_avg_len:.0f}字)"
                        ),
                        suggestion="缩短对话长度以匹配角色说话风格",
                    )
                elif ratio < 0.3:
                    result.add_issue(
                        type="ooc_dialogue_length",
                        severity="low",
                        location=f"角色: {card.name}",
                        description=(
                            f"{card.name}的对话长度(平均{current_avg_len:.0f}字)"
                            f"显著短于其典型风格(平均{sample_avg_len:.0f}字)"
                        ),
                        suggestion="延长对话以匹配角色说话风格",
                    )

    def _check_behavior_consistency(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Check if character actions are consistent with their profile."""
        for card in context.character_cards:
            if card.name not in chapter_text:
                continue

            # Check relationships: enemies should not suddenly be friendly
            for target, rel_type in card.relationships.items():
                if target not in chapter_text:
                    continue

                hostile_rels = ["敌对", "仇敌", "对手", "死敌"]
                friendly_rels = ["盟友", "朋友", "恋人", "师徒", "兄弟"]

                is_hostile = any(h in rel_type for h in hostile_rels)
                is_friendly = any(f in rel_type for f in friendly_rels)

                # Look for interaction between the two characters
                combined_pattern = (
                    f"(?:{re.escape(card.name)}.*?{re.escape(target)}|"
                    f"{re.escape(target)}.*?{re.escape(card.name)})"
                )
                interactions = re.findall(combined_pattern, chapter_text[:5000])

                for interaction in interactions[:3]:
                    friendly_actions = ["微笑", "拥抱", "感谢", "合作", "帮助"]
                    hostile_actions = ["攻击", "杀", "怒视", "敌意", "对抗"]

                    if is_hostile:
                        for action in friendly_actions:
                            if action in interaction:
                                result.add_issue(
                                    type="ooc_relationship",
                                    severity="high",
                                    location=f"角色: {card.name} 与 {target}",
                                    description=(
                                        f"{card.name}与{target}是{rel_type}关系，"
                                        f"但文本中出现了友好行为'{action}'"
                                    ),
                                    suggestion=(
                                        f"调整互动方式以符合{rel_type}关系，"
                                        f"或先铺垫关系转变"
                                    ),
                                )
                                break

                    if is_friendly:
                        for action in hostile_actions:
                            if action in interaction:
                                result.add_issue(
                                    type="ooc_relationship",
                                    severity="medium",
                                    location=f"角色: {card.name} 与 {target}",
                                    description=(
                                        f"{card.name}与{target}是{rel_type}关系，"
                                        f"但文本中出现了敌对行为'{action}'"
                                    ),
                                    suggestion=(
                                        f"调整互动方式以符合{rel_type}关系，"
                                        f"或先铺垫矛盾冲突"
                                    ),
                                )
                                break

    def _check_mental_state(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Check if character behavior matches their current mental state."""
        mental_state_contradictions = {
            "悲伤": ["大笑", "兴奋", "欢快", "开心地"],
            "愤怒": ["温柔地", "轻声", "平静地", "微笑"],
            "恐惧": ["勇敢地冲", "毫不畏惧", "轻松地"],
            "开心": ["痛哭", "悲痛", "绝望"],
            "绝望": ["信心十足", "充满希望", "轻松"],
        }

        for card in context.character_cards:
            if not card.mental_state or card.name not in chapter_text:
                continue

            for state, contradictions in mental_state_contradictions.items():
                if state not in card.mental_state:
                    continue

                # Search near character name mentions
                name_positions = [
                    m.start() for m in re.finditer(re.escape(card.name), chapter_text)
                ]

                for pos in name_positions:
                    window = chapter_text[pos:pos + 200]
                    for contradiction in contradictions:
                        if contradiction in window:
                            result.add_issue(
                                type="ooc_mental_state",
                                severity="medium",
                                location=f"角色: {card.name}",
                                description=(
                                    f"{card.name}当前心理状态为'{card.mental_state}'，"
                                    f"但附近出现了矛盾表现'{contradiction}'"
                                ),
                                suggestion=(
                                    f"调整行为描写以符合{card.name}的当前心理状态，"
                                    f"或先铺垫情绪转变"
                                ),
                            )
                            break

    async def _llm_ooc_check(
        self,
        chapter_text: str,
        characters: list,
        dialogues: dict[str, list[str]],
        result: CheckResult,
    ) -> None:
        """Use LLM for deep OOC analysis on important characters."""
        try:
            from app.services.model_router import get_model_router_async

            router = await get_model_router_async()

            chars_desc = []
            for card in characters[:5]:
                char_info = card.to_prompt()
                char_dialogues = dialogues.get(card.name, [])
                if char_dialogues:
                    sample_lines = "; ".join(char_dialogues[:3])
                    char_info += f"\n  本章对话: {sample_lines[:200]}"
                chars_desc.append(char_info)

            chars_text = "\n".join(chars_desc)

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一位小说角色一致性审查员。检查角色的言行是否符合其人设。\n\n"
                        "返回纯JSON数组，每个元素包含:\n"
                        '- "character": 角色名\n'
                        '- "severity": "high" 或 "medium" 或 "low"\n'
                        '- "description": OOC描述\n'
                        '- "suggestion": 修改建议\n\n'
                        "仅报告确实OOC的情况。如果没有问题，返回空数组 []"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"角色设定:\n{chars_text}\n\n"
                        f"待检查文本(节选):\n{chapter_text[:2500]}"
                    ),
                },
            ]

            gen_result = await router.generate_with_tier_fallback(
                task_type="extraction",
                messages=messages,
                temperature=0.2,
                max_tokens=1024,
                _log_meta={"caller": "ooc_checker._llm_ooc_check"},
            )

            issues = _parse_json_array(gen_result.text)
            for issue in issues:
                result.add_issue(
                    type="ooc_llm",
                    severity=issue.get("severity", "medium"),
                    location=f"角色: {issue.get('character', '')}",
                    description=issue.get("description", ""),
                    suggestion=issue.get("suggestion", ""),
                )

        except Exception as e:
            logger.warning("LLM OOC check failed: %s", e)

    def _compute_score(self, result: CheckResult) -> float:
        score = 10.0
        for issue in result.issues:
            severity = issue.get("severity", "low")
            if severity == "critical":
                score -= 3.0
            elif severity == "high":
                score -= 2.0
            elif severity == "medium":
                score -= 1.0
            elif severity == "low":
                score -= 0.5
        return max(0.0, min(10.0, score))


def _parse_json_array(text: str) -> list[dict]:
    """Parse JSON array from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        import re as _re
        match = _re.search(r"\[.*\]", text, _re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return []
