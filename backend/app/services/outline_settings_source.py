"""Source checks before a slow settings extraction creates persistent records."""
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from app.models.project import Outline, Project
from app.services.entity_sources import digest
from app.services.outline_authority import outline_for_generation
from app.services.outline_order import REF, UNAVAILABLE


class SettingsSourceError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SettingsSource:
    project_id: UUID
    outline_id: UUID
    text: str
    digest: str
    material_hash: str | None = None


def settings_material_hash(content):
    """Fingerprint the full material, independent of unrelated project counters."""
    if (not isinstance(content, dict) or not isinstance(content.get('raw_text'), str)
            or not content['raw_text'].strip() or content.get('parse_error') or content.get('_parse_error')
            or 'available' in content and content['available'] is not True
            or content.get(UNAVAILABLE) or REF in content and not content[REF]):
        return None
    try:
        projected = outline_for_generation(content)
        if not isinstance(projected, dict) or not projected:
            return None
        return digest({'text': content['raw_text'], 'outline': projected})
    except (ValueError, TypeError):
        return None


async def read_settings_source(db, project_id, outline_id, *, lock=False):
    try:
        pid, oid = UUID(str(project_id)), UUID(str(outline_id))
    except ValueError as exc:
        raise SettingsSourceError('作品或大纲编号无效') from exc
    def query(model, *where):
        stmt = select(model).where(*where).execution_options(populate_existing=True)
        # Projection uses a separate PG session to insert project-owned rows.
        # NO KEY UPDATE still blocks source edits/deletion, but permits its FK
        # KEY SHARE checks; FOR UPDATE here would deadlock that projection.
        if lock:
            return stmt.with_for_update(key_share=model is Project)
        return stmt
    project = await db.scalar(query(Project, Project.id == pid))
    if project is None or project.deleted_at is not None:
        raise SettingsSourceError('作品不存在或已删除', 404)
    outline = await db.scalar(query(Outline, Outline.id == oid, Outline.project_id == pid))
    if outline is None:
        raise SettingsSourceError('本作品中找不到该大纲', 404)
    if outline.level != 'book':
        raise SettingsSourceError('仅支持从全书大纲提取设定')
    content = outline.content_json or {}
    text = content.get('raw_text') if isinstance(content, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise SettingsSourceError('全书大纲没有可提取的文字')
    def values(row):
        return {column.key: getattr(row, column.key) for column in row.__table__.columns}
    data = {'project': values(project), 'outline': values(outline)}
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return SettingsSource(pid, oid, text, digest, settings_material_hash(content))


async def require_current_settings_source(db, source):
    try:
        current = await read_settings_source(db, source.project_id, source.outline_id, lock=True)
    except SettingsSourceError as exc:
        raise SettingsSourceError('提取期间作品或书纲已变化，未写入设定，请刷新后重试') from exc
    if current.digest != source.digest:
        raise SettingsSourceError('提取期间作品或书纲已变化，未写入设定，请刷新后重试')
    return current
