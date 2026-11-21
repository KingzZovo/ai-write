"""Book-ordered evaluation evidence with explicit history/future boundaries."""
from dataclasses import dataclass
from sqlalchemy import select, tuple_, func
from app.models.project import Chapter, Volume
from app.services.summary_provenance import current_summary_sql


@dataclass
class EvaluationNeighbors:
    previous_context: str = ''
    following_context: str = ''
    previous_tail: str = ''


def _render(row, *, future=False):
    if row is None:
        return ''
    label = f'[VOL-{row.volume_idx}/CH-{row.chapter_idx}; id={row.id}; status={row.status}]'
    boundary = ('后文仅用于检查后续衔接，不能据此认定本章事件已发生或角色已获知信息。'
                if future else '前章文本用于衔接核查，不覆盖作品权威事实。')
    if row.status != 'completed':
        boundary += '该章节尚未完成验收，仅作待审草稿参考，不视为既定事实。'
    parts = [label, boundary]
    if row.summary:
        parts.append('摘要：\n' + row.summary)
    if row.excerpt:
        parts.append(('正文开头节选：\n' if future else '正文结尾节选：\n') + row.excerpt)
    if not row.summary and not row.excerpt:
        parts.append('相邻章节尚无正文或摘要；不能跳过它假定前后事件已经衔接。')
    return '\n'.join(parts)


async def load_evaluation_neighbors(db, chapter_id, project_id=None) -> EvaluationNeighbors:
    target = select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx, Volume.volume_idx,
                    Volume.project_id).join(Volume, Chapter.volume_id == Volume.id).where(Chapter.id == chapter_id)
    if project_id is not None:
        target = target.where(Volume.project_id == project_id)
    current = (await db.execute(target)).one_or_none()
    if current is None:
        raise ValueError('评审目标章节不存在或不属于当前作品')
    count = await db.scalar(select(func.count()).select_from(Chapter).where(
        Chapter.volume_id == current.volume_id, Chapter.chapter_idx == current.chapter_idx))
    if count != 1:
        raise ValueError('评审目标卷内章号重复，无法确定衔接顺序')
    position = tuple_(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
    cursor = (current.volume_idx, current.volume_id, current.chapter_idx, current.id)
    columns = (Chapter.id, Chapter.chapter_idx, Chapter.status, current_summary_sql().label("summary"), Volume.volume_idx)
    base = select(*columns).join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == current.project_id)
    # Slice in SQL: a 500万字 book never loads all neighbouring prose into memory.
    previous = (await db.execute(base.add_columns(func.right(Chapter.content_text, 3500).label('excerpt'))
        .where(position < cursor).order_by(Volume.volume_idx.desc(), Volume.id.desc(),
                                          Chapter.chapter_idx.desc(), Chapter.id.desc()).limit(1))).one_or_none()
    following = (await db.execute(base.add_columns(func.left(Chapter.content_text, 3500).label('excerpt'))
        .where(position > cursor).order_by(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id).limit(1))).one_or_none()
    return EvaluationNeighbors(_render(previous), _render(following, future=True),
                               (previous.excerpt or '') if previous else '')
