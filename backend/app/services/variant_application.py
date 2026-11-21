"""Apply a candidate against the exact saved manuscript the author reviewed.

The caller owns commit/rollback and publishes only after commit confirmation.
No model, graph or broker calls run while the write locks are held.
"""
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.project import Chapter, ChapterVariant, ChapterVersion, Volume
from app.services.bvsr import select_variant
from app.services.chapter_content import check_chapter_replacement
from app.services.chapter_order import lock_chapter_structure
from app.services.review_contract import draft_digest


class VariantApplicationUnavailable(LookupError):
    pass


class VariantApplicationPrecondition(ValueError):
    pass


@dataclass(frozen=True)
class AppliedVariant:
    variant: ChapterVariant
    project_id: UUID
    chapter_id: UUID
    chapter_idx: int
    backup_version_id: UUID | None
    changed: bool


async def prepare_variant_application(
    db, variant_id: UUID, *, expected_content_text: str | None, force: bool = False,
) -> AppliedVariant:
    if expected_content_text is None:
        raise VariantApplicationPrecondition('请先读取当前保存稿，再核对并确认应用候选。')
    scope = (await db.execute(
        select(ChapterVariant.chapter_id, Volume.project_id)
        .join(Chapter, ChapterVariant.chapter_id == Chapter.id)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(ChapterVariant.id == variant_id)
        .execution_options(autoflush=False)
    )).one_or_none()
    if scope is None:
        raise VariantApplicationUnavailable('候选或对应章节已不存在。')

    # Same order as structural writes: generation -> materialization -> project
    # -> chapter -> candidate. Re-read after locking, even in a warm session.
    await lock_chapter_structure(db, scope.project_id)
    chapter = (await db.execute(
        select(Chapter).join(Volume, Chapter.volume_id == Volume.id)
        .where(Chapter.id == scope.chapter_id, Volume.project_id == scope.project_id)
        .with_for_update(of=Chapter).execution_options(populate_existing=True, autoflush=False)
    )).scalar_one_or_none()
    candidate = (await db.execute(
        select(ChapterVariant).where(ChapterVariant.id == variant_id, ChapterVariant.chapter_id == scope.chapter_id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False)
    )).scalar_one_or_none()
    if chapter is None or candidate is None or not isinstance(candidate.content_text, str):
        raise VariantApplicationUnavailable('候选或对应章节已不存在。')

    previous = chapter.content_text or ''
    replacement = candidate.content_text
    check_chapter_replacement(previous, replacement, expected=expected_content_text, force=force)
    changed = replacement != previous
    backup = None
    if changed:
        backup = ChapterVersion(
            id=uuid4(), chapter_id=chapter.id, branch_name='before_variant_apply',
            content_text=previous, word_count=len(previous), is_active=0,
            source='variant_apply_backup', metadata_json={
                'variant_id': str(candidate.id), 'run_id': str(candidate.run_id) if candidate.run_id else None,
                'title': chapter.title, 'status': chapter.status,
                'source_sha256': draft_digest(previous), 'candidate_sha256': draft_digest(replacement),
            },
        )
        db.add(backup)
        chapter.content_text = replacement
        chapter.word_count = len(replacement)

    chosen = await select_variant(db, variant_id=variant_id)
    chosen.word_count = len(replacement)
    await db.flush()
    return AppliedVariant(chosen, scope.project_id, chapter.id, chapter.chapter_idx,
                          backup.id if backup else None, changed)
