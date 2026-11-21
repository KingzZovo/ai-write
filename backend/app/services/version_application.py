"""Prepare an explicit restoration against the manuscript the author saw."""
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.project import Chapter, ChapterVersion, Volume
from app.services.chapter_content import check_chapter_replacement
from app.services.chapter_order import lock_chapter_structure
from app.services.review_contract import draft_digest
from app.services.version_control import VersionControlService, VersionUnavailable


class VersionApplicationPrecondition(ValueError):
    pass


@dataclass(frozen=True)
class AppliedVersion:
    project_id: UUID
    chapter_id: UUID
    chapter_idx: int
    version_id: UUID
    content_text: str
    backup_version_id: UUID | None
    changed: bool


async def prepare_version_application(db, chapter_id, version_id, *, expected_content_text=None, force=False):
    if expected_content_text is None:
        raise VersionApplicationPrecondition('请先读取并核对当前保存稿，再确认恢复版本。')
    cid, vid = UUID(str(chapter_id)), UUID(str(version_id))
    pid = await db.scalar(select(Volume.project_id).join(Chapter, Chapter.volume_id == Volume.id)
        .where(Chapter.id == cid).execution_options(autoflush=False))
    if pid is None:
        raise VersionUnavailable('章节已不存在。')
    await lock_chapter_structure(db, pid)
    chapter = await db.scalar(select(Chapter).join(Volume, Chapter.volume_id == Volume.id)
        .where(Chapter.id == cid, Volume.project_id == pid)
        .with_for_update(of=Chapter).execution_options(populate_existing=True, autoflush=False))
    version = await db.scalar(select(ChapterVersion).where(ChapterVersion.id == vid, ChapterVersion.chapter_id == cid)
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    if chapter is None or version is None:
        raise VersionUnavailable('版本或章节不存在，或版本不属于当前章节。')
    previous, replacement = chapter.content_text or '', version.content_text
    check_chapter_replacement(previous, replacement, expected=expected_content_text, force=force)
    changed = previous != replacement
    backup = None
    if changed:
        backup = ChapterVersion(
            id=uuid4(), chapter_id=cid, branch_name='before_version_restore',
            content_text=previous, word_count=len(previous), is_active=0,
            source='version_restore_backup', metadata_json={
                'restored_version_id': str(vid), 'title': chapter.title, 'status': chapter.status,
                'source_sha256': draft_digest(previous), 'restored_sha256': draft_digest(replacement),
            },
        )
        db.add(backup)
        chapter.content_text = replacement
    # Historical restores may have copied an inaccurate version count. Even
    # a same-body selection must keep the canonical count tied to its text.
    chapter.word_count = len(replacement)
    await VersionControlService(db).switch_active(cid, vid)
    await db.flush()
    return AppliedVersion(pid, cid, chapter.chapter_idx, vid, replacement, backup.id if backup else None, changed)
