"""Keep generated book candidates separate and reject stale source snapshots."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from app.models.project import Outline, Project
from app.services.outline_consistency_gate import validate_outline_consistency
from app.services.outline_generator import OutlineGenerator


@dataclass(frozen=True)
class BookOutlineSnapshot:
    project_id: UUID
    digest: str


async def _read(db, project_id, *, lock=False):
    def query(model, *conditions):
        stmt = select(model).where(*conditions).execution_options(populate_existing=True)
        return stmt.with_for_update() if lock else stmt

    project = await db.scalar(query(Project, Project.id == project_id))
    if project is None or project.deleted_at is not None:
        raise ValueError('作品不存在或已删除')
    books = (await db.scalars(query(Outline, Outline.project_id == project_id,
                                    Outline.level == 'book').order_by(Outline.id))).all()
    def values(row):
        return {c.key: getattr(row, c.key) for c in row.__table__.columns}
    data = {'project': values(project), 'books': [values(book) for book in books]}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return books, digest


async def snapshot_book_outline(db, project_id):
    pid = UUID(str(project_id))
    _, digest = await _read(db, pid)
    return BookOutlineSnapshot(pid, digest)


def book_content(full_text, staged_payload=None):
    if not isinstance(full_text, str) or not full_text.strip():
        raise ValueError('整书大纲没有有效正文')
    # These parsers do not need a model router or a live provider.
    raw = OutlineGenerator._strip_volume_plan_tags(full_text)
    if not raw.strip():
        raise ValueError('整书大纲没有有效正文')
    content = {'raw_text': raw}
    if staged_payload is not None and not isinstance(staged_payload, dict):
        raise ValueError('整书大纲结构无效')
    payload = staged_payload or {}
    plan = payload.get('volume_plan')
    if not plan:
        plan = OutlineGenerator._extract_volume_plan(full_text)
    if (staged_payload is not None or '<volume-plan>' in full_text) and not plan:
        raise ValueError('整书大纲缺少有效分卷计划')
    if plan:
        if not isinstance(plan, list) or any(not isinstance(item, dict) for item in plan):
            raise ValueError('分卷计划格式无效')
        indices = [item.get('idx') for item in plan]
        if any(type(idx) is not int for idx in indices) or sorted(indices) != list(range(1, len(plan) + 1)):
            raise ValueError('分卷计划卷号不完整或重复')
        if any(type(item.get('est_chapters')) is not int or item['est_chapters'] < 1 for item in plan):
            raise ValueError('分卷计划章数无效')
        content['volume_plan'] = deepcopy(plan)
    structured = payload.get('structured') or {}
    if not isinstance(structured, dict):
        raise ValueError('整书大纲结构无效')
    for key, value in structured.items():
        if value and key not in ('raw_text', 'volume_plan', '_consistency_report'):
            content[key] = deepcopy(value)
    content['_consistency_report'] = validate_outline_consistency(content, level='book').to_dict()
    return content


async def save_book_outline(db, snapshot, full_text, staged_payload=None):
    """Caller commits once; previous candidates, confirmations and prose are kept."""
    content = book_content(full_text, staged_payload)
    books, digest = await _read(db, snapshot.project_id, lock=True)
    if digest != snapshot.digest:
        raise ValueError('生成期间作品设置或书纲已变化，请刷新核对后重试')
    outline = Outline(project_id=snapshot.project_id, level='book', parent_id=None,
                      content_json=content, is_confirmed=0,
                      version=max((book.version for book in books), default=0) + 1)
    db.add(outline)
    await db.flush()
    return outline
