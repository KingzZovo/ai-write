"""
Consistency Checker

Checks for setting/power system violations and world rule conflicts.
Validates that the generated chapter does not contradict established
world rules, power levels, or other immutable facts.
"""

from __future__ import annotations

import logging
import re

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack
from app.services.review_contract import parse_checker_issues

logger = logging.getLogger(__name__)


class ConsistencyChecker(BaseChecker):
    """Check for world rule violations and setting contradictions.

    Detection methods:
    1. Rule-based: exact match of contradictory patterns against world rules
    2. LLM-assisted: uses extraction model to detect subtle violations
    """

    name = "consistency"

    async def check(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> CheckResult:
        result = CheckResult(checker_name=self.name)

        if not chapter_text.strip():
            result.mark_unavailable('empty_draft')
            return result
        if context is None:
            result.mark_unavailable('review_context_missing')
            return result

        # Keyword matches are observations; the contextual review below
        # decides whether a prohibited action actually took place.
        self._check_power_consistency(chapter_text, context, result)

        # 3. Check contradiction cache
        self._check_contradiction_cache(chapter_text, context, result)

        # Keyword proximity can suggest a place to read, not establish which
        # character acts or whether the sentence is negated/remembered.
        result.observations.extend(result.issues)
        result.issues = []
        result.passed = True

        # 4. LLM-based deep check if world rules exist
        if context.world_rules or context.character_cards or context.cognition_boundaries:
            await self._llm_consistency_check(chapter_text, context, result)

        # Compute score from issues
        if result.completed:
            result.score = self._compute_score(result)
        return result

    def _check_power_consistency(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Check if character power levels are used consistently."""
        for card in context.character_cards:
            if not card.power_level:
                continue

            # Check for power level keywords in text
            name = card.name
            if name not in chapter_text:
                continue

            # Look for power escalation indicators that may conflict
            power_keywords = {
                "初级": 1, "初阶": 1, "入门": 1,
                "中级": 2, "中阶": 2,
                "高级": 3, "高阶": 3,
                "大师": 4, "宗师": 4, "圣级": 5,
                "神级": 6, "仙级": 7,
            }

            current_level = 0
            for kw, level in power_keywords.items():
                if kw in card.power_level:
                    current_level = level
                    break

            if current_level == 0:
                continue

            # Check if the text describes abilities beyond their level
            # by looking for higher-tier keywords near the character name
            text_lower = chapter_text
            name_positions = [m.start() for m in re.finditer(re.escape(name), text_lower)]

            for pos in name_positions:
                context_window = text_lower[max(0, pos - 50): pos + len(name) + 200]
                for kw, level in power_keywords.items():
                    if kw in context_window and level > current_level + 1:
                        result.add_issue(
                            type="power_level_violation",
                            severity="medium",
                            location=f"角色: {name}, 附近出现 '{kw}'",
                            description=(
                                f"{name}当前实力为'{card.power_level}',"
                                f"但文本暗示了'{kw}'级别的能力"
                            ),
                            suggestion=(
                                f"确保{name}的表现符合其当前实力等级'{card.power_level}'"
                            ),
                        )

    def _check_contradiction_cache(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Check against known contradictions that should be avoided."""
        for contradiction in context.contradiction_cache:
            # Simple keyword matching for cached contradictions
            # Each contradiction entry is a brief description
            keywords = re.findall(r'[\u4e00-\u9fff]+', contradiction)
            # Check if multiple keywords from the contradiction appear together
            found_keywords = [kw for kw in keywords if kw in chapter_text and len(kw) >= 2]
            if len(found_keywords) >= 2:
                result.add_issue(
                    type="known_contradiction",
                    severity="medium",
                    location=f"相关: {', '.join(found_keywords[:3])}",
                    description=f"文本可能触犯了已知矛盾: {contradiction[:80]}",
                    suggestion="请检查这些元素的搭配是否与已知矛盾冲突",
                )

    async def _llm_consistency_check(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Use LLM to perform deep consistency analysis."""
        try:
            from app.services.model_router import get_model_router_async

            router = await get_model_router_async()

            rules_text = "\n".join(f"- {r}" for r in context.world_rules)
            chars_text = "\n".join(
                c.to_prompt() for c in context.character_cards
            )
            cognition_text = context.cognition_boundaries
            if not isinstance(cognition_text, str):
                from app.services.context_errors import RequiredContextUnavailable
                raise RequiredContextUnavailable('character_cognition_retrieval_pending')

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一个小说一致性审查员。检查给定章节文本是否违反了世界规则或角色设定。\n\n"
                        "认知记录按原章序理解，怀疑、误信、人物隐瞒和不可靠叙述不能直接当作逻辑错误；"
                        "必须结合具体原句核查，较早的未知记录不能推翻后来的获知。\n\n"
                        "返回纯JSON数组，每个元素包含:\n"
                        '- "type": "world_rule_violation" 或 "character_violation"\n'
                        '- "severity": "critical" 或 "high" 或 "medium"\n'
                        '- "description": 问题描述\n'
                        '- "location": 本稿中能逐字找到的证据原句，不能只写段落号\n'
                        '- "suggestion": 修改建议\n\n'
                        "如果没有问题，返回空数组 []"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"世界规则:\n{rules_text}\n\n"
                        f"角色设定:\n{chars_text}\n\n"
                        f"人物认知记录:\n{cognition_text}\n\n"
                        f"待检查文本:\n{chapter_text}"
                    ),
                },
            ]

            gen_result = await router.generate_with_tier_fallback(
                task_type="extraction",
                messages=messages,
                temperature=0.2,
                max_tokens=1024,
                _log_meta={"caller": "consistency_checker._llm_consistency_check"},
            )

            issues = parse_checker_issues(gen_result, draft=chapter_text)
            for issue in issues:
                result.add_issue(
                    type=issue.get("type", "consistency_violation"),
                    severity=issue.get("severity", "medium"),
                    location=issue.get("location", ""),
                    description=issue.get("description", ""),
                    suggestion=issue.get("suggestion", ""),
                )

        except Exception as e:
            logger.warning("LLM consistency check unavailable (%s)", type(e).__name__)
            result.mark_unavailable('model_review_unavailable', stage='consistency_llm')

    def _compute_score(self, result: CheckResult) -> float:
        """Compute score based on issues found."""
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
