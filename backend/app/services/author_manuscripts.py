"""Prepare an author edit under the shared project/chapter write locks."""
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.project import Chapter, ChapterVersion, Project, Volume
from app.services.chapter_content import ChapterContentError, check_chapter_replacement
from app.services.chapter_order import ChapterOrderUnavailable, lock_chapter_structure


@dataclass(frozen=True)
class AuthorManuscriptChange:
    chapter: Chapter
    previous_text: str
    changed: bool
    backup_version_id: str | None


async def prepare_author_manuscript(db, project_id, chapter_id, *, text, expected, force=False):
    """Validate and stage a backup; caller applies fields and commits together.

    No content mutation, commit, model call, or extraction dispatch occurs here.
    Mixed metadata/body requests can still reject their metadata preconditions
    before the transaction makes any version or manuscript visible.
    """
    if expected is None:
        raise ChapterContentError('chapter_content_precondition_required',
                                  '请先读取完整保存稿，再保存编辑区修改。')
    pid, cid = UUID(str(project_id)), UUID(str(chapter_id))
    await lock_chapter_structure(db, pid)
    chapter = await db.scalar(select(Chapter).join(Volume, Chapter.volume_id == Volume.id)
        .join(Project, Volume.project_id == Project.id)
        .where(Chapter.id == cid, Volume.project_id == pid, Project.deleted_at.is_(None))
        .with_for_update(of=Chapter).execution_options(populate_existing=True, autoflush=False))
    if chapter is None:
        raise ChapterOrderUnavailable('章节不存在或作品已删除。')
    previous = chapter.content_text or ''
    check_chapter_replacement(previous, text, expected=expected, force=force)
    changed = previous != text
    backup = None
    if changed and force:
        backup = ChapterVersion(id=uuid4(), chapter_id=cid, content_text=previous,
            word_count=len(previous), branch_name='before_author_edit', is_active=0,
            source='author_edit_backup', metadata_json={'title': chapter.title, 'status': chapter.status})
        db.add(backup)
    return AuthorManuscriptChange(chapter, previous, changed, str(backup.id) if backup else None)
