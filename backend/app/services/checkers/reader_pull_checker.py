"""
Reader Pull Checker ("追读力" Check)

Evaluates a chapter's ability to keep readers engaged and coming back.
Checks for effective hooks, engaging openings, micro-payoffs, and
cliffhanger endings.
"""

from __future__ import annotations

import logging
import re

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack

logger = logging.getLogger(__name__)

# Hook types and their detection patterns
HOOK_PATTERNS: dict[str, list[str]] = {
    "悬念钩": ["到底", "究竟", "难道", "为何", "为什么", "怎么可能", "不可能"],
    "反转钩": ["没想到", "出乎意料", "竟然", "居然", "却是", "原来", "实际上"],
    "危机钩": ["危险", "必须", "来不及", "最后的", "唯一的", "绝境", "生死"],
    "情感钩": ["心痛", "不舍", "泪", "哭", "感动", "心疼", "温暖"],
    "秘密钩": ["秘密", "真相", "隐藏", "不为人知", "暗中", "背后"],
    "成长钩": ["突破", "领悟", "觉醒", "进化", "晋升", "蜕变"],
    "承诺钩": ["一定", "发誓", "绝不", "必须", "总有一天", "等着瞧"],
    "伏笔钩": ["似乎", "隐约", "仿佛", "或许", "也许", "预感"],
}

# Payoff indicators (readers feel satisfied)
PAYOFF_MARKERS = [
    "终于", "果然", "如愿", "成功", "做到了",
    "胜利", "赢了", "突破了", "领悟了", "找到了",
    "真相", "原来如此", "恍然大悟", "难怪",
]

# Engagement killers
ENGAGEMENT_KILLERS = [
    "话说回来", "言归正传", "总而言之", "综上所述",
    "正如前文所述", "众所周知",
    # Very long exposition markers
]


class ReaderPullChecker(BaseChecker):
    """Evaluates chapter's reader engagement potential.

    Checks:
    - Hook effectiveness in opening 20%
    - Micro-payoffs throughout
    - Cliffhanger/hook at ending 10%
    - Engagement killer detection
    - Page-turner pacing (suspense distribution)
    """

    name = "reader_pull"

    async def check(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> CheckResult:
        result = CheckResult(checker_name=self.name)

        if not chapter_text.strip():
            return result

        total_len = len(chapter_text)
        paragraphs = [p.strip() for p in chapter_text.split("\n") if p.strip()]

        if total_len < 100:
            return result

        # Define sections
        opening_end = int(total_len * 0.20)
        closing_start = int(total_len * 0.90)

        opening = chapter_text[:opening_end]
        middle = chapter_text[opening_end:closing_start]
        closing = chapter_text[closing_start:]

        # 1. Opening hook check
        self._check_opening_hook(opening, result)

        # 2. Micro-payoffs throughout
        self._check_micro_payoffs(chapter_text, paragraphs, result)

        # 3. Closing hook / cliffhanger
        self._check_closing_hook(closing, result)

        # 4. Engagement killers
        self._check_engagement_killers(chapter_text, result)

        # 5. Suspense distribution
        self._check_suspense_distribution(paragraphs, result)

        # 6. Pacing variety
        self._check_pacing_variety(paragraphs, result)

        # 7. Context-aware hook suggestions
        self._suggest_hooks(context, result)

        result.score = self._compute_score(result)
        return result

    def _check_opening_hook(
        self,
        opening: str,
        result: CheckResult,
    ) -> None:
        """Check if the opening 20% has an effective hook."""
        hooks_found: list[str] = []

        for hook_type, patterns in HOOK_PATTERNS.items():
            for pattern in patterns:
                if pattern in opening:
                    hooks_found.append(hook_type)
                    break

        # Check for dialogue in opening (engaging)
        has_dialogue = any(
            c in opening for c in ['"', '"', '「']
        )
        if has_dialogue:
            hooks_found.append("对话开场")

        # Check for action in opening
        action_words = ["冲", "跑", "打", "踢", "挡", "躲", "砍", "劈"]
        has_action = any(w in opening[:200] for w in action_words)
        if has_action:
            hooks_found.append("动作开场")

        if not hooks_found:
            result.add_issue(
                type="weak_opening_hook",
                severity="high",
                location="开头20%",
                description="开头缺少有效钩子，未设置悬念、冲突或情感触发点",
                suggestion=(
                    "在开头添加以下任一元素:\n"
                    "- 悬念问题(角色面临未知)\n"
                    "- 动作场面(立即进入冲突)\n"
                    "- 对话开场(角色间互动)\n"
                    "- 情感触发(引发读者共鸣)"
                ),
            )
        elif len(hooks_found) == 1:
            result.add_issue(
                type="moderate_opening_hook",
                severity="low",
                location="开头20%",
                description=f"开头仅有1种钩子类型({hooks_found[0]})，建议叠加多种吸引元素",
                suggestion="考虑在开头叠加悬念+对话或动作+情感等组合钩子",
            )

    def _check_micro_payoffs(
        self,
        chapter_text: str,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Check for micro-payoffs that reward readers throughout the chapter."""
        payoff_count = 0
        payoff_positions: list[int] = []

        for i, para in enumerate(paragraphs):
            for marker in PAYOFF_MARKERS:
                if marker in para:
                    payoff_count += 1
                    payoff_positions.append(i)
                    break

        if len(paragraphs) > 20 and payoff_count == 0:
            result.add_issue(
                type="no_micro_payoffs",
                severity="medium",
                location="全章",
                description="整章缺少微型回报(payoff)，读者缺乏满足感",
                suggestion=(
                    "每隔15-20段安排一个小成就/小揭露/小突破，"
                    "让读者在追读过程中获得阶段性满足"
                ),
            )
        elif len(paragraphs) > 30 and payoff_count < 2:
            result.add_issue(
                type="insufficient_payoffs",
                severity="low",
                location="全章",
                description=f"仅有{payoff_count}个微回报点，对于长章节偏少",
                suggestion="增加角色小胜利、线索揭露、关系进展等微回报",
            )

        # Check payoff distribution
        if payoff_positions and len(paragraphs) > 20:
            middle_start = len(paragraphs) // 4
            middle_end = len(paragraphs) * 3 // 4
            middle_payoffs = [
                p for p in payoff_positions
                if middle_start <= p <= middle_end
            ]
            if not middle_payoffs and len(payoff_positions) >= 2:
                result.add_issue(
                    type="payoff_distribution",
                    severity="low",
                    location="中段",
                    description="微回报集中在章节首尾，中段缺乏奖励点",
                    suggestion="在章节中段(25%-75%)处安排至少一个微回报",
                )

    def _check_closing_hook(
        self,
        closing: str,
        result: CheckResult,
    ) -> None:
        """Check if the chapter ending has an effective cliffhanger/hook."""
        hooks_found: list[str] = []

        for hook_type, patterns in HOOK_PATTERNS.items():
            for pattern in patterns:
                if pattern in closing:
                    hooks_found.append(hook_type)
                    break

        # Check for question marks (suspense)
        has_question = "？" in closing or "?" in closing

        # Check for ellipsis (implication)
        has_ellipsis = "......" in closing or "……" in closing

        # Check for exclamation (shock/excitement)
        has_exclamation = "！" in closing[-200:] if len(closing) > 200 else "！" in closing

        cliffhanger_elements = sum([
            bool(hooks_found),
            has_question,
            has_ellipsis,
            has_exclamation,
        ])

        if cliffhanger_elements == 0:
            result.add_issue(
                type="weak_closing_hook",
                severity="high",
                location="结尾10%",
                description="章节结尾缺少钩子/悬念，无法驱动读者追读下一章",
                suggestion=(
                    "在结尾添加以下任一元素:\n"
                    "- 新危机出现(打断当前局面)\n"
                    "- 重要信息揭露(留下疑问)\n"
                    "- 反转/意外(颠覆预期)\n"
                    "- 角色决定(预示下一步行动)\n"
                    "- 省略号/问句(留白引发好奇)"
                ),
            )

        # Check for abrupt endings
        last_sentences = re.split(r'[。！？]', closing)
        last_sentences = [s.strip() for s in last_sentences if s.strip()]
        if last_sentences:
            last = last_sentences[-1]
            if len(last) < 5 and not has_ellipsis:
                result.add_issue(
                    type="abrupt_ending",
                    severity="low",
                    location="最后一句",
                    description=f"结尾过于突兀('{last}')，缺少收束感",
                    suggestion="延展最后一句，添加悬念暗示或余韵感",
                )

    def _check_engagement_killers(
        self,
        chapter_text: str,
        result: CheckResult,
    ) -> None:
        """Check for phrases that break reader engagement."""
        found_killers: list[tuple[str, int]] = []

        for killer in ENGAGEMENT_KILLERS:
            positions = [m.start() for m in re.finditer(re.escape(killer), chapter_text)]
            for pos in positions:
                found_killers.append((killer, pos))

        if found_killers:
            killer_list = ", ".join(f"'{k}'" for k, _ in found_killers[:5])
            result.add_issue(
                type="engagement_killer",
                severity="low",
                location="多处",
                description=f"存在{len(found_killers)}处打断沉浸感的用语: {killer_list}",
                suggestion="删除或替换这些过于生硬的过渡语，使用更自然的叙述衔接",
            )

    def _check_suspense_distribution(
        self,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Check how suspense is distributed across the chapter."""
        if len(paragraphs) < 10:
            return

        # Count suspense elements per section
        section_size = max(1, len(paragraphs) // 4)
        sections = [
            paragraphs[i:i + section_size]
            for i in range(0, len(paragraphs), section_size)
        ]

        section_suspense: list[int] = []
        for section in sections:
            combined = " ".join(section)
            suspense = sum(
                1 for patterns in HOOK_PATTERNS.values()
                for p in patterns
                if p in combined
            )
            section_suspense.append(suspense)

        # Check for dead zones (sections with 0 suspense)
        for i, count in enumerate(section_suspense):
            if count == 0 and i > 0 and i < len(section_suspense) - 1:
                position = ["前段", "中前段", "中后段", "后段"][
                    min(i, len(["前段", "中前段", "中后段", "后段"]) - 1)
                ]
                result.add_issue(
                    type="suspense_dead_zone",
                    severity="low",
                    location=position,
                    description=f"章节{position}缺少悬念/冲突元素，可能导致读者走神",
                    suggestion=f"在{position}增加一个小悬念或冲突点维持阅读动力",
                )

    def _check_pacing_variety(
        self,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Check for pacing variety (action/dialogue/description mix)."""
        if len(paragraphs) < 10:
            return

        # Classify paragraphs
        action_count = 0
        dialogue_count = 0
        description_count = 0

        for para in paragraphs:
            has_dialogue = any(c in para for c in ['"', '"', '「'])
            has_action = any(
                w in para
                for w in ["冲", "打", "跑", "踢", "砍", "劈", "挡", "躲", "攻"]
            )

            if has_dialogue:
                dialogue_count += 1
            elif has_action:
                action_count += 1
            else:
                description_count += 1

        total = len(paragraphs)
        action_ratio = action_count / total
        dialogue_ratio = dialogue_count / total
        desc_ratio = description_count / total

        # Check for excessive monotony in any category
        if desc_ratio > 0.75:
            result.add_issue(
                type="monotonous_narration",
                severity="medium",
                location="全章",
                description=(
                    f"叙述/描写段落占比{desc_ratio*100:.0f}%，"
                    f"对话占{dialogue_ratio*100:.0f}%，动作占{action_ratio*100:.0f}%。"
                    f"叙述比例过高，节奏偏慢"
                ),
                suggestion="增加对话和动作段落，提高章节活力和代入感",
            )

    def _suggest_hooks(
        self,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Generate hook suggestions based on context."""
        if context.hook_suggestion:
            return  # Already has a suggestion

        suggestions: list[str] = []

        # Based on foreshadow proximity
        ready_foreshadows = [
            f for f in context.foreshadow_triplets if f.proximity > 0.7
        ]
        if ready_foreshadows:
            suggestions.append(
                f"伏笔'{ready_foreshadows[0].foreshadow[:20]}...'接近消解，"
                f"可用作本章悬念钩子"
            )

        # Based on strand tracker
        warnings = context.strand_tracker.get_warnings(
            context.current_outline.get("chapter_idx", 0)
        )
        if warnings:
            suggestions.append("考虑切换叙事线索以增加新鲜感")

        if suggestions:
            context.hook_suggestion = " | ".join(suggestions)

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
