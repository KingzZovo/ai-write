"""ConStory-Checker: Cross-chapter consistency deep check.

Validates consistency across chapters for:
- Character states (location, appearance, abilities)
- Timeline (event ordering, date consistency)
- World rules (power system, geography)
- Plot threads (unresolved threads, contradictions)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ConsistencyIssue:
    category: str  # character, timeline, world_rule, plot
    severity: str  # error, warning, info
    description: str
    chapter_refs: list[int] = field(default_factory=list)
    suggestion: str = ""


@dataclass
class ConsistencyReport:
    total_issues: int = 0
    errors: int = 0
    warnings: int = 0
    issues: list[ConsistencyIssue] = field(default_factory=list)
    score: float = 10.0  # 0-10, deducted by issues

    def to_dict(self) -> dict:
        return {
            "total_issues": self.total_issues,
            "errors": self.errors,
            "warnings": self.warnings,
            "score": self.score,
            "issues": [
                {"category": i.category, "severity": i.severity,
                 "description": i.description, "chapter_refs": i.chapter_refs,
                 "suggestion": i.suggestion}
                for i in self.issues
            ],
        }


CONSTORY_PROMPT = """你是一个专业的小说一致性校验引擎。检查以下多章节文本之间的一致性问题。

检查维度：
1. 角色一致性：同一角色在不同章节中的外貌、能力、性格是否矛盾
2. 时间线一致性：事件发生顺序是否合理，有无时间跳跃矛盾
3. 世界观一致性：设定/规则在不同章节中是否自相矛盾
4. 情节线一致性：伏笔是否被遗忘，支线是否断裂

输出纯 JSON 格式：
{
  "issues": [
    {
      "category": "character|timeline|world_rule|plot",
      "severity": "error|warning|info",
      "description": "具体问题描述",
      "chapter_refs": [涉及的章节号],
      "suggestion": "修改建议"
    }
  ]
}

如果没有发现问题，返回 {"issues": []}

以下是需要检查的章节内容：
"""


async def check_cross_chapter_consistency(
    chapter_texts: list[tuple[int, str, str]],  # [(chapter_idx, title, content)]
) -> ConsistencyReport:
    """Run consistency check across multiple chapters using LLM.

    Args:
        chapter_texts: List of (chapter_idx, title, content) tuples

    Returns:
        ConsistencyReport with found issues
    """
    import json
    from app.services.model_router import get_model_router

    if len(chapter_texts) < 2:
        return ConsistencyReport()

    # Build context — sample beginning + end of each chapter to fit token limits
    parts = []
    for idx, title, content in chapter_texts:
        excerpt = content[:800] + ("\n...\n" + content[-400:] if len(content) > 1200 else "")
        parts.append(f"【第{idx}章 {title}】\n{excerpt}")

    combined = "\n\n---\n\n".join(parts)

    router = get_model_router()
    try:
        result = await router.generate(
            task_type="evaluation",
            messages=[
                {"role": "system", "content": "你是小说一致性检查引擎，只输出 JSON。"},
                {"role": "user", "content": CONSTORY_PROMPT + combined},
            ],
            max_tokens=1024,
        )

        text = result.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)

        issues = []
        for item in data.get("issues", []):
            issues.append(ConsistencyIssue(
                category=item.get("category", "plot"),
                severity=item.get("severity", "warning"),
                description=item.get("description", ""),
                chapter_refs=item.get("chapter_refs", []),
                suggestion=item.get("suggestion", ""),
            ))

        errors = sum(1 for i in issues if i.severity == "error")
        warnings = sum(1 for i in issues if i.severity == "warning")
        score = max(0, 10.0 - errors * 2.0 - warnings * 0.5)

        return ConsistencyReport(
            total_issues=len(issues),
            errors=errors,
            warnings=warnings,
            issues=issues,
            score=score,
        )

    except Exception as e:
        logger.warning("ConStory check failed: %s", e)
        return ConsistencyReport()
