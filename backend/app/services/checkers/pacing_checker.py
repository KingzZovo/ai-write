"""
Pacing Checker

Analyzes chapter rhythm: sentence length variation, information density waves,
and tension curve. Detects monotonous pacing and suggests improvements.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack

logger = logging.getLogger(__name__)

# Tension-raising elements
TENSION_UP_MARKERS = [
    "突然", "忽然", "猛然", "骤然", "一声巨响", "危险",
    "杀意", "杀气", "怒吼", "尖叫", "爆炸", "冲击",
    "来不及", "快", "急", "立刻", "马上",
    "不好", "糟了", "完了", "死", "血",
]

# Tension-lowering elements
TENSION_DOWN_MARKERS = [
    "平静", "安静", "沉默", "轻轻", "缓缓", "慢慢",
    "微笑", "笑了", "放心", "安心", "没事",
    "休息", "睡", "吃", "喝茶", "聊天",
    "阳光", "微风", "花", "鸟鸣",
]

# Dialogue markers
DIALOGUE_MARKERS = ['"', '"', '"', '「', '」', '：']


class PacingChecker(BaseChecker):
    """Rhythm and pacing analysis.

    Checks:
    - Sentence length variation (monotony detection)
    - Information density waves
    - Tension curve shape
    - Dialogue vs narration balance
    - Paragraph length distribution
    """

    name = "pacing"

    async def check(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> CheckResult:
        result = CheckResult(checker_name=self.name)

        if not chapter_text.strip():
            return result

        sentences = self._split_sentences(chapter_text)
        paragraphs = [p.strip() for p in chapter_text.split("\n") if p.strip()]

        if len(sentences) < 5:
            return result

        # 1. Sentence length variation
        self._check_sentence_variation(sentences, result)

        # 2. Paragraph length distribution
        self._check_paragraph_distribution(paragraphs, result)

        # 3. Tension curve
        self._check_tension_curve(paragraphs, result)

        # 4. Dialogue vs narration balance
        self._check_dialogue_balance(chapter_text, paragraphs, result)

        # 5. Information density
        self._check_information_density(paragraphs, result)

        # 6. Opening and closing quality
        self._check_opening_closing(paragraphs, result)

        result.score = self._compute_score(result)
        return result

    def _split_sentences(self, text: str) -> list[str]:
        """Split text into sentences using Chinese punctuation."""
        # Split on sentence-ending punctuation
        raw = re.split(r'[。！？!?]', text)
        return [s.strip() for s in raw if s.strip() and len(s.strip()) > 1]

    def _check_sentence_variation(
        self,
        sentences: list[str],
        result: CheckResult,
    ) -> None:
        """Check sentence length variation to detect monotony."""
        lengths = [len(s) for s in sentences]
        if len(lengths) < 10:
            return

        avg_len = sum(lengths) / len(lengths)
        std_dev = math.sqrt(sum((l - avg_len) ** 2 for l in lengths) / len(lengths))
        cv = std_dev / avg_len if avg_len > 0 else 0  # coefficient of variation

        if cv < 0.25:
            result.add_issue(
                type="monotonous_sentences",
                severity="medium",
                location="全章",
                description=(
                    f"句长变异系数仅{cv:.2f}(建议>0.35)，"
                    f"平均句长{avg_len:.0f}字，标准差{std_dev:.1f}字。"
                    f"句式过于单一，缺乏节奏变化"
                ),
                suggestion=(
                    "交替使用长短句：紧张场面用短句(3-8字)，"
                    "描写/心理用长句(20-40字)，增加节奏变化"
                ),
            )

        # Check for consecutive similar-length sentences
        consecutive_similar = 0
        max_consecutive = 0
        for i in range(1, len(lengths)):
            ratio = min(lengths[i], lengths[i - 1]) / max(lengths[i], lengths[i - 1], 1)
            if ratio > 0.8:  # within 20% of each other
                consecutive_similar += 1
                max_consecutive = max(max_consecutive, consecutive_similar)
            else:
                consecutive_similar = 0

        if max_consecutive >= 8:
            result.add_issue(
                type="consecutive_similar_length",
                severity="medium",
                location="多处",
                description=(
                    f"存在连续{max_consecutive}个相近长度的句子，"
                    f"阅读体验趋于平淡"
                ),
                suggestion="在连续段落中穿插不同长度的句子以增加节奏感",
            )

    def _check_paragraph_distribution(
        self,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Check paragraph length distribution."""
        if len(paragraphs) < 5:
            return

        lengths = [len(p) for p in paragraphs]
        avg_len = sum(lengths) / len(lengths)

        # Check for wall-of-text paragraphs
        long_paras = [i for i, l in enumerate(lengths) if l > 300]
        if len(long_paras) > len(paragraphs) * 0.3:
            result.add_issue(
                type="wall_of_text",
                severity="medium",
                location=f"段落 {', '.join(str(i+1) for i in long_paras[:5])}",
                description=(
                    f"{len(long_paras)}个段落超过300字(占{len(long_paras)/len(paragraphs)*100:.0f}%)，"
                    f"大段文字墙影响阅读体验"
                ),
                suggestion="将长段落拆分，每段控制在100-200字，增加对话和动作穿插",
            )

        # Check for too many short paragraphs in sequence (choppy)
        short_streak = 0
        max_short_streak = 0
        for length in lengths:
            if length < 20:
                short_streak += 1
                max_short_streak = max(max_short_streak, short_streak)
            else:
                short_streak = 0

        if max_short_streak >= 10:
            result.add_issue(
                type="choppy_paragraphs",
                severity="low",
                location="多处",
                description=(
                    f"连续{max_short_streak}个极短段落(各不足20字)，"
                    f"节奏过于碎片化"
                ),
                suggestion="适当合并短段落，或在短段落间穿插较长的描写段",
            )

    def _check_tension_curve(
        self,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Analyze the tension curve across the chapter."""
        if len(paragraphs) < 5:
            return

        # Compute tension score for each paragraph
        tension_scores: list[float] = []
        for para in paragraphs:
            up = sum(1 for m in TENSION_UP_MARKERS if m in para)
            down = sum(1 for m in TENSION_DOWN_MARKERS if m in para)
            # Sentence length also affects tension: shorter = more tense
            sentences = re.split(r'[。！？]', para)
            avg_sent_len = (
                sum(len(s) for s in sentences if s.strip()) /
                max(len([s for s in sentences if s.strip()]), 1)
            )
            length_factor = max(0, 1.0 - avg_sent_len / 50.0)  # shorter = higher

            score = (up - down * 0.5 + length_factor) / max(len(para) / 100, 1)
            tension_scores.append(score)

        # Check for flat tension (no peaks or valleys)
        if tension_scores:
            max_tension = max(tension_scores)
            min_tension = min(tension_scores)
            tension_range = max_tension - min_tension

            if tension_range < 0.3 and len(paragraphs) > 10:
                result.add_issue(
                    type="flat_tension",
                    severity="medium",
                    location="全章",
                    description=(
                        f"张力曲线过于平坦(波动范围{tension_range:.2f})，"
                        f"缺乏明显的高潮和低谷"
                    ),
                    suggestion=(
                        "设计至少1-2个小高潮节点，在行动/冲突段落使用短句和紧张词汇，"
                        "在过渡段落放缓节奏"
                    ),
                )

            # Check if tension peaks too early or too late
            if len(tension_scores) >= 5:
                quarter = len(tension_scores) // 4
                last_quarter_avg = (
                    sum(tension_scores[-quarter:]) / quarter if quarter > 0 else 0
                )
                first_quarter_avg = (
                    sum(tension_scores[:quarter]) / quarter if quarter > 0 else 0
                )

                # Tension should generally rise toward the end (for cliffhanger)
                peak_idx = tension_scores.index(max_tension)
                position_ratio = peak_idx / len(tension_scores)

                if position_ratio < 0.15 and len(paragraphs) > 15:
                    result.add_issue(
                        type="early_peak",
                        severity="low",
                        location=f"段落 {peak_idx + 1}",
                        description=(
                            f"全章高潮点出现在前{position_ratio*100:.0f}%处，"
                            f"后续可能趋于平淡"
                        ),
                        suggestion="考虑将高潮推后，或在后半段设计第二个高潮",
                    )

    def _check_dialogue_balance(
        self,
        chapter_text: str,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Check dialogue vs narration balance."""
        total_chars = len(chapter_text)
        if total_chars < 100:
            return

        # Count dialogue characters
        dialogue_chars = 0
        in_dialogue = False
        for char in chapter_text:
            if char in '""「':
                in_dialogue = True
            elif char in '""」':
                in_dialogue = False
            elif in_dialogue:
                dialogue_chars += 1

        dialogue_ratio = dialogue_chars / total_chars

        if dialogue_ratio > 0.7:
            result.add_issue(
                type="excessive_dialogue",
                severity="medium",
                location="全章",
                description=(
                    f"对话占比{dialogue_ratio*100:.0f}%(建议30-60%)，"
                    f"缺少叙述、描写和心理活动"
                ),
                suggestion="增加场景描写、角色心理活动和动作描写，减少纯对话",
            )
        elif dialogue_ratio < 0.1 and total_chars > 2000:
            result.add_issue(
                type="insufficient_dialogue",
                severity="low",
                location="全章",
                description=(
                    f"对话占比仅{dialogue_ratio*100:.0f}%(建议30-60%)，"
                    f"大段叙述可能缺乏生动感"
                ),
                suggestion="适当增加角色对话以增强场景生动感和代入感",
            )

    def _check_information_density(
        self,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Check information density waves.

        Too much new information without breaks = overwhelming.
        Too little information = boring/filler.
        """
        if len(paragraphs) < 5:
            return

        # Estimate information density by counting unique entities per paragraph
        info_densities: list[float] = []
        for para in paragraphs:
            # Count unique CJK word-like patterns (crude but effective)
            unique_terms = set(re.findall(r'[\u4e00-\u9fff]{2,4}', para))
            density = len(unique_terms) / max(len(para) / 10, 1)
            info_densities.append(density)

        # Check for sustained high density (infodump)
        high_density_streak = 0
        for density in info_densities:
            if density > 3.0:
                high_density_streak += 1
                if high_density_streak >= 5:
                    result.add_issue(
                        type="infodump",
                        severity="medium",
                        location="多处",
                        description=(
                            f"连续{high_density_streak}段信息密度过高，"
                            f"疑似信息倾泻(infodump)"
                        ),
                        suggestion="将密集信息分散到对话、行动中自然传达，避免大段说明文",
                    )
                    break
            else:
                high_density_streak = 0

    def _check_opening_closing(
        self,
        paragraphs: list[str],
        result: CheckResult,
    ) -> None:
        """Check opening engagement and closing hook."""
        if not paragraphs:
            return

        # Check opening (first 20%)
        opening_count = max(1, len(paragraphs) // 5)
        opening = " ".join(paragraphs[:opening_count])

        # Opening should not be pure narration with no hook
        has_dialogue_in_opening = any(
            marker in opening for marker in ['"', '"', '「']
        )
        has_action_in_opening = any(
            word in opening
            for word in ["突然", "忽然", "一声", "砰", "轰", "啊", "快"]
        )
        has_question_in_opening = "？" in opening or "?" in opening

        engagement_score = sum([
            has_dialogue_in_opening,
            has_action_in_opening,
            has_question_in_opening,
        ])

        if engagement_score == 0 and len(opening) > 200:
            result.add_issue(
                type="weak_opening",
                severity="low",
                location="开头",
                description=(
                    "开头缺少吸引元素(无对话、无动作、无悬念)，"
                    "纯叙述性开场可能导致读者流失"
                ),
                suggestion="在开头20%处加入对话、冲突或悬念钩子",
            )

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
