"""
Continuity Checker

Checks timeline continuity, character position consistency, and
event sequence logic across chapters.
"""

from __future__ import annotations

import logging
import re

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack

logger = logging.getLogger(__name__)

# Time-related keywords for temporal order analysis
TIME_KEYWORDS_ORDER = [
    "清晨", "早上", "上午", "中午", "下午", "傍晚", "黄昏", "晚上", "深夜", "凌晨",
    "第一天", "第二天", "第三天",
    "次日", "翌日", "隔天",
    "一个月后", "半年后", "一年后",
]

# Spatial movement verbs
MOVEMENT_VERBS = [
    "来到", "到达", "抵达", "前往", "走向", "飞往", "传送到",
    "离开", "逃离", "撤退", "返回", "回到",
]


class ContinuityChecker(BaseChecker):
    """Check timeline continuity, character position, and event sequence.

    Detects:
    - Time flow inconsistencies (e.g. afternoon before morning)
    - Characters appearing in impossible locations
    - Event sequence contradictions with previous chapters
    - Dead characters reappearing without explanation
    """

    name = "continuity"

    async def check(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> CheckResult:
        result = CheckResult(checker_name=self.name)

        if not chapter_text.strip():
            return result

        # 1. Timeline consistency within chapter
        self._check_internal_timeline(chapter_text, result)

        # 2. Character location consistency
        self._check_character_locations(chapter_text, context, result)

        # 3. Event sequence against recent summaries
        self._check_event_sequence(chapter_text, context, result)

        # 4. Cross-chapter continuity
        self._check_cross_chapter_continuity(chapter_text, context, result)

        # Compute score
        result.score = self._compute_score(result)
        return result

    def _check_internal_timeline(
        self,
        chapter_text: str,
        result: CheckResult,
    ) -> None:
        """Check for time flow inconsistencies within the chapter."""
        paragraphs = [p.strip() for p in chapter_text.split("\n") if p.strip()]

        last_time_idx = -1
        last_time_word = ""

        for para_idx, para in enumerate(paragraphs):
            for time_idx, time_word in enumerate(TIME_KEYWORDS_ORDER):
                if time_word in para:
                    # Check for backwards time flow within same day context
                    if (
                        last_time_idx >= 0
                        and time_idx < last_time_idx
                        and last_time_idx - time_idx <= 5  # within daily cycle
                        and "前" not in para[max(0, para.index(time_word) - 5):para.index(time_word)]
                        and "回忆" not in para
                        and "想起" not in para
                        and "记得" not in para
                    ):
                        result.add_issue(
                            type="timeline_reversal",
                            severity="medium",
                            location=f"段落 {para_idx + 1}",
                            description=(
                                f"时间流向异常: 先出现'{last_time_word}'"
                                f"(段落更早处), 后出现'{time_word}', "
                                f"时间似乎倒流"
                            ),
                            suggestion="检查时间顺序是否正确，或添加回忆/闪回标记",
                        )
                    last_time_idx = time_idx
                    last_time_word = time_word
                    break

    def _check_character_locations(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Check if character locations are consistent."""
        # Build location map from context
        char_locations: dict[str, str] = {}
        for card in context.character_cards:
            if card.location:
                char_locations[card.name] = card.location

        if not char_locations:
            return

        paragraphs = [p.strip() for p in chapter_text.split("\n") if p.strip()]

        # Track location changes within the chapter
        chapter_locations: dict[str, list[tuple[int, str]]] = {}

        for para_idx, para in enumerate(paragraphs):
            for char_name, known_location in char_locations.items():
                if char_name not in para:
                    continue

                # Check for movement verbs
                for verb in MOVEMENT_VERBS:
                    if verb in para:
                        # Extract destination
                        verb_pos = para.index(verb)
                        after_verb = para[verb_pos + len(verb):verb_pos + len(verb) + 20]
                        destination = after_verb.strip().split("，")[0].split("。")[0]
                        if destination:
                            chapter_locations.setdefault(char_name, []).append(
                                (para_idx, destination)
                            )

        # Check for teleportation (same character in distant locations
        # without travel description)
        for char_name, locations in chapter_locations.items():
            for i in range(1, len(locations)):
                prev_para, prev_loc = locations[i - 1]
                curr_para, curr_loc = locations[i]
                if (
                    prev_loc != curr_loc
                    and curr_para - prev_para <= 2  # Very close paragraphs
                    and prev_loc not in curr_loc
                    and curr_loc not in prev_loc
                ):
                    result.add_issue(
                        type="location_teleport",
                        severity="medium",
                        location=f"段落 {curr_para + 1}, 角色 {char_name}",
                        description=(
                            f"{char_name}在段落{prev_para + 1}位于'{prev_loc}',"
                            f"但在段落{curr_para + 1}突然出现在'{curr_loc}',"
                            f"中间没有移动描写"
                        ),
                        suggestion="添加角色从一处到另一处的移动过渡",
                    )

    def _check_event_sequence(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Check event sequence against recent chapter summaries."""
        if not context.recent_summaries:
            return

        # Extract key events from current chapter
        current_events = self._extract_key_events(chapter_text)
        if not current_events:
            return

        # Check for events that contradict recent summaries
        combined_summaries = " ".join(context.recent_summaries)

        # Look for contradictory patterns
        contradiction_pairs = [
            ("死", "活着"),
            ("离开", "一直在"),
            ("失去", "拥有"),
            ("破坏", "完好"),
            ("消失", "出现"),
        ]

        for positive, negative in contradiction_pairs:
            for event in current_events:
                if positive in event:
                    # Check if the negative exists in recent context
                    # suggesting a contradiction
                    subject = event.split(positive)[0][-10:]
                    if subject and negative in combined_summaries:
                        # Check if the same subject is involved
                        subject_clean = re.sub(r'[^\u4e00-\u9fff]', '', subject)
                        if subject_clean and subject_clean in combined_summaries:
                            result.add_issue(
                                type="event_contradiction",
                                severity="high",
                                location=f"事件: {event[:30]}",
                                description=(
                                    f"当前章节描述'{event[:30]}...'，"
                                    f"但近期摘要中暗示了相反情况"
                                ),
                                suggestion="检查与前文的一致性",
                            )

    def _check_cross_chapter_continuity(
        self,
        chapter_text: str,
        context: ContextPack,
        result: CheckResult,
    ) -> None:
        """Check continuity with previous chapter content."""
        if not context.recent_summaries:
            return

        last_summary = context.recent_summaries[-1] if context.recent_summaries else ""
        if not last_summary:
            return

        # Check if opening connects to previous chapter ending
        first_500 = chapter_text[:500]

        # Check for character names in last summary that should appear early
        # in the new chapter (unless there's a scene change)
        scene_change_indicators = [
            "与此同时", "另一边", "此时此刻", "在另一个地方",
            "话分两头", "却说", "且说",
        ]

        has_scene_change = any(
            indicator in first_500 for indicator in scene_change_indicators
        )

        if not has_scene_change and last_summary:
            # Extract character names from last summary
            char_names_in_summary: set[str] = set()
            for card in context.character_cards:
                if card.name in last_summary:
                    char_names_in_summary.add(card.name)

            # If key characters from last chapter don't appear in the
            # first 500 chars and there's no scene change, flag it
            if char_names_in_summary:
                names_in_opening = {
                    name for name in char_names_in_summary if name in first_500
                }
                missing = char_names_in_summary - names_in_opening
                if missing and len(missing) == len(char_names_in_summary):
                    result.add_issue(
                        type="continuity_break",
                        severity="low",
                        location="章节开头",
                        description=(
                            f"上一章涉及角色{', '.join(list(missing)[:3])}，"
                            f"但本章开头未提及任何一位，且没有转场标记"
                        ),
                        suggestion="添加转场过渡，或在开头延续前文角色线索",
                    )

    def _extract_key_events(self, text: str) -> list[str]:
        """Extract key event descriptions from chapter text."""
        events: list[str] = []
        # Look for sentences with strong action verbs
        action_verbs = [
            "击败", "杀死", "获得", "突破", "发现", "揭露",
            "背叛", "结盟", "离开", "到达", "死亡", "复活",
            "领悟", "觉醒", "毁灭", "创造", "失去", "救",
        ]
        sentences = re.split(r'[。！？]', text)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            for verb in action_verbs:
                if verb in sentence:
                    events.append(sentence[:60])
                    break
        return events[:20]

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
