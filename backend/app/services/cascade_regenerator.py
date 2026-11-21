"""
Cascade Regeneration Service

When a chapter is modified, determines which subsequent chapters
are affected and need regeneration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, Volume
from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)


IMPACT_ANALYSIS_PROMPT = """\
你是一个小说剧情分析专家。请比较以下章节的旧版本和新版本，分析修改的影响。

旧版本：
{old_text}

新版本：
{new_text}

请用纯 JSON 输出：
{{
  "changed_plot_points": ["发生变化的剧情要素"],
  "changed_character_states": ["角色状态变化，格式：角色名-变化描述"],
  "changed_relationships": ["关系变化，格式：角色A-角色B-变化描述"],
  "severity": "minor|moderate|major",
  "summary": "修改影响的简述（50字以内）"
}}
"""


@dataclass
class CascadeImpact:
    """Result of cascade impact analysis."""

    changed_plot_points: list[str] = field(default_factory=list)
    changed_character_states: list[str] = field(default_factory=list)
    changed_relationships: list[str] = field(default_factory=list)
    severity: str = "minor"  # minor, moderate, major
    summary: str = ""
    affected_chapter_ids: list[str] = field(default_factory=list)


class CascadeRegenerator:
    """Analyzes chapter edits and determines downstream regeneration needs."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.router = get_model_router()
        # 被改章所属的**卷号**;调用方知道时应当赋值,否则影响范围只能按卷内号推断
        # (跨卷同号会漏判,见 get_affected_chapters 的注释)。
        self._modified_volume_idx: int | None = None

    async def analyze_impact(
        self,
        project_id: str,
        modified_chapter_idx: int,
        old_text: str,
        new_text: str,
    ) -> CascadeImpact:
        """
        Analyze what downstream chapters are affected by a chapter edit.

        Uses LLM to compare old vs new text and identify:
        - Changed plot points
        - Changed character states
        - Changed relationships

        Then checks which subsequent chapters reference these elements.
        """
        impact = CascadeImpact()

        # Quick check: if texts are identical, no impact
        if old_text.strip() == new_text.strip():
            return impact

        # Use LLM to analyze differences
        prompt = IMPACT_ANALYSIS_PROMPT.format(
            old_text=old_text[:3000],
            new_text=new_text[:3000],
        )

        try:
            result = await self.router.generate(
                task_type="extraction",
                messages=[
                    {"role": "system", "content": "你是一个小说分析助手，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1024,
            )

            data = _parse_json(result.text)
            impact.changed_plot_points = data.get("changed_plot_points", [])
            impact.changed_character_states = data.get("changed_character_states", [])
            impact.changed_relationships = data.get("changed_relationships", [])
            impact.severity = data.get("severity", "minor")
            impact.summary = data.get("summary", "")

        except Exception as exc:
            logger.warning("Impact analysis LLM call failed: %s", exc)
            # Fallback: treat any non-trivial edit as moderate
            impact.severity = "moderate"
            impact.summary = "无法自动分析影响，建议人工检查后续章节"

        # Determine affected chapters
        if impact.severity != "minor":
            affected = await self.get_affected_chapters(
                project_id, modified_chapter_idx, self._modified_volume_idx
            )
            impact.affected_chapter_ids = [ch["id"] for ch in affected]

        return impact

    async def get_affected_chapters(
        self,
        project_id: str,
        modified_chapter_idx: int,
        modified_volume_idx: int | None = None,
    ) -> list[dict]:
        """
        Get list of chapters that may need regeneration.

        Returns all subsequent chapters in the same volume and following
        volumes that come after the modified chapter index.
        """
        affected: list[dict] = []

        try:
            # Get all volumes for this project
            vol_result = await self.db.execute(
                select(Volume).where(Volume.project_id == project_id).order_by(
                    Volume.volume_idx
                )
            )
            volumes = vol_result.scalars().all()

            # 2026-11-17:`chapter_idx` 是**卷内**序号,原先对所有卷都用
            # `chapter_idx > modified_chapter_idx` 过滤 —— 改第 1 卷第 5 章时,
            # 第 2 卷的 1–5 章会被漏判为"不受影响"。正确范围是:
            #   同一卷:卷内号大于被改章;之后的卷:**全部**;之前的卷:全部跳过。
            for volume in volumes:
                if modified_volume_idx is not None:
                    if volume.volume_idx < modified_volume_idx:
                        continue
                    if volume.volume_idx == modified_volume_idx:
                        stmt = (select(Chapter)
                                .where(Chapter.volume_id == volume.id,
                                       Chapter.chapter_idx > modified_chapter_idx)
                                .order_by(Chapter.chapter_idx))
                    else:
                        stmt = (select(Chapter)
                                .where(Chapter.volume_id == volume.id)
                                .order_by(Chapter.chapter_idx))
                else:
                    stmt = (select(Chapter)
                            .where(Chapter.volume_id == volume.id,
                                   Chapter.chapter_idx > modified_chapter_idx)
                            .order_by(Chapter.chapter_idx))
                ch_result = await self.db.execute(stmt)
                chapters = ch_result.scalars().all()

                for ch in chapters:
                    affected.append(
                        {
                            "id": str(ch.id),
                            "title": ch.title,
                            "chapter_idx": ch.chapter_idx,
                            "volume_id": str(volume.id),
                            "status": ch.status,
                        }
                    )

        except Exception as exc:
            logger.warning("Failed to get affected chapters: %s", exc)

        return affected



def _parse_json(text: str) -> dict:
    """Parse JSON from LLM output, handling markdown code blocks."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return json.loads(cleaned)
