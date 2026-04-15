"""
Three-Strand Weave Tracking Service

Monitors the balance of three narrative strands across chapters:

- Quest (主线/任务线): Main plot advancement, battles, challenges, goals.
  Readers want to see progress toward the central objective.

- Fire (感情/人物线): Emotional and relationship developments, character
  growth, interpersonal conflict. Keeps readers emotionally invested.

- Constellation (世界观/设定线): Worldbuilding revelations, power system
  details, lore discoveries. Maintains sense of wonder and depth.

The ideal novel weaves all three strands, never letting any go dormant
for too long. This service tracks when each strand last appeared and
warns when balance is off.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.project import Chapter, Volume
from app.services.context_pack import StrandTracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# Strand detection keywords
# ---------------------------------------------------------------

QUEST_KEYWORDS: list[str] = [
    # Action / battle
    "战斗", "打斗", "对决", "交锋", "出手", "攻击", "防御", "躲避",
    "击败", "杀", "战胜", "逃跑", "追击",
    # Goal / mission
    "任务", "目标", "使命", "寻找", "到达", "完成", "挑战",
    "闯关", "副本", "boss",
    # Competition / conflict
    "比赛", "竞争", "较量", "争夺", "冲突", "危机", "困境",
    "突破", "晋级", "升级", "突围",
]

FIRE_KEYWORDS: list[str] = [
    # Emotion
    "心疼", "感动", "难过", "开心", "高兴", "愤怒", "悲伤",
    "哭", "泪", "笑", "心跳", "脸红",
    # Relationship
    "喜欢", "爱", "恨", "思念", "想念", "牵挂", "担心",
    "信任", "背叛", "原谅", "和解", "告白", "分别", "重逢",
    # Character growth
    "成长", "改变", "领悟", "反思", "理解", "承担", "选择",
    "勇气", "决心", "放下", "释怀",
    # Interpersonal
    "朋友", "兄弟", "师徒", "恋人", "对手", "知己",
    "拥抱", "握手", "对视", "倾诉",
]

CONSTELLATION_KEYWORDS: list[str] = [
    # Worldbuilding
    "修炼体系", "境界", "灵气", "法则", "规则", "天道",
    "历史", "传说", "古老", "遗迹", "秘境", "禁地",
    # Power system
    "功法", "秘术", "阵法", "丹药", "法宝", "灵器",
    "血脉", "天赋", "技能", "属性",
    # Lore / discovery
    "发现", "秘密", "真相", "起源", "来历",
    "地图", "大陆", "势力", "宗门", "王朝", "帝国",
    # New concepts
    "据说", "传闻", "古籍记载", "从未见过", "闻所未闻",
]


@dataclass
class StrandAnalysis:
    """Detailed analysis of strand presence in a chapter."""

    chapter_idx: int
    quest_score: float = 0.0
    fire_score: float = 0.0
    constellation_score: float = 0.0
    dominant: str = "quest"
    quest_keywords_found: list[str] = field(default_factory=list)
    fire_keywords_found: list[str] = field(default_factory=list)
    constellation_keywords_found: list[str] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        return self.quest_score + self.fire_score + self.constellation_score


class StrandTrackerService:
    """Service that tracks Quest/Fire/Constellation strand balance.

    Analyzes chapter content (text or summary) to determine which
    strands are active, and produces warnings when any strand has
    been dormant too long.
    """

    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db

    async def _get_db(self) -> AsyncSession:
        if self._db is not None:
            return self._db
        return async_session_factory()

    def analyze_text(self, chapter_idx: int, text: str) -> StrandAnalysis:
        """Analyze a single chapter's text for strand presence.

        Args:
            chapter_idx: The chapter index.
            text: Full text or summary of the chapter.

        Returns:
            StrandAnalysis with scores and detected keywords.
        """
        analysis = StrandAnalysis(chapter_idx=chapter_idx)

        if not text:
            return analysis

        text_lower = text.lower()

        # Count keyword matches for each strand
        for kw in QUEST_KEYWORDS:
            count = text_lower.count(kw)
            if count > 0:
                analysis.quest_score += count
                analysis.quest_keywords_found.append(kw)

        for kw in FIRE_KEYWORDS:
            count = text_lower.count(kw)
            if count > 0:
                analysis.fire_score += count
                analysis.fire_keywords_found.append(kw)

        for kw in CONSTELLATION_KEYWORDS:
            count = text_lower.count(kw)
            if count > 0:
                analysis.constellation_score += count
                analysis.constellation_keywords_found.append(kw)

        # Normalize scores by text length
        text_len = max(len(text), 1)
        norm_factor = 1000.0 / text_len
        analysis.quest_score *= norm_factor
        analysis.fire_score *= norm_factor
        analysis.constellation_score *= norm_factor

        # Determine dominant strand
        scores = {
            "quest": analysis.quest_score,
            "fire": analysis.fire_score,
            "constellation": analysis.constellation_score,
        }
        analysis.dominant = max(scores, key=lambda k: scores[k])

        return analysis

    async def analyze_strands(
        self,
        project_id: str | UUID,
        current_chapter_idx: int,
        lookback: int = 20,
    ) -> StrandTracker:
        """Analyze strand balance across recent chapters.

        Reads chapter summaries (or content if no summary) from the
        database and computes when each strand last appeared.

        Args:
            project_id: The project to analyze.
            current_chapter_idx: The current chapter index.
            lookback: How many previous chapters to analyze.

        Returns:
            StrandTracker with last appearance data and warnings.
        """
        db = await self._get_db()
        pid = str(project_id)

        tracker = StrandTracker()

        try:
            # Get recent chapters
            result = await db.execute(
                select(Chapter.chapter_idx, Chapter.summary, Chapter.content_text)
                .join(Volume, Chapter.volume_id == Volume.id)
                .where(
                    Volume.project_id == pid,
                    Chapter.chapter_idx <= current_chapter_idx,
                    Chapter.chapter_idx > max(0, current_chapter_idx - lookback),
                )
                .order_by(Chapter.chapter_idx.asc())
            )
            chapters = result.all()

            if not chapters:
                return tracker

            # Analyze each chapter
            analyses: list[StrandAnalysis] = []
            for row in chapters:
                # Prefer summary, fall back to content
                text = row.summary or (row.content_text or "")[:1000]
                if not text:
                    continue
                analysis = self.analyze_text(row.chapter_idx, text)
                analyses.append(analysis)

            # Find last chapter where each strand was dominant or significant
            quest_threshold = 1.0
            fire_threshold = 1.0
            constellation_threshold = 0.5

            for analysis in analyses:
                if analysis.quest_score > quest_threshold:
                    tracker.last_quest_chapter = analysis.chapter_idx
                if analysis.fire_score > fire_threshold:
                    tracker.last_fire_chapter = analysis.chapter_idx
                if analysis.constellation_score > constellation_threshold:
                    tracker.last_constellation_chapter = analysis.chapter_idx

            # Set current dominant from the most recent chapter
            if analyses:
                tracker.current_dominant = analyses[-1].dominant

        except Exception as e:
            logger.warning("Strand analysis failed: %s", e)

        return tracker

    async def get_strand_history(
        self,
        project_id: str | UUID,
        from_chapter: int = 0,
        to_chapter: int | None = None,
    ) -> list[StrandAnalysis]:
        """Get full strand analysis history for visualization.

        Args:
            project_id: The project to analyze.
            from_chapter: Start chapter index.
            to_chapter: End chapter index (None = latest).

        Returns:
            List of StrandAnalysis objects, one per chapter.
        """
        db = await self._get_db()
        pid = str(project_id)

        try:
            query = (
                select(Chapter.chapter_idx, Chapter.summary, Chapter.content_text)
                .join(Volume, Chapter.volume_id == Volume.id)
                .where(
                    Volume.project_id == pid,
                    Chapter.chapter_idx >= from_chapter,
                )
                .order_by(Chapter.chapter_idx.asc())
            )

            if to_chapter is not None:
                query = query.where(Chapter.chapter_idx <= to_chapter)

            result = await db.execute(query)
            chapters = result.all()

            analyses: list[StrandAnalysis] = []
            for row in chapters:
                text = row.summary or (row.content_text or "")[:1000]
                analysis = self.analyze_text(row.chapter_idx, text)
                analyses.append(analysis)

            return analyses

        except Exception as e:
            logger.warning("Strand history query failed: %s", e)
            return []

    def get_balance_recommendations(
        self,
        tracker: StrandTracker,
        current_chapter: int,
        genre: str = "",
    ) -> list[str]:
        """Generate specific recommendations for strand balance.

        Args:
            tracker: Current strand tracker state.
            current_chapter: The chapter being planned.
            genre: The genre of the novel.

        Returns:
            List of recommendation strings.
        """
        recommendations: list[str] = []
        warnings = tracker.get_warnings(current_chapter)

        quest_gap = current_chapter - tracker.last_quest_chapter
        fire_gap = current_chapter - tracker.last_fire_chapter
        constellation_gap = current_chapter - tracker.last_constellation_chapter

        # Prioritize the most neglected strand
        gaps = [
            ("quest", quest_gap, 5),
            ("fire", fire_gap, 10),
            ("constellation", constellation_gap, 15),
        ]
        gaps.sort(key=lambda x: x[1] / x[2], reverse=True)

        most_neglected = gaps[0][0]

        if most_neglected == "quest" and quest_gap > 3:
            recommendations.append(
                "建议本章推进主线: 设计一个明确的目标/挑战/冲突"
            )
            if genre in ("玄幻", "仙侠"):
                recommendations.append(
                    "可安排: 境界突破的机遇/对手出现/新任务获取"
                )
            elif genre == "都市":
                recommendations.append(
                    "可安排: 商业对决/能力展示/关键抉择"
                )

        if most_neglected == "fire" and fire_gap > 5:
            recommendations.append(
                "建议本章加入感情戏: 角色互动/关系变化/内心成长"
            )
            if genre == "言情":
                recommendations.append(
                    "可安排: CP互动/误会化解/感情升温"
                )
            else:
                recommendations.append(
                    "可安排: 队友对话/师徒传承/敌人背景揭示(增添人情味)"
                )

        if most_neglected == "constellation" and constellation_gap > 8:
            recommendations.append(
                "建议本章展示世界观: 新设定/新区域/力量体系细节"
            )
            if genre in ("玄幻", "仙侠"):
                recommendations.append(
                    "可安排: 新境界体验/宗门秘辛/上古传说/禁地探索"
                )
            elif genre == "科幻":
                recommendations.append(
                    "可安排: 技术展示/社会结构/新物种/异星文明"
                )

        # General balance advice
        if len(warnings) >= 2:
            recommendations.append(
                "多条线索严重失衡，建议本章设计一个融合场景:"
                "在推进主线的战斗/事件中穿插角色情感互动，同时展示新设定"
            )

        return recommendations
