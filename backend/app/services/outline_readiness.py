"""Outline-chain readiness checks for chapter prose generation.

The writing pipeline is intentionally hierarchical:

    book outline -> volume outline -> chapter outline -> chapter prose

This module centralizes the readiness logic so API gates and UI status use the
same rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, Outline, Volume


LAYER_LABELS = {
    "book": "全书大纲",
    "volume": "当前卷大纲",
    "chapter": "本章大纲",
}

DEGRADED_OUTLINE_STATUSES = {
    "degraded_structural_draft",
    "invalidated_degraded_draft",
    "invalidated_consistency_failed",
}


def is_degraded_outline(content: Any) -> bool:
    """Return True when an outline is only a diagnostic scaffold."""
    if not isinstance(content, dict):
        return False
    return str(content.get("_quality_status") or "") in DEGRADED_OUTLINE_STATUSES


def has_meaningful_outline_content(content: Any) -> bool:
    """Return True when an outline-like JSON payload carries usable content."""
    if not isinstance(content, dict) or not content:
        return False
    if is_degraded_outline(content):
        return False
    for value in content.values():
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and bool(value):
            return True
        if value not in (None, "", [], {}):
            return True
    return False


@dataclass(slots=True)
class OutlineLayerReadiness:
    ready: bool
    detail: str = ""
    outline_id: str | None = None
    title: str | None = None
    volume_idx: int | None = None
    chapter_idx: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "detail": self.detail,
            "outline_id": self.outline_id,
            "title": self.title,
            "volume_idx": self.volume_idx,
            "chapter_idx": self.chapter_idx,
        }


@dataclass(slots=True)
class OutlineReadinessReport:
    project_id: str
    volume_id: str | None = None
    volume_idx: int | None = None
    chapter_id: str | None = None
    chapter_idx: int | None = None
    layers: dict[str, OutlineLayerReadiness] = field(default_factory=dict)

    @property
    def missing_layers(self) -> list[str]:
        return [
            key
            for key in ("book", "volume", "chapter")
            if not self.layers.get(key, OutlineLayerReadiness(False)).ready
        ]

    @property
    def ready(self) -> bool:
        return not self.missing_layers

    def block_message(self) -> str:
        if self.ready:
            return ""
        labels = [LAYER_LABELS.get(layer, layer) for layer in self.missing_layers]
        return "缺少：" + "、".join(labels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "volume_id": self.volume_id,
            "volume_idx": self.volume_idx,
            "chapter_id": self.chapter_id,
            "chapter_idx": self.chapter_idx,
            "ready": self.ready,
            "missing_layers": self.missing_layers,
            "block_message": self.block_message(),
            "layers": {key: value.to_dict() for key, value in self.layers.items()},
        }


def _first_meaningful_outline(rows: list[Any]) -> Any | None:
    for row in rows:
        if has_meaningful_outline_content(getattr(row, "content_json", None)):
            return row
    return None


def _has_degraded_outline(rows: list[Any]) -> bool:
    return any(is_degraded_outline(getattr(row, "content_json", None)) for row in rows)


def _outline_title(content: Any) -> str | None:
    if not isinstance(content, dict):
        return None
    title = content.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    raw = content.get("raw_text")
    if isinstance(raw, str) and raw.strip():
        first_line = raw.strip().splitlines()[0].strip()
        return first_line[:60] if first_line else None
    return None


async def _resolve_target_chapter(
    db: AsyncSession,
    *,
    chapter_id: str | None,
    volume_id: str | None,
    chapter_idx: int | None,
) -> Chapter | None:
    if chapter_id:
        return await db.get(Chapter, chapter_id)
    if not volume_id or chapter_idx is None:
        return None
    result = await db.execute(
        select(Chapter).where(
            Chapter.volume_id == volume_id,
            Chapter.chapter_idx == chapter_idx,
        )
    )
    return result.scalar_one_or_none()


async def build_outline_readiness_report(
    db: AsyncSession,
    *,
    project_id: str,
    chapter_id: str | None = None,
    volume_id: str | None = None,
    chapter_idx: int | None = None,
) -> OutlineReadinessReport:
    """Load and evaluate outline-chain readiness for one chapter target."""
    chapter = await _resolve_target_chapter(
        db,
        chapter_id=chapter_id,
        volume_id=volume_id,
        chapter_idx=chapter_idx,
    )
    volume: Volume | None = None
    if chapter is not None:
        volume = await db.get(Volume, chapter.volume_id)
    elif volume_id:
        volume = await db.get(Volume, volume_id)

    resolved_chapter_id = str(getattr(chapter, "id", chapter_id)) if (chapter or chapter_id) else None
    resolved_volume_id = str(getattr(volume, "id", volume_id)) if (volume or volume_id) else None
    resolved_chapter_idx = (
        int(getattr(chapter, "chapter_idx"))
        if chapter is not None and getattr(chapter, "chapter_idx", None) is not None
        else chapter_idx
    )
    resolved_volume_idx = (
        int(getattr(volume, "volume_idx"))
        if volume is not None and getattr(volume, "volume_idx", None) is not None
        else None
    )

    book_rows = (
        await db.execute(
            select(Outline)
            .where(Outline.project_id == project_id, Outline.level == "book")
            .order_by(Outline.is_confirmed.desc(), Outline.created_at.asc())
        )
    ).scalars().all()
    book_rows_list = list(book_rows)
    book_outline = _first_meaningful_outline(book_rows_list)
    has_degraded_book = _has_degraded_outline(book_rows_list)

    volume_outline = None
    has_degraded_volume = False
    if resolved_volume_idx is not None:
        volume_rows = (
            await db.execute(
                select(Outline)
                .where(Outline.project_id == project_id, Outline.level == "volume")
                .order_by(Outline.created_at.desc())
            )
        ).scalars().all()
        for row in volume_rows:
            content = getattr(row, "content_json", None)
            if not isinstance(content, dict):
                continue
            try:
                row_volume_idx = int(content.get("volume_idx") or 0)
            except (TypeError, ValueError):
                row_volume_idx = 0
            if row_volume_idx == resolved_volume_idx and is_degraded_outline(content):
                has_degraded_volume = True
            if (
                row_volume_idx == resolved_volume_idx
                and has_meaningful_outline_content(content)
            ):
                volume_outline = row
                break

    chapter_ready = chapter is not None and has_meaningful_outline_content(
        getattr(chapter, "outline_json", None)
    )

    layers = {
        "book": OutlineLayerReadiness(
            ready=book_outline is not None,
            detail=(
                "已找到有效全书大纲"
                if book_outline is not None
                else "当前全书大纲是降级结构草稿，需要重新生成高质量版本"
                if has_degraded_book
                else "未找到有效全书大纲"
            ),
            outline_id=str(getattr(book_outline, "id", "")) if book_outline is not None else None,
            title=_outline_title(getattr(book_outline, "content_json", None)) if book_outline is not None else None,
        ),
        "volume": OutlineLayerReadiness(
            ready=volume_outline is not None,
            detail=(
                "已找到当前卷分卷大纲"
                if volume_outline is not None
                else "当前卷大纲是降级结构草稿，需要重新生成高质量版本"
                if has_degraded_volume
                else "未找到当前卷的分卷大纲"
            ),
            outline_id=str(getattr(volume_outline, "id", "")) if volume_outline is not None else None,
            title=_outline_title(getattr(volume_outline, "content_json", None)) if volume_outline is not None else None,
            volume_idx=resolved_volume_idx,
        ),
        "chapter": OutlineLayerReadiness(
            ready=chapter_ready,
            detail="已找到本章章节大纲" if chapter_ready else "本章章节大纲尚未生成",
            title=getattr(chapter, "title", None) if chapter is not None else None,
            chapter_idx=resolved_chapter_idx,
        ),
    }

    return OutlineReadinessReport(
        project_id=str(project_id),
        volume_id=resolved_volume_id,
        volume_idx=resolved_volume_idx,
        chapter_id=resolved_chapter_id,
        chapter_idx=resolved_chapter_idx,
        layers=layers,
    )
