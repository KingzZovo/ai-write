"""Execute the actual outline-selection SQL against an isolated SQLite table.

This exercises filtering/ordering, not the PostgreSQL runtime or schema.
"""
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.dialects import sqlite
from app.services.fact_contract import build_fact_contract
from app.services.outline_authority import record_text_edit, TEXT_REVISION
from app.services.outline_order import REF, UNAVAILABLE


class SourceDB:
    def __init__(self, rows, characters=()):
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute('CREATE TABLE outlines (id TEXT, project_id TEXT, level TEXT, is_confirmed INTEGER, version INTEGER, created_at TEXT, content_json TEXT)')
        self.connection.executemany('INSERT INTO outlines VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
        self.characters = characters
        self.calls = 0
        self.statements = []

    async def execute(self, statement):
        self.calls += 1
        self.statements.append(statement)
        if self.calls == 1:
            compiled = statement.compile(dialect=sqlite.dialect())
            values = tuple(compiled.params[key] for key in compiled.positiontup)
            cursor = self.connection.execute(str(compiled), values)
            row = cursor.fetchone()
            record = dict(zip((column[0] for column in cursor.description), row)) if row else None
            if record is not None:
                raw = record['content_json']
                record['content_json'] = json.loads(raw) if raw is not None else None
            return NS(scalar_one_or_none=lambda: record['content_json'] if record else None,
                      one_or_none=lambda: NS(**record) if record is not None else None)
        return NS(all=lambda: self.characters)


def row(id, name, *, confirmed=1, version=1, project='p', level='book', created='2026-10-01'):
    return (id, project, level, confirmed, version, created,
            json.dumps({'entity_registry': {'protagonist': name}}))


@pytest.mark.asyncio
async def test_unconfirmed_latest_draft_and_other_projects_cannot_override_approved_identity():
    db = SourceDB([
        row('approved', '林照'),
        row('draft', '林烨', confirmed=0, version=99),
        row('alien', '别书主角', project='other', version=100),
        row('volume', '卷内替名', level='volume', version=100),
    ])
    try:
        contract = await build_fact_contract(db, 'p')
        assert contract.role_names == {'protagonist': '林照'}
        assert contract.source == 'confirmed_book_outline+characters'
        assert '林烨' not in contract.to_prompt()
    finally:
        db.connection.close()


@pytest.mark.asyncio
async def test_no_confirmed_outline_does_not_promote_a_draft_to_authority():
    db = SourceDB([row('draft', '未确认主角', confirmed=0)],
                  [NS(name='已登记人物', profile_json={})])
    try:
        contract = await build_fact_contract(db, 'p')
        assert contract.role_names == {}
        assert contract.source == 'characters'
        assert contract.registered_character_names == ['已登记人物']
        assert '未确认主角' not in contract.to_prompt()
    finally:
        db.connection.close()


@pytest.mark.asyncio
async def test_confirmed_selection_uses_version_then_creation_then_id():
    rows = [row('old', '旧稿', version=1, created='2026-10-07'),
            row('a', '同版本早稿', version=2, created='2026-10-05'),
            row('b', '同时间前项', version=2, created='2026-10-06'),
            row('c', '当前确认稿', version=2, created='2026-10-06')]
    for order in (rows, list(reversed(rows))):
        db = SourceDB(order)
        try:
            assert (await build_fact_contract(db, 'p')).role_names == {'protagonist': '当前确认稿'}
        finally:
            db.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('profiles', [
    [('林照', {'role': '不是主角'})],
    [('林远山', {'identity': '主角的父亲'})],
    [('林照', {'role': '主角', 'type': '导师'})],
    [('沈听澜', {'role': '导师'}), ('陈川', {'role': '导师'})],
])
async def test_ambiguous_or_descriptive_profiles_do_not_invent_a_unique_role(profiles):
    for order in (profiles, list(reversed(profiles))):
        db = SourceDB([], [NS(name=name, profile_json=profile) for name, profile in order])
        try:
            contract = await build_fact_contract(db, 'p')
            assert contract.role_names == {}
            assert set(contract.registered_character_names) == {name for name, _ in profiles}
        finally:
            db.connection.close()


@pytest.mark.asyncio
async def test_unique_explicit_profile_labels_remain_supported():
    db = SourceDB([], [NS(name='林照', profile_json={'role': ' 主角 '}),
                       NS(name='沈听澜', profile_json={'type': 'MENTOR'})])
    try:
        contract = await build_fact_contract(db, 'p')
        assert contract.role_names == {'protagonist': '林照', 'mentor': '沈听澜'}
        assert contract.role_name_sources == {'protagonist': 'character_profile', 'mentor': 'character_profile'}
    finally:
        db.connection.close()


@pytest.mark.asyncio
async def test_explicit_approved_role_is_not_replaced_by_ambiguous_profiles():
    db = SourceDB([row('approved', '林照')],
                  [NS(name=name, profile_json={'role': '主角'}) for name in ['林照', '沈青']])
    try:
        assert (await build_fact_contract(db, 'p')).role_names['protagonist'] == '林照'
    finally:
        db.connection.close()


def current_row(payload, *, id='current', version=2):
    return (*row(id, 'unused', version=version)[:-1], json.dumps(payload))


@pytest.mark.asyncio
async def test_author_revision_does_not_get_suppressed_slots_back_from_old_profiles():
    payload = {'entity_registry': {'protagonist': '林照', 'mentor': '沈听澜'},
               'raw_text': '林禾暂且独自修船，还没有拜师。', '_author_text_revision': 'saved-author-revision'}
    db = SourceDB([current_row(payload)], [
        NS(name='林照', profile_json={'role': '主角'}),
        NS(name='沈听澜', profile_json={'role': '导师'}),
    ])
    try:
        contract = await build_fact_contract(db, 'p')
        assert contract.role_names == {} and contract.role_name_sources == {}
        assert contract.registered_character_names == ['林照', '沈听澜']
        assert contract.source == 'confirmed_book_outline+characters'
        assert contract.outline_source['id'] == 'current'
        assert contract.outline_source['version'] == 2
        assert contract.outline_source['authority'] == 'author_text'
        assert '登记姓名不能证明当前角色身份' in contract.to_prompt()
    finally:
        db.connection.close()


@pytest.mark.asyncio
async def test_author_explicit_rename_owns_identity_and_source_without_mutating_the_outline():
    payload = {'entity_registry': {'protagonist': '林照'},
               'raw_text': '主角：林禾。', '_author_text_revision': 'saved-author-revision'}
    db = SourceDB([row('old', '林照'), current_row(payload)],
                  [NS(name='林照', profile_json={'role': '主角'})])
    try:
        contract = await build_fact_contract(db, 'p')
        assert contract.role_names == {'protagonist': '林禾'}
        assert contract.role_name_sources == {'protagonist': 'author_text'}
        assert len(contract.outline_source['content_hash']) == 64
        assert db.statements[0].get_execution_options()['autoflush'] is False
        saved = db.connection.execute("SELECT content_json FROM outlines WHERE id = 'current'").fetchone()[0]
        assert json.loads(saved) == payload
    finally:
        db.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    None, {}, [], '主角：林照。', 7,
    {'entity_registry': {'protagonist': '林照'}, 'parse_error': True},
    {'entity_registry': {'protagonist': '林照'}, '_parse_error': True},
    {'entity_registry': {'protagonist': '林照'}, 'available': False},
    {'entity_registry': {'protagonist': '林照'}, 'available': None},
    {'entity_registry': {'protagonist': '林照'}, UNAVAILABLE: True},
    {'entity_registry': {'protagonist': '林照'}, REF: None},
    {'entity_registry': {'protagonist': '林照'}, 'raw_text': ['坏当前文字']},
    {'entity_registry': {'protagonist': '林照'}, 'raw_text': '', '_author_text_revision': 'revision'},
    {'raw_text': ''}, {'raw_text': ' \n ', 'available': True},
    {'title': '只有标题', 'available': True, 'parse_error': False},
    {'entity_registry': {'protagonist': '林照'}, '_quality_status': 'degraded_structural_draft'},
    {'entity_registry': {'protagonist': '林照'}, '_quality_status': 'invalidated_degraded_draft'},
    {'entity_registry': {'protagonist': '林照'}, '_quality_status': 'invalidated_consistency_failed'},
])
async def test_bad_current_confirmed_outline_never_falls_back_to_an_old_outline_or_profiles(payload):
    db = SourceDB([row('old', '林照'), current_row(payload)],
                  [NS(name='旧档案人物', profile_json={'role': '主角'})])
    try:
        with pytest.raises(ValueError, match='当前确认书纲'):
            await build_fact_contract(db, 'p')
        assert db.calls == 1
    finally:
        db.connection.close()


@pytest.mark.asyncio
async def test_sql_null_in_a_saved_outline_is_distinct_from_no_confirmed_outline():
    db = SourceDB([(*row('null-row', 'unused')[:-1], None)])
    try:
        with pytest.raises(ValueError, match='当前确认书纲'):
            await build_fact_contract(db, 'p')
    finally:
        db.connection.close()


@pytest.mark.asyncio
async def test_unique_profile_hint_cannot_pick_one_of_two_outline_mentors():
    db = SourceDB([current_row({'raw_text': '导师：沈听澜。另一位导师：陈川。'})],
                  [NS(name='沈听澜', profile_json={'role': '导师'})])
    try:
        contract = await build_fact_contract(db, 'p')
        assert contract.role_names == {}
        assert contract.outline_role_names == {'mentor': ['沈听澜', '陈川']}
    finally:
        db.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [
    {'raw_text': '林禾独自修船，暂且没有拜师。'},
    {'summary': '林禾独自修船，暂且没有拜师。'},
    {'entity_registry': {'protagonist': '林禾'}},
])
async def test_replacing_outline_without_a_revision_marker_cannot_restore_unconfirmed_profile_slots(replacement):
    old = {'entity_registry': {'mentor': '沈听澜'}, 'raw_text': '导师：沈听澜。'}
    current = record_text_edit(old, replacement)
    assert TEXT_REVISION not in current
    db = SourceDB([current_row(current)], [NS(name='沈听澜', profile_json={'role': '导师'})])
    try:
        contract = await build_fact_contract(db, 'p')
        assert 'mentor' not in contract.role_names
        assert 'mentor' not in contract.role_name_sources
        assert contract.registered_character_names == ['沈听澜']
    finally:
        db.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('summary', [0, False, None, []])
async def test_invalid_explicit_text_field_cannot_disappear_through_a_falsy_fallback(summary):
    db = SourceDB([current_row({'raw_text': '', 'summary': summary,
                               'entity_registry': {'protagonist': '林照'}})])
    try:
        with pytest.raises(ValueError, match='当前确认书纲'):
            await build_fact_contract(db, 'p')
    finally:
        db.connection.close()
