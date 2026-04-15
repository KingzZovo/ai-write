"""
Version Control System

Tree-structured version history for chapters:
- Each save creates a version node
- Users can create branches (Draft A, Draft B)
- Diff comparison between any two versions
- Switch active version
- Merge branches
"""

from __future__ import annotations

import difflib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import ChapterVersion

logger = logging.getLogger(__name__)


@dataclass
class VersionNode:
    """Represents a single version in the chapter version tree."""

    id: str
    chapter_id: str
    parent_id: str | None
    branch_name: str
    content_text: str
    content_diff: str
    word_count: int
    created_at: str
    is_active: bool
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to a serializable dictionary."""
        return {
            "id": self.id,
            "chapter_id": self.chapter_id,
            "parent_id": self.parent_id,
            "branch_name": self.branch_name,
            "content_text": self.content_text,
            "content_diff": self.content_diff,
            "word_count": self.word_count,
            "created_at": self.created_at,
            "is_active": self.is_active,
            "metadata": self.metadata,
        }


def _compute_diff(old_text: str, new_text: str) -> str:
    """Compute a unified diff between two texts."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="parent",
        tofile="current",
        lineterm="",
    )
    return "".join(diff)


def _apply_diff(base_text: str, diff_text: str) -> str:
    """
    Apply a unified diff to base text to reconstruct the target version.

    For simplicity, we store full content_text as well, so this is a
    utility for verification rather than the primary reconstruction path.
    """
    # Since we always store content_text, this is provided for completeness
    # In a production system, you might only store diffs for space savings
    return base_text  # Placeholder -- actual content is in content_text


def _model_to_node(version: ChapterVersion) -> VersionNode:
    """Convert a ChapterVersion ORM model to a VersionNode dataclass."""
    return VersionNode(
        id=str(version.id),
        chapter_id=str(version.chapter_id),
        parent_id=str(version.parent_id) if version.parent_id else None,
        branch_name=version.branch_name or "main",
        content_text=version.content_text or "",
        content_diff=version.content_diff or "",
        word_count=version.word_count or 0,
        created_at=version.created_at.isoformat() if version.created_at else "",
        is_active=bool(version.is_active),
        metadata=version.metadata_json or {},
    )


class VersionControlService:
    """Git-like version control for chapter content."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_version(
        self,
        chapter_id: str,
        content: str,
        branch_name: str = "main",
        parent_id: str | None = None,
        metadata: dict | None = None,
    ) -> VersionNode:
        """
        Create a new version node for a chapter.

        If parent_id is provided, computes a diff from the parent version.
        The new version is automatically set as active; the previous active
        version on the same branch is deactivated.

        Args:
            chapter_id: UUID of the chapter.
            content: Full text content for this version.
            branch_name: Name of the branch (default "main").
            parent_id: UUID of the parent version, if any.
            metadata: Additional metadata (source, etc.).

        Returns:
            The created VersionNode.
        """
        meta = metadata or {}

        # Compute diff from parent if parent exists
        content_diff = ""
        if parent_id:
            parent = await self._db.get(ChapterVersion, parent_id)
            if parent and parent.content_text:
                content_diff = _compute_diff(parent.content_text, content)

        # Deactivate all currently active versions for this chapter + branch
        await self._db.execute(
            update(ChapterVersion)
            .where(
                ChapterVersion.chapter_id == chapter_id,
                ChapterVersion.branch_name == branch_name,
                ChapterVersion.is_active == 1,
            )
            .values(is_active=0)
        )

        version = ChapterVersion(
            id=uuid.uuid4(),
            chapter_id=chapter_id,
            parent_id=parent_id,
            branch_name=branch_name,
            content_text=content,
            content_diff=content_diff,
            word_count=len(content),
            is_active=1,
            source=meta.get("source", "user_edit"),
            metadata_json=meta,
        )
        self._db.add(version)
        await self._db.flush()
        await self._db.refresh(version)

        logger.info(
            "Created version %s for chapter %s (branch=%s, words=%d)",
            version.id,
            chapter_id,
            branch_name,
            version.word_count,
        )

        return _model_to_node(version)

    async def get_version_tree(self, chapter_id: str) -> list[VersionNode]:
        """
        Get the full version tree for a chapter.

        Returns all versions ordered by creation time, which can be assembled
        into a tree structure by the client using parent_id references.

        Args:
            chapter_id: UUID of the chapter.

        Returns:
            List of VersionNode objects forming the version tree.
        """
        query = (
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.created_at)
        )
        result = await self._db.execute(query)
        versions = result.scalars().all()

        return [_model_to_node(v) for v in versions]

    async def switch_active(self, chapter_id: str, version_id: str) -> None:
        """
        Switch the active version for a chapter.

        Deactivates all versions for the chapter, then activates the specified one.

        Args:
            chapter_id: UUID of the chapter.
            version_id: UUID of the version to activate.
        """
        # Verify version exists and belongs to the chapter
        version = await self._db.get(ChapterVersion, version_id)
        if not version or str(version.chapter_id) != chapter_id:
            raise ValueError(
                f"Version {version_id} not found for chapter {chapter_id}"
            )

        # Deactivate all versions for this chapter
        await self._db.execute(
            update(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id, ChapterVersion.is_active == 1)
            .values(is_active=0)
        )

        # Activate the target version
        version.is_active = 1
        await self._db.flush()

        logger.info(
            "Switched active version for chapter %s to %s (branch=%s)",
            chapter_id,
            version_id,
            version.branch_name,
        )

    async def get_diff(self, version_id_a: str, version_id_b: str) -> str:
        """
        Get the unified diff between two versions.

        Args:
            version_id_a: UUID of the first version (\"from\").
            version_id_b: UUID of the second version (\"to\").

        Returns:
            Unified diff string.
        """
        version_a = await self._db.get(ChapterVersion, version_id_a)
        version_b = await self._db.get(ChapterVersion, version_id_b)

        if not version_a:
            raise ValueError(f"Version {version_id_a} not found")
        if not version_b:
            raise ValueError(f"Version {version_id_b} not found")

        return _compute_diff(
            version_a.content_text or "",
            version_b.content_text or "",
        )

    async def create_branch(
        self,
        chapter_id: str,
        source_version_id: str,
        branch_name: str,
    ) -> VersionNode:
        """
        Create a new branch from an existing version.

        Copies the source version's content into a new version on the
        specified branch.

        Args:
            chapter_id: UUID of the chapter.
            source_version_id: UUID of the version to branch from.
            branch_name: Name of the new branch.

        Returns:
            The new VersionNode on the new branch.
        """
        source = await self._db.get(ChapterVersion, source_version_id)
        if not source or str(source.chapter_id) != chapter_id:
            raise ValueError(
                f"Source version {source_version_id} not found for chapter {chapter_id}"
            )

        # Check that branch name doesn't already exist for this chapter
        existing_query = select(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter_id,
            ChapterVersion.branch_name == branch_name,
        )
        existing_result = await self._db.execute(existing_query)
        if existing_result.scalars().first():
            raise ValueError(
                f"Branch '{branch_name}' already exists for chapter {chapter_id}"
            )

        return await self.create_version(
            chapter_id=chapter_id,
            content=source.content_text or "",
            branch_name=branch_name,
            parent_id=source_version_id,
            metadata={"source": "branch", "branched_from": source_version_id},
        )

    async def merge_branch(
        self,
        chapter_id: str,
        source_version_id: str,
        target_version_id: str,
    ) -> VersionNode:
        """
        Merge a source version into a target version's branch.

        Takes the content from the source version and creates a new version
        on the target version's branch, with the target as parent.

        For simplicity, this performs a \"take theirs\" merge -- the source
        content replaces the target. A more sophisticated three-way merge
        could be implemented if needed.

        Args:
            chapter_id: UUID of the chapter.
            source_version_id: UUID of the source version (content to take).
            target_version_id: UUID of the target version (branch to merge into).

        Returns:
            The new merged VersionNode.
        """
        source = await self._db.get(ChapterVersion, source_version_id)
        target = await self._db.get(ChapterVersion, target_version_id)

        if not source or str(source.chapter_id) != chapter_id:
            raise ValueError(
                f"Source version {source_version_id} not found for chapter {chapter_id}"
            )
        if not target or str(target.chapter_id) != chapter_id:
            raise ValueError(
                f"Target version {target_version_id} not found for chapter {chapter_id}"
            )

        return await self.create_version(
            chapter_id=chapter_id,
            content=source.content_text or "",
            branch_name=target.branch_name or "main",
            parent_id=target_version_id,
            metadata={
                "source": "merge",
                "merged_from": source_version_id,
                "merged_into": target_version_id,
            },
        )

    async def get_active_version(self, chapter_id: str) -> VersionNode | None:
        """
        Get the currently active version for a chapter.

        Args:
            chapter_id: UUID of the chapter.

        Returns:
            The active VersionNode, or None if no versions exist.
        """
        query = select(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter_id,
            ChapterVersion.is_active == 1,
        )
        result = await self._db.execute(query)
        version = result.scalars().first()

        if not version:
            return None
        return _model_to_node(version)
