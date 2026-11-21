"""Write one settings extraction as a single Neo4j transaction."""
import json
from uuid import uuid4

from app.services.rel_type import canonicalize_rel_type
from app.services.relationship_graph_write import GUARD_CONSTRAINT, lock_relationship_graph
from app.services.relationship_write_contract import relationship_suppression_key


def settings_payload(extracted, existing_characters):
    """Seed missing graph nodes from existing author cards without replacing them."""
    characters = {}
    for item in extracted.get('characters', []):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            continue
        name = item['name'].strip()
        if not name or name in characters:
            continue
        existing = existing_characters.get(name)
        characters[name] = {'name': name, 'id': str(existing.id) if existing else str(uuid4()),
            'profile_json': json.dumps(existing.profile_json if existing else item, ensure_ascii=False),
            'profile_origin': 'author_card' if existing else 'outline_extraction'}
    relationships = {}
    for item in extracted.get('relationships', []):
        if not isinstance(item, dict):
            continue
        src, dst = item.get('source_name'), item.get('target_name')
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        src, dst = src.strip(), dst.strip()
        if not src or not dst or src == dst:
            continue
        if any(name not in characters and name not in existing_characters for name in (src, dst)):
            continue
        for name in (src, dst):
            if name not in characters:
                author = existing_characters[name]
                characters[name] = {'name': name, 'id': str(author.id),
                    'profile_json': json.dumps(author.profile_json or {}, ensure_ascii=False), 'profile_origin': 'author_card'}
        raw_type = str(item.get('rel_type') or 'other')
        kind = canonicalize_rel_type(raw_type)
        relationships[(src, dst, kind)] = {'source': src, 'target': dst, 'type': kind, 'raw_type': raw_type}
    rules = {}
    for item in extracted.get('world_rules', []):
        if not isinstance(item, dict):
            continue
        category, text = item.get('category'), item.get('rule_text')
        if not isinstance(category, str) or not isinstance(text, str) or not category.strip() or not text.strip():
            continue
        key = category.strip(), text.strip()
        rules[key] = {'id': str(uuid4()), 'category': key[0], 'text': key[1]}
    return list(characters.values()), list(rules.values()), list(relationships.values())


async def write_settings_graph(driver, source, extracted, existing_characters):
    characters, rules, relationships = settings_payload(extracted, existing_characters)
    for row in relationships:
        row['suppression_key'] = relationship_suppression_key(source.project_id,
            row['source'], row['target'], {'type': row['type'],
                'source_outline_id': str(source.outline_id), 'source_outline_digest': source.digest})
    async def write(tx):
        await lock_relationship_graph(tx, source.project_id)
        params = {'pid': str(source.project_id), 'oid': str(source.outline_id), 'digest': source.digest,
                  'material_hash': getattr(source, 'material_hash', None)}
        for query, rows in (
            ("UNWIND $rows AS row MERGE (c:Character {project_id: $pid, name: row.name}) "
             "ON CREATE SET c.id = row.id, c.profile_json = row.profile_json, "
             "c.source_outline_id = $oid, c.source_outline_digest = $digest, c.profile_origin = row.profile_origin", characters),
            ("UNWIND $rows AS row MERGE (w:WorldRule {project_id: $pid, category: row.category, text: row.text}) "
             "ON CREATE SET w.id = row.id, w.origin = 'outline_extraction', w.source_outline_id = $oid, "
             "w.source_outline_digest = $digest, w.source_outline_material_hash = $material_hash", rules),
            ("UNWIND $rows AS row MATCH (a:Character {project_id: $pid, name: row.source}), "
             "(b:Character {project_id: $pid, name: row.target}) "
             "WHERE NOT EXISTS { MATCH (:RelationshipSuppression {key: row.suppression_key}) } "
             "MERGE (a)-[r:RELATES_TO {project_id: $pid, source_name: row.source, target_name: row.target, "
             "type: row.type, chapter_start: 0}]->(b) "
             "ON CREATE SET r.raw_type = row.raw_type, r.source_outline_id = $oid, r.source_outline_digest = $digest", relationships),
        ):
            if rows:
                result = await tx.run(query, rows=rows, **params)
                await result.consume()
    async with driver.session() as session:
        await (await session.run(GUARD_CONSTRAINT)).consume()
        await session.execute_write(write)
