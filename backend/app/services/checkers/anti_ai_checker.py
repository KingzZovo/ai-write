"""
Anti-AI Writing Trace Checker

Detects common AI writing artifacts in Chinese web novel text:
- High-frequency AI words (璀璨/瑰丽/不禁/油然而生...)
- Four-character idiom density
- "的" density
- Sentence pattern monotony
- Mechanical parallelism
- Excessive formality
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from app.services.checkers.base import BaseChecker, CheckResult
from app.services.context_pack import ContextPack

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# AI writing trace word lists
# ---------------------------------------------------------------

AI_WORDS: list[str] = [
    # Transitional/structural (AI-flavored)
    "此外", "然而", "值得注意的是", "需要强调的是", "不可忽视",
    "与此同时", "毋庸置疑", "诚然", "显而易见", "不言而喻",
    "总而言之", "综上所述", "总的来说",
    # AI-preferred verbs
    "彰显", "诠释", "赋能", "映射", "折射",
    "承载", "凝聚", "汇聚", "蕴含", "涌现",
    "践行", "赋予", "传递", "构建",
    # AI-preferred emotional expressions
    "不禁", "油然而生", "心潮澎湃", "感慨万千",
    "肃然起敬", "心生敬意", "由衷地",
    "内心深处", "灵魂深处",
    # AI-preferred adjectives/descriptions
    "璀璨", "瑰丽", "熠熠生辉", "光芒万丈",
    "波澜壮阔", "气势磅礴", "蔚为壮观",
    "深邃", "厚重", "醇厚",
    "恢弘", "宏大", "壮丽",
    # AI-preferred sentence structures
    "如同", "宛如", "恰似", "犹如",  # overuse of similes
    "仿佛一幅", "宛若一首", "犹如一道",
    # Filler expressions
    "说实话", "不得不说", "可以说", "毫不夸张地说",
]

# Extremely common AI phrases (higher weight)
AI_PHRASES: list[str] = [
    "映入眼帘", "不禁感叹", "心中一动", "不由得",
    "脑海中浮现", "心中暗道", "嘴角微微上扬",
    "眼中闪过一丝", "目光中透露出",
    "空气中弥漫着", "阳光洒在",
    "仿佛在诉说", "似乎在告诉",
    "一股莫名的", "一种说不出的",
    "心中五味杂陈", "百感交集",
]

# Four-character idiom pattern (CJK 4-char combinations)
FOUR_CHAR_PATTERN = re.compile(r'[\u4e00-\u9fff]{4}')

# Common four-character idioms that are AI favorites
AI_FAVORITE_IDIOMS = [
    "目光灼灼", "心如止水", "势如破竹", "锲而不舍",
    "坚定不移", "毫不犹豫", "义无反顾", "全力以赴",
    "刻骨铭心", "浑然天成", "如沐春风", "焕然一新",
    "栩栩如生", "淋漓尽致", "恰到好处", "引人入胜",
    "耐人寻味", "发人深省", "令人叹服", "令人动容",
]

# Threshold constants
DE_DENSITY_THRESHOLD = 0.08  # "的" should not exceed 8% of total chars
AI_WORD_DENSITY_THRESHOLD = 0.015  # AI words should be < 1.5% of text
FOUR_CHAR_DENSITY_THRESHOLD = 0.06  # Four-char idioms < 6% of text
SIMILE_DENSITY_THRESHOLD = 0.005  # Simile markers < 0.5%

# ---------------------------------------------------------------
# PR-AI1 — forbidden hallucinated compounds + naming directive.
# ---------------------------------------------------------------
# Terms observed in E2E runs (PID 310c1f9a V1 CH2) where the LLM
# manufactured non-words. Update this list as new patterns surface.
FORBIDDEN_HALLUCINATION_TERMS: list[str] = [
    "黄铜怎表", "怎表", "屃门", "黑贬表", "黄表",
    # Generic 'X怎表' / 'X屃X' patterns that are usually nonsense.
]

# Heuristic: 「[中文单字] + 怎/屃/表/门 」 类似起来像术语但实际不是词的并联体。
SUSPICIOUS_COMPOUND_PATTERN = re.compile(r"[\u4e00-\u9fff](怎表|屃门|表門|门怎)")

NAMING_DIRECTIVE: str = (
    "【PR-AI1 命名与词汇硬约束】\n"
    "· 自创器物/术语/阵营/宗门名须使用现代汉语真实词汇或含义可推测的复合词\n"
    "  例：「血玉牌」「潮汐罗盘」「谢原宗」。\n"
    "· 严禁生造单字拼凑且含义不明的「器物」：\n"
    "  反例：「黄铜怎表」「屃门」「黄表」。\n"
    "· 主角/配角/门派/地名 统一使用项目 glossary 中已有的名称\n"
    "  （角色卡 / 世界规则 / outline.character），不可临时重命名。\n"
    "· 首次出现的全新专有名词须在本章给出一句背景说明，不可裸露使用。\n"
    "· 同一器物/术语在本章中只用一个正名，不要多名交替。\n"
)

# ---------------------------------------------------------------
# PR-STY1 — style v9 节奏 / 留白 / 信息密度 directives.
# ---------------------------------------------------------------
# Each entry is rendered as a bullet under 『创作指导』 in the
# generation prompt. Keep them imperative + measurable so the model
# can self-check; long prose is wasted tokens.
STYLE_V9_DIRECTIVES: list[str] = [
    "【PR-STY1 节奏】本章开头不要检讨上章、不要总起背景；\n"
    "　　从具体动作、感官鑲点或对话进入，头三句不出现人名可以。",
    "【PR-STY1 留白】段落长短交错：每 6-8 个长段必须出现一次 1-2 句的短段\n"
    "　　（如人物心跳、自言自语、外部闪现），避免从头到尾均匀长段带来的 AI 调。",
    "【PR-STY1 信息密度】每 200-300 字推进一个新 beat（动作、冲突、揭露）；\n"
    "　　不可连续 3 个段落仅描写心理/环境/类似公式表达。",
    "【PR-STY1 句式】避免连续三句以上「他/她/主角名 + 动词」平补句型；\n"
    "　　可穿插环境备忘、人物互动、心理独白。",
    "【PR-STY1 在场】主角出场必须被环境迫近一次（被看、被听、被接触、被闻到），\n"
    "　　不要全程以上帝视角描述。",
]


class AntiAIChecker(BaseChecker):
    """AI writing trace detection.

    Detects patterns commonly found in AI-generated Chinese text that
    give away the artificial origin. These traces make the text feel
    generic, overly formal, or machine-like to human readers.
    """

    name = "anti_ai"

    async def check(
        self,
        chapter_text: str,
        context: ContextPack,
    ) -> CheckResult:
        result = CheckResult(checker_name=self.name)

        if not chapter_text.strip():
            return result

        total_chars = len(chapter_text)
        if total_chars < 100:
            return result

        # 1. AI word frequency check
        self._check_ai_words(chapter_text, total_chars, result)

        # 2. AI phrase check
        self._check_ai_phrases(chapter_text, total_chars, result)

        # 3. "的" density check
        self._check_de_density(chapter_text, total_chars, result)

        # 4. Four-character idiom density
        self._check_four_char_density(chapter_text, total_chars, result)

        # 5. Sentence pattern monotony
        self._check_sentence_pattern_monotony(chapter_text, result)

        # 6. Simile overuse
        self._check_simile_overuse(chapter_text, total_chars, result)

        # 7. Mechanical parallelism
        self._check_mechanical_parallelism(chapter_text, result)

        # 8. Formality level
        self._check_formality(chapter_text, result)

        result.score = self._compute_score(result)
        return result

    def _check_ai_words(
        self,
        text: str,
        total_chars: int,
        result: CheckResult,
    ) -> None:
        """Check frequency of known AI-favored words."""
        found: dict[str, int] = {}
        total_ai_chars = 0

        for word in AI_WORDS:
            count = text.count(word)
            if count > 0:
                found[word] = count
                total_ai_chars += count * len(word)

        ai_density = total_ai_chars / total_chars

        if ai_density > AI_WORD_DENSITY_THRESHOLD:
            severity = "high" if ai_density > AI_WORD_DENSITY_THRESHOLD * 2 else "medium"
            # Sort by frequency
            top_words = sorted(found.items(), key=lambda x: x[1], reverse=True)[:8]
            word_list = ", ".join(f"'{w}'({c}次)" for w, c in top_words)

            result.add_issue(
                type="ai_word_density",
                severity=severity,
                location="全文",
                description=(
                    f"AI常用词密度{ai_density*100:.2f}%(阈值{AI_WORD_DENSITY_THRESHOLD*100}%)。"
                    f"高频词: {word_list}"
                ),
                suggestion=(
                    "替换AI风格用词为更自然的表达:\n"
                    "- '不禁' -> 直接描写动作/表情\n"
                    "- '油然而生' -> 具体描写情感来源\n"
                    "- '璀璨' -> 使用更具体的光线描写\n"
                    "- '此外/然而' -> 用动作或对话自然过渡"
                ),
            )
        elif found and ai_density > AI_WORD_DENSITY_THRESHOLD * 0.5:
            top_words = sorted(found.items(), key=lambda x: x[1], reverse=True)[:5]
            word_list = ", ".join(f"'{w}'({c}次)" for w, c in top_words)
            result.add_issue(
                type="ai_word_warning",
                severity="low",
                location="全文",
                description=f"检测到AI常用词(密度尚可): {word_list}",
                suggestion="注意控制这些词的使用频率",
            )

    def _check_ai_phrases(
        self,
        text: str,
        total_chars: int,
        result: CheckResult,
    ) -> None:
        """Check for AI-characteristic phrases."""
        found: list[str] = []
        for phrase in AI_PHRASES:
            count = text.count(phrase)
            if count > 0:
                found.extend([phrase] * count)

        if len(found) > 3:
            unique_found = list(set(found))
            result.add_issue(
                type="ai_phrases",
                severity="medium" if len(found) > 6 else "low",
                location="多处",
                description=(
                    f"检测到{len(found)}处AI特征短语: "
                    f"{', '.join(repr(p) for p in unique_found[:6])}"
                ),
                suggestion=(
                    "用更具体、个性化的表达替换:\n"
                    "- '映入眼帘' -> 直接描述看到的具体事物\n"
                    "- '嘴角微微上扬' -> 用更有个性的笑容描写\n"
                    "- '心中暗道' -> 直接写想法，减少'心中'前缀"
                ),
            )

    def _check_de_density(
        self,
        text: str,
        total_chars: int,
        result: CheckResult,
    ) -> None:
        """Check "的" particle density.

        AI text tends to overuse "的" for modification, leading to
        long and clunky modifier chains.
        """
        de_count = text.count("的")
        de_density = de_count / total_chars

        if de_density > DE_DENSITY_THRESHOLD:
            result.add_issue(
                type="de_overuse",
                severity="medium" if de_density > DE_DENSITY_THRESHOLD * 1.5 else "low",
                location="全文",
                description=(
                    f"'的'字使用密度{de_density*100:.2f}%"
                    f"(阈值{DE_DENSITY_THRESHOLD*100}%)，"
                    f"共{de_count}个'的'字。"
                    f"过多的'的'会让文字拖沓"
                ),
                suggestion=(
                    "减少'的'字使用:\n"
                    "- '美丽的花朵' -> '繁花'\n"
                    "- '他的心中的想法' -> '他心想'\n"
                    "- 删除不必要的修饰语"
                ),
            )

        # Check for "的的的" chains (multiple consecutive 的)
        de_chains = re.findall(r'[\u4e00-\u9fff]+的[\u4e00-\u9fff]+的[\u4e00-\u9fff]+的', text)
        if len(de_chains) > 3:
            result.add_issue(
                type="de_chain",
                severity="medium",
                location="多处",
                description=(
                    f"检测到{len(de_chains)}处三连'的'修饰链，"
                    f"示例: '{de_chains[0][:20]}...'"
                ),
                suggestion="拆分长修饰链为短句或使用动词替代部分修饰",
            )

    def _check_four_char_density(
        self,
        text: str,
        total_chars: int,
        result: CheckResult,
    ) -> None:
        """Check four-character idiom density.

        AI text often overuses four-character idioms (chengyu) which
        makes the prose feel formulaic.
        """
        matches = FOUR_CHAR_PATTERN.findall(text)
        # Filter to only count actual idiom-like patterns (not random 4-char sequences)
        # by checking against known AI favorites
        idiom_count = 0
        found_idioms: list[str] = []

        for match in matches:
            if match in AI_FAVORITE_IDIOMS:
                idiom_count += 1
                found_idioms.append(match)

        # Also estimate general four-char idiom density
        # (four chars that appear as a unit, often with parallel structure)
        four_char_ratio = len(matches) * 4 / total_chars

        if four_char_ratio > FOUR_CHAR_DENSITY_THRESHOLD:
            result.add_issue(
                type="four_char_overuse",
                severity="medium",
                location="全文",
                description=(
                    f"四字短语密度{four_char_ratio*100:.1f}%"
                    f"(阈值{FOUR_CHAR_DENSITY_THRESHOLD*100}%)，"
                    f"文字过于成语化"
                ),
                suggestion="减少四字成语使用，用更口语化、具体的描写替代",
            )

        if idiom_count > 5:
            result.add_issue(
                type="ai_favorite_idioms",
                severity="low",
                location="多处",
                description=(
                    f"检测到{idiom_count}个AI高频成语: "
                    f"{', '.join(set(found_idioms[:6]))}"
                ),
                suggestion="替换为更有特色的表达或具体场景描写",
            )

    def _check_sentence_pattern_monotony(
        self,
        text: str,
        result: CheckResult,
    ) -> None:
        """Check for sentence pattern monotony.

        AI text often falls into repetitive sentence structures:
        - Subject + 的 + Noun pattern
        - 他/她 开头重复
        - 是...的 pattern
        """
        sentences = re.split(r'[。！？]', text)
        sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 3]

        if len(sentences) < 10:
            return

        # Check for repeated sentence openings
        openings = [s[:2] for s in sentences if len(s) >= 2]
        opening_counts = Counter(openings)

        most_common_opening, count = opening_counts.most_common(1)[0]
        opening_ratio = count / len(sentences)

        if opening_ratio > 0.25 and count >= 5:
            result.add_issue(
                type="repeated_opening",
                severity="medium",
                location="全文",
                description=(
                    f"有{count}个句子以'{most_common_opening}'开头"
                    f"(占{opening_ratio*100:.0f}%)，句式单一"
                ),
                suggestion=(
                    f"减少以'{most_common_opening}'开头的句子，"
                    f"使用动作、对话、场景描写等多种开头方式"
                ),
            )

        # Check for "他/她" opening monotony specifically
        ta_openings = sum(
            1 for s in sentences
            if s and s[0] in "他她它"
        )
        ta_ratio = ta_openings / len(sentences)

        if ta_ratio > 0.3 and ta_openings >= 8:
            result.add_issue(
                type="pronoun_opening_monotony",
                severity="medium",
                location="全文",
                description=(
                    f"有{ta_openings}个句子以人称代词开头"
                    f"(占{ta_ratio*100:.0f}%)，叙述视角单调"
                ),
                suggestion="变换句式开头：用动作、感受、环境描写替代代词开头",
            )

    def _check_simile_overuse(
        self,
        text: str,
        total_chars: int,
        result: CheckResult,
    ) -> None:
        """Check for overuse of simile structures."""
        simile_markers = ["如同", "宛如", "恰似", "犹如", "好似", "仿佛", "好像"]
        simile_count = sum(text.count(m) for m in simile_markers)
        simile_density = simile_count * 2 / total_chars  # 2 chars per marker avg

        if simile_density > SIMILE_DENSITY_THRESHOLD and simile_count > 5:
            result.add_issue(
                type="simile_overuse",
                severity="medium",
                location="全文",
                description=(
                    f"比喻/类比使用{simile_count}次，过于频繁。"
                    f"AI文常见的'宛如/犹如/仿佛'堆砌"
                ),
                suggestion="减少明喻，使用暗喻或直接描写，让比喻更精准有力",
            )

    def _check_mechanical_parallelism(
        self,
        text: str,
        result: CheckResult,
    ) -> None:
        """Check for mechanical parallelism (AI-style repetitive structures).

        Detects patterns like: A是B，C是D，E是F (mechanical listing)
        """
        # Look for repetitive "X是Y" patterns
        shi_patterns = re.findall(r'[\u4e00-\u9fff]{2,8}是[\u4e00-\u9fff]{2,8}', text)
        if len(shi_patterns) > 5:
            # Check if they appear close together (within 200 chars)
            positions = [m.start() for m in re.finditer(
                r'[\u4e00-\u9fff]{2,8}是[\u4e00-\u9fff]{2,8}', text
            )]
            clustered = 0
            for i in range(1, len(positions)):
                if positions[i] - positions[i - 1] < 100:
                    clustered += 1

            if clustered >= 3:
                result.add_issue(
                    type="mechanical_parallelism",
                    severity="low",
                    location="多处",
                    description=(
                        f"检测到{clustered}处密集的平行结构，"
                        f"排比过于机械化"
                    ),
                    suggestion="避免连续使用相同句式，用不同句型表达并列内容",
                )

    def _check_formality(
        self,
        text: str,
        result: CheckResult,
    ) -> None:
        """Check for excessive formality (AI text tends to be overly formal)."""
        formal_markers = [
            "值得一提的是", "不得不提", "首先", "其次", "最后",
            "一方面", "另一方面", "总而言之",
            "可以说是", "从某种意义上说",
        ]

        formal_count = sum(text.count(m) for m in formal_markers)
        if formal_count > 3:
            result.add_issue(
                type="excessive_formality",
                severity="medium" if formal_count > 6 else "low",
                location="全文",
                description=(
                    f"检测到{formal_count}处论文式/公文式用语，"
                    f"小说文本不应有议论文的结构词"
                ),
                suggestion="删除论文式过渡语，用故事内的动作和对话推进叙述",
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
