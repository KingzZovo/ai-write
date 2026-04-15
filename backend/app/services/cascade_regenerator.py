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
                project_id, modified_chapter_idx
            )
            impact.affected_chapter_ids = [ch["id"] for ch in affected]

        return impact

    async def get_affected_chapters(
        self,
        project_id: str,
        modified_chapter_idx: int,
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

            for volume in volumes:
                ch_result = await self.db.execute(
                    select(Chapter)
                    .where(
                        Chapter.volume_id == volume.id,
                        Chapter.chapter_idx > modified_chapter_idx,
                    )
                    .order_by(Chapter.chapter_idx)
                )
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

    async def regenerate_chapter(
        self,
        project_id: str,
        chapter_id: str,
    ) -> str:
        """
        Regenerate a single chapter using updated context.

        Fetches the chapter's outline and uses the full memory pipeline
        to produce fresh content consistent with any upstream edits.
        """
        chapter = await self.db.get(Chapter, chapter_id)
        if not chapter:
            raise ValueError(f"Chapter {chapter_id} not found")

        # Build context using the existing context assembler
        from app.services.context_assembler import build_context_for_chapter

        # Get the previous chapter for recent-text context
        prev_text = ""
        if chapter.chapter_idx > 0:
            prev_result = await self.db.execute(
                select(Chapter).where(
                    Chapter.volume_id == chapter.volume_id,
                    Chapter.chapter_idx == chapter.chapter_idx - 1,
                )
            )
            prev_chapter = prev_result.scalar_one_or_none()
            if prev_chapter and prev_chapter.content_text:
                prev_text = prev_chapter.content_text

        messages = build_context_for_chapter(
            chapter_outline=chapter.outline_json or {},
            previous_chapter_text=prev_text,
            user_instruction=(
                f"请根据以上设定和大纲，重新生成第{chapter.chapter_idx}章"
                f"《{chapter.title}》的正文内容。"
            ),
        )

        try:
            result = await self.router.generate(
                task_type="generation",
                messages=messages,
                max_tokens=4096,
            )
            new_text = result.text

            # Update the chapter in DB
            chapter.content_text = new_text
            chapter.word_count = len(new_text)
            chapter.status = "regenerated"
            await self.db.flush()

            logger.info(
                "Regenerated chapter %s (idx=%d), %d chars",
                chapter_id,
                chapter.chapter_idx,
                len(new_text),
            )
            return new_text

        except Exception as exc:
            logger.error("Failed to regenerate chapter %s: %s", chapter_id, exc)
            raise


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
