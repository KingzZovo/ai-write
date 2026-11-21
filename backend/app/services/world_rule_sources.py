"""Read current graph rules; PostgreSQL text copies cannot stand in for them."""
import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models.project import Outline, Project
from app.services.outline_settings_source import read_settings_source, SettingsSourceError, settings_material_hash
from app.services.world_rule_material import WorldRuleSourceError, validate_world_rule_source

RULE_QUERY = ('MATCH (w:WorldRule {project_id:$pid}) '
    'RETURN w.id AS id,w.category AS category,w.text AS rule_text,w.origin AS origin,'
    'w.source_outline_id AS source_outline_id,w.source_outline_digest AS source_outline_digest,'
    'w.source_outline_material_hash AS source_outline_material_hash '
    'ORDER BY category,rule_text,id')


async def current_rule_driver():
    from app.db.neo4j import get_neo4j
    async for driver in get_neo4j():
        return driver
    raise RuntimeError('graph_unavailable')


def _project_id(project_id):
    try:
        return str(UUID(str(project_id)))
    except (ValueError, TypeError, AttributeError):
        raise WorldRuleSourceError('world_rule_project_invalid', '作品编号无效。', status=422) from None


async def require_world_rule_project(db, project_id, *, lock=False):
    pid = _project_id(project_id)
    query = select(Project.id).where(Project.id == UUID(pid), Project.deleted_at.is_(None))
    if lock:
        # Serialize rule edits and block final planning snapshots, while still
        # allowing the separate projection connection's FK KEY SHARE checks.
        query = query.with_for_update(key_share=True)
    try:
        project = await db.scalar(query.execution_options(autoflush=False))
    except SQLAlchemyError:
        raise WorldRuleSourceError('world_rule_project_unavailable', '作品暂时无法核对，请保留原请求后重试。', status=503) from None
    if project is None:
        raise WorldRuleSourceError('world_rule_project_unavailable', '作品不存在或已删除。', status=404)
    return pid


async def _current_book_basis(db, project_id, rules):
    derived = [r for r in rules if r.get('origin') != 'author' and any(r.get(key) for key in
        ('source_outline_id', 'source_outline_digest', 'source_outline_material_hash'))]
    if not derived:
        return None
    row = (await db.execute(select(Outline.id, Outline.content_json).where(
        Outline.project_id == UUID(str(project_id)), Outline.level == 'book').order_by(
        Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc())
        .limit(1).execution_options(autoflush=False))).one_or_none()
    if row is None:
        return None
    material_hash = settings_material_hash(row.content_json)
    legacy_digest = None
    if any(r.get('source_outline_material_hash') is None and r.get('source_outline_id') == str(row.id)
           for r in derived):
        try:
            legacy_digest = (await read_settings_source(db, project_id, row.id)).digest
        except SettingsSourceError:
            pass  # Keep the rule visible as needing source review, not valid.
    return {'id': str(row.id), 'material_hash': material_hash, 'legacy_digest': legacy_digest}


async def read_world_rule_source(db, project_id, *, driver=None):
    pid = _project_id(project_id)
    try:
        async with asyncio.timeout(30):
            driver = driver if driver is not None else await current_rule_driver()
            async with driver.session() as session:
                async def read(tx):
                    return [dict(row) async for row in await tx.run(RULE_QUERY, pid=pid)]
                rules = await session.execute_read(read)
        source = {'version': 1, 'project_id': pid, 'rules': rules, 'book_source': None}
        validate_world_rule_source(source, pid)
        source['book_source'] = await _current_book_basis(db, pid, rules)
        return validate_world_rule_source(source, pid)
    except WorldRuleSourceError:
        raise
    except SQLAlchemyError:
        raise
    except Exception:
        raise WorldRuleSourceError('world_rules_context_unavailable',
            '当前世界规则暂时无法读取，请保留原请求后重试。', status=503) from None
