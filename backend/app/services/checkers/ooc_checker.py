"""Review character choices in context; personality labels are not rules."""
from __future__ import annotations

import logging

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack
from app.services.review_contract import parse_checker_issues

logger = logging.getLogger(__name__)


class OOCChecker(BaseChecker):
    name = 'ooc'

    async def check(self, chapter_text: str, context: ContextPack) -> CheckResult:
        result = CheckResult(checker_name=self.name)
        if not chapter_text.strip() or context is None:
            result.mark_unavailable('review_input_missing')
            return result
        if not context.character_cards:
            return result
        # Read every prepared character and the full manuscript. There is no
        # standalone dialogue excerpt or archetype word-count judgement.
        await self._llm_ooc_check(chapter_text, context.character_cards, {}, result,
                                  cognition=context.cognition_boundaries)
        if result.completed:
            result.score = self._compute_score(result)
        return result

    async def _llm_ooc_check(
        self,
        chapter_text: str,
        characters: list,
        dialogues: dict[str, list[str]],
        result: CheckResult,
        *, cognition: str = '',
    ) -> None:
        """Use LLM for deep OOC analysis on important characters."""
        try:
            if not isinstance(cognition, str):
                raise ValueError('character_cognition_retrieval_pending')
            from app.services.model_router import get_model_router_async

            router = await get_model_router_async()

            chars_desc = []
            for card in characters:
                char_info = card.to_prompt()
                chars_desc.append(char_info)

            chars_text = "\n".join(chars_desc)

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一位小说角色一致性审查员。检查角色的言行是否符合其人设。\n\n"
                        "结合处境、关系、顾虑和前文变化理解人物选择。冷酷的人也会笑，老练的人也会怕；"
                        "误信、隐瞒、试探、反讽和情绪转变不能仅凭标签判成OOC。\n\n"
                        "返回纯JSON数组，每个元素包含:\n"
                        '- "character": 角色名\n'
                        '- "severity": "high" 或 "medium" 或 "low"\n'
                        '- "description": OOC描述\n'
                        '- "location": 本稿逐字原句，不能仅写人物名或段落号\n'
                        '- "suggestion": 修改建议\n\n'
                        "仅报告确实OOC的情况。如果没有问题，返回空数组 []"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"角色设定:\n{chars_text}\n\n"
                        f"人物认知记录（按原章序理解误信、怀疑与获知）:\n{cognition}\n\n"
                        f"待检查完整文本:\n{chapter_text}"
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

            issues = parse_checker_issues(gen_result, draft=chapter_text)
            for issue in issues:
                result.add_issue(
                    type="ooc_llm",
                    severity=issue.get("severity", "medium"),
                    location=issue['location'],
                    description=issue.get("description", ""),
                    suggestion=issue.get("suggestion", ""),
                )

        except Exception as e:
            logger.warning("LLM OOC check unavailable (%s)", type(e).__name__)
            result.mark_unavailable('model_review_unavailable', stage='ooc_llm')

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
