"""Serialize explicit rule edits before changing the canonical graph settings."""
from uuid import uuid4
import re

from app.services.world_rule_sources import require_world_rule_project
from app.services.world_rule_material import WorldRuleSourceError, rule_from_properties, world_rule_version


async def write_world_rule(db, driver, project_id, operation, *, category, text, new_category=None, new_text=None,
                          expected_version=None):
    if operation not in {'create', 'update', 'delete'}:
        raise ValueError('unknown rule operation')
    values = [category, text] + ([new_category, new_text] if operation == 'update' else [])
    if (any(not isinstance(value, str) for value in values) or not text.strip()
            or operation == 'create' and not category.strip()
            or operation == 'update' and (not new_category.strip() or not new_text.strip())):
        raise WorldRuleSourceError('world_rule_invalid', '分类和完整规则不能为空。', status=422)
    if operation != 'create' and expected_version is None:
        raise WorldRuleSourceError('world_rule_precondition_required', '请先读取当前规则，再保存或删除。', status=428)
    if operation != 'create' and (not isinstance(expected_version, str) or re.fullmatch('[0-9a-f]{64}', expected_version) is None):
        raise WorldRuleSourceError('world_rule_invalid', '规则来源版本无效，请重新读取。', status=422)
    # Existing nodes must be matched byte-for-byte, including legacy blank
    # categories and leading/trailing whitespace present in the read version.
    if operation == 'create':
        category, text = category.strip(), text.strip()
    if operation == 'update':
        new_category, new_text = new_category.strip(), new_text.strip()
    pid = await require_world_rule_project(db, project_id, lock=True)
    params = {'pid': pid, 'cat': category, 'txt': text, 'id': str(uuid4())}

    async def read_key(tx, cat, txt):
        result = await tx.run('MATCH (w:WorldRule {project_id:$pid,category:$cat,text:$txt}) '
            'RETURN properties(w) AS properties', pid=pid, cat=cat, txt=txt)
        rows = [dict(row) async for row in result]
        if len(rows) > 1:
            raise WorldRuleSourceError('world_rule_source_invalid', '这条规则有重复记录，请先核对。')
        return rule_from_properties(rows[0]['properties']) if rows else None

    async def write(tx):
        if operation == 'create':
            await read_key(tx, category, text)  # Reject duplicate existing keys before adopting either node.
            row = await (await tx.run('MERGE (w:WorldRule {project_id:$pid,category:$cat,text:$txt}) '
                'SET w.id=coalesce(w.id,$id),w.origin="author" RETURN w.id AS id', **params)).single()
            if row is None or not row.get('id'):
                raise RuntimeError('rule_creation_unconfirmed')
            return {'id': row['id']}
        current = await read_key(tx, category, text)
        if current is None:
            raise WorldRuleSourceError('world_rule_not_found', '该规则已变化或不存在，请重新读取。', status=404)
        if world_rule_version(current) != expected_version:
            raise WorldRuleSourceError('world_rule_source_changed', '规则来源已变化，请保留输入并重新读取核对。')
        if operation == 'delete':
            row = await (await tx.run('MATCH (w:WorldRule {project_id:$pid,category:$cat,text:$txt}) '
                'DETACH DELETE w RETURN count(w) AS deleted', **params)).single()
            if row is None or row.get('deleted') != 1:
                raise RuntimeError('rule_deletion_unconfirmed')
            return {'category': category, 'text': text, 'deleted': 1}
        target_exists = ((new_category, new_text) != (category, text)
                         and await read_key(tx, new_category, new_text) is not None)
        if target_exists:
            # The explicit edit adopts the existing target as an author
            # setting. Retained source tags now describe only its history.
            row = await (await tx.run('MATCH (w:WorldRule {project_id:$pid,category:$cat,text:$txt}) '
                'SET w.id=coalesce(w.id,$id),w.origin="author" RETURN count(w) AS matched',
                pid=pid, cat=new_category, txt=new_text, id=params['id'])).single()
            if row is None or row.get('matched') != 1:
                raise RuntimeError('rule_adoption_unconfirmed')
            row = await (await tx.run('MATCH (w:WorldRule {project_id:$pid,category:$cat,text:$txt}) '
                'DETACH DELETE w RETURN count(w) AS deleted', **params)).single()
            if row is None or row.get('deleted') != 1:
                raise RuntimeError('rule_deletion_unconfirmed')
        else:
            row = await (await tx.run('MATCH (w:WorldRule {project_id:$pid,category:$cat,text:$txt}) '
                'SET w.category=$new_cat,w.text=$new_txt,w.id=coalesce(w.id,$id),w.origin="author" '
                'RETURN count(w) AS matched', **params, new_cat=new_category, new_txt=new_text)).single()
            if row is None or row.get('matched') != 1:
                raise RuntimeError('rule_update_unconfirmed')
        return {'category': new_category, 'text': new_text, 'deduplicated': bool(target_exists)}

    try:
        async with driver.session() as session:
            return await session.execute_write(write)
    except WorldRuleSourceError:
        raise
    except Exception:
        raise WorldRuleSourceError('world_rule_write_unconfirmed',
            '规则写入结果尚未确认，请保留输入并重新读取当前规则核对。', status=503) from None
