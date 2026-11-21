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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, ChapterVersion, Project, Volume

logger = logging.getLogger(__name__)


class VersionUnavailable(LookupError):
    pass


class VersionConflict(ValueError):
    pass


async def lock_version_chapter(db, chapter_id):
    """All version writers lock the chapter before any version/FK writes."""
    chapter = await db.scalar(select(Chapter.id).join(Volume, Chapter.volume_id == Volume.id)
        .join(Project, Volume.project_id == Project.id)
        .where(Chapter.id == uuid.UUID(str(chapter_id)), Project.deleted_at.is_(None))
        .with_for_update(of=Chapter).execution_options(autoflush=False))
    if chapter is None:
        raise VersionUnavailable('章节不存在或作品已删除。')


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
    source: str = 'unknown'
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
            "source": self.source,
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
        lineterm="\n",
    )
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in diff)


def _model_to_node(version: ChapterVersion) -> VersionNode:
    """Convert a ChapterVersion ORM model to a VersionNode dataclass."""
    return VersionNode(
        id=str(version.id),
        chapter_id=str(version.chapter_id),
        parent_id=str(version.parent_id) if version.parent_id else None,
        branch_name=version.branch_name or "main",
        content_text=version.content_text or "",
        content_diff=version.content_diff or "",
        word_count=len(version.content_text or ''),
        created_at=version.created_at.isoformat() if version.created_at else "",
        is_active=bool(version.is_active),
        source=version.source or 'unknown',
        metadata=version.metadata_json or {},
    )


class VersionControlService:
    """Git-like version control for chapter content."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _version(self, chapter_id, version_id):
        version = await self._db.scalar(select(ChapterVersion).where(
            ChapterVersion.id == uuid.UUID(str(version_id)),
            ChapterVersion.chapter_id == uuid.UUID(str(chapter_id)),
        ).execution_options(populate_existing=True, autoflush=False))
        if version is None:
            raise VersionUnavailable('版本不存在或不属于当前章节。')
        return version

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
        version is deactivated. This selection does not replace chapter prose.

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
        chapter_id = uuid.UUID(str(chapter_id))
        parent_id = uuid.UUID(str(parent_id)) if parent_id else None
        branch_name = branch_name.strip()
        if not branch_name or len(branch_name) > 100:
            raise VersionConflict('版本分支名须为1至100个字符。')
        await lock_version_chapter(self._db, chapter_id)

        # Compute diff from parent if parent exists
        content_diff = ""
        if parent_id:
            parent = await self._version(chapter_id, parent_id)
            content_diff = _compute_diff(parent.content_text or '', content)

        # One selected comparison baseline per chapter, across branches.
        await self._db.execute(
            update(ChapterVersion)
            .where(
                ChapterVersion.chapter_id == chapter_id,
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
            .where(ChapterVersion.chapter_id == uuid.UUID(str(chapter_id)))
            .order_by(ChapterVersion.created_at, ChapterVersion.id)
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
        chapter_id = uuid.UUID(str(chapter_id))
        await lock_version_chapter(self._db, chapter_id)
        version = await self._version(chapter_id, version_id)

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

    async def get_diff(self, chapter_id: str, version_id_a: str, version_id_b: str) -> str:
        """
        Get the unified diff between two versions.

        Args:
            version_id_a: UUID of the first version (\"from\").
            version_id_b: UUID of the second version (\"to\").

        Returns:
            Unified diff string.
        """
        version_a = await self._version(chapter_id, version_id_a)
        version_b = await self._version(chapter_id, version_id_b)

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
        chapter_id = uuid.UUID(str(chapter_id))
        branch_name = branch_name.strip()
        await lock_version_chapter(self._db, chapter_id)
        source = await self._version(chapter_id, source_version_id)

        # Check that branch name doesn't already exist for this chapter
        existing_query = select(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter_id,
            ChapterVersion.branch_name == branch_name,
        )
        existing_result = await self._db.execute(existing_query)
        if existing_result.scalars().first():
            raise VersionConflict('此章节已存在同名分支，请核对版本列表。')

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
        await lock_version_chapter(self._db, chapter_id)
        source = await self._version(chapter_id, source_version_id)
        target = await self._version(chapter_id, target_version_id)

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
            ChapterVersion.chapter_id == uuid.UUID(str(chapter_id)),
            ChapterVersion.is_active == 1,
        )
        result = await self._db.execute(query)
        versions = result.scalars().all()
        if len(versions) > 1:
            raise VersionConflict('历史记录存在多个选中版本，请先明确选择对比基准。')
        version = versions[0] if versions else None

        if not version:
            return None
        return _model_to_node(version)
