"""Current rules must retain conditions and their actual registered sources."""
import pytest
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.project import Project, Outline, WorldRule
from app.services.world_rule_sources import read_world_rule_source
from app.services.world_rule_material import WorldRuleSourceError, world_rule_material, render_world_rules, rule_from_properties, world_rule_version
from app.services.world_rule_writes import write_world_rule
from app.services.outline_settings_source import read_settings_source, settings_material_hash
from tests.support.world_rules import RuleGraph
from tests.test_generation_execution import LocalSession


@pytest.fixture
def rule_book(tmp_path, monkeypatch):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "world-rule-source.sqlite"}')
    for model in (Project, Outline, WorldRule): model.__table__.create(engine)
    pid, foreign = uuid4(), uuid4()
    with engine.begin() as db:
        db.execute(sa.insert(Project), [{'id': pid, 'title': '渡口规则', 'target_word_count': 5_000_000},
                                       {'id': foreign, 'title': '另一部书', 'target_word_count': 5_000_000}])
    graph = RuleGraph(pid)
    async def driver(): return graph
    monkeypatch.setattr('app.services.world_rule_sources.current_rule_driver', driver)
    @asynccontextmanager
    async def sessions():
        with Session(engine, expire_on_commit=False) as db:
            yield LocalSession(db)
    yield NS(engine=engine, pid=pid, foreign=foreign, graph=graph, sessions=sessions)
    engine.dispose()


def registered_rule(r, **values):
    row = {'id': str(uuid4()), 'category': '水利与航运', 'text': '封航期间不得载客过河。', 'origin': 'author', **values}
    r.graph.rules.append(row)
    return row


def book_outline(r, **values):
    oid = uuid4()
    content = values.pop('content_json', {'raw_text': '原始书纲。\n铜铃损坏后不能复原；新弧不得恢复它。'})
    with r.engine.begin() as db:
        db.execute(sa.insert(Outline).values(id=oid, project_id=r.pid, level='book', version=1,
                                             is_confirmed=1, content_json=content, **values))
    return oid, content


async def source(r):
    async with r.sessions() as db: return await read_world_rule_source(db, r.pid)


def version(row):
    return world_rule_version(rule_from_properties(row))


@pytest.mark.parametrize('original,other', [
    ('渡口铜铃每日只能敲一次，必须由守铃人亲手敲响，其他人不得代敲。',
     '渡口铜铃每日只能敲一次，必须由守铃人亲手敲响，其他人不得代敲。洪水期间由守堤人负责。'),
    ('灵力耗尽以后，无论是白天还是黑夜，七天之内不得再次施展空间转移术。',
     '灵力耗尽以后，无论是白天还是黑夜，七天之内可以再次施展空间转移术。'),
])
def test_projection_never_treats_an_exception_or_negation_as_permission_to_replace_a_rule(original, other):
    from app.tasks.entity_tasks import _plan_world_rule_writes
    inserts, updates = _plan_world_rule_writes([('能力限制', original)], [('能力限制', original), ('能力限制', other)])
    assert inserts == [('能力限制', other)]
    assert updates == []


@pytest.mark.asyncio
async def test_actual_rule_list_and_writer_read_current_graph_values_instead_of_stale_pg_rows(rule_book):
    from app.api.settings import list_world_rules
    from app.services.context_pack import ContextPack, ContextPackBuilder
    r = rule_book
    text = '铜铃已断，不能再次施术。\n' + '洪水期间须沿原堤撤离，不能临时增加渡船。' * 170
    row = registered_rule(r, text=text)
    registered_rule(r, project_id=str(r.foreign), text='另一本书的规则不能混入')
    with r.engine.begin() as db:
        db.execute(sa.insert(WorldRule).values(project_id=r.pid, category=row['category'], rule_text='旧副本称铜铃可以复原。'))
    async with r.sessions() as db:
        response = await list_world_rules(r.pid, db)
        pack = ContextPack()
        await ContextPackBuilder(db=db)._build_world_rules(pack, r.pid)
    assert response.total == 1 and response.world_rules[0].rule_text == text
    assert response.world_rules[0].id == UUID(row['id'])
    assert response.world_rules[0].version == version(row)
    assert text in '\n'.join(pack.world_rules)
    assert '旧副本' not in '\n'.join(pack.world_rules) and '另一本书' not in '\n'.join(pack.world_rules)
    r.graph.rules.remove(row)
    async with r.sessions() as db:
        assert (await list_world_rules(r.pid, db)).total == 0
        await ContextPackBuilder(db=db)._build_world_rules(pack, r.pid)
    assert text not in '\n'.join(pack.world_rules)
    with Session(r.engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(WorldRule)) == 1  # Old PG data was not deleted.


@pytest.mark.asyncio
async def test_more_than_two_thousand_similar_registered_rules_are_preserved_in_full(rule_book):
    r = rule_book
    for n in range(2101): registered_rule(r, text=f'第{n}处河段封航，解除须有本段水位记录；不能借用别处测量。')
    result = render_world_rules(await source(r), r.pid)
    assert len(result) == 2101
    assert all(any(row['text'] in part for part in result) for row in (r.graph.rules[0], r.graph.rules[1050], r.graph.rules[-1]))


@pytest.mark.asyncio
async def test_graph_read_failure_does_not_fall_back_to_old_pg_rule_or_expose_diagnostics(rule_book):
    from app.services.context_pack import ContextPack, ContextPackBuilder
    r = rule_book
    with r.engine.begin() as db:
        db.execute(sa.insert(WorldRule).values(project_id=r.pid, category='旧数据', rule_text='不允许当作图谱成功读取。'))
    r.graph.failure = RuntimeError('private graph endpoint diagnostic')
    pack = ContextPack(world_rules=['already prepared caller material'])
    async with r.sessions() as db:
        with pytest.raises(WorldRuleSourceError) as caught:
            await ContextPackBuilder(db=db)._build_world_rules(pack, r.pid)
    assert caught.value.status == 503 and 'private' not in str(caught.value)
    assert pack.world_rules == ['already prepared caller material']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['text', 'structure', 'selected_outline', 'deleted'])
async def test_changed_book_material_keeps_rule_visible_but_stops_automatic_use_until_author_adoption(rule_book, change):
    from app.api.settings import list_world_rules
    r = rule_book; oid, content = book_outline(r)
    row = registered_rule(r, origin='outline_extraction', source_outline_id=str(oid),
                          source_outline_material_hash=settings_material_hash(content), source_outline_digest='a' * 64)
    assert world_rule_material(await source(r), r.pid)[0]['source_status'] == 'current_outline'
    with r.engine.begin() as db:
        if change == 'deleted': db.execute(sa.delete(Outline).where(Outline.id == oid))
        elif change == 'selected_outline':
            db.execute(sa.insert(Outline).values(id=uuid4(), project_id=r.pid, level='book', version=2, is_confirmed=1,
                                                content_json=content))
        else:
            revised = {**content, **({'raw_text': '作者改写了本书的能力边界。'} if change == 'text' else {'main_plot': '新的完整安排'})}
            db.execute(sa.update(Outline).where(Outline.id == oid).values(content_json=revised))
    current = await source(r)
    with pytest.raises(WorldRuleSourceError, match='核对'): render_world_rules(current, r.pid)
    async with r.sessions() as db:
        listing = await list_world_rules(r.pid, db)
        assert listing.world_rules[0].source_status == 'needs_review'
        assert listing.world_rules[0].rule_text == row['text']
        await write_world_rule(db, r.graph, r.pid, 'update', category=row['category'], text=row['text'],
            new_category=row['category'], new_text=row['text'], expected_version=listing.world_rules[0].version)
        await db.commit()
    assert row['source_outline_id'] == str(oid)  # The old provenance remains historical evidence.
    assert row['origin'] == 'author' and world_rule_material(await source(r), r.pid)[0]['source_status'] == 'author'


@pytest.mark.asyncio
async def test_material_fingerprint_does_not_expire_for_unrelated_project_counters(rule_book):
    r = rule_book; oid, content = book_outline(r)
    registered_rule(r, origin='outline_extraction', source_outline_id=str(oid),
                    source_outline_material_hash=settings_material_hash(content), source_outline_digest='b' * 64)
    before = await source(r)
    with r.engine.begin() as db:
        db.execute(sa.update(Project).where(Project.id == r.pid).values(settings_json={'unused_runtime_counter': 57}))
    assert await source(r) == before
    assert world_rule_material(before, r.pid)[0]['source_status'] == 'current_outline'


@pytest.mark.asyncio
async def test_old_extraction_digest_is_verified_without_inventing_a_new_material_hash(rule_book):
    r = rule_book; oid, content = book_outline(r)
    async with r.sessions() as db: previous = await read_settings_source(db, r.pid, oid)
    row = registered_rule(r, origin=None, source_outline_id=str(oid), source_outline_digest=previous.digest)
    assert world_rule_material(await source(r), r.pid)[0]['source_status'] == 'current_outline'
    assert 'source_outline_material_hash' not in row
    with r.engine.begin() as db:
        db.execute(sa.update(Outline).where(Outline.id == oid).values(content_json={'raw_text': '作者的新版书纲'}))
    with pytest.raises(WorldRuleSourceError): render_world_rules(await source(r), r.pid)


@pytest.mark.asyncio
async def test_unknown_historical_registration_is_labeled_and_does_not_gain_an_invented_origin(rule_book):
    r = rule_book; row = registered_rule(r, id=None, origin=None)
    before = deepcopy(row)
    prepared = await source(r)
    assert world_rule_material(prepared, r.pid)[0]['source_status'] == 'registered'
    assert '提取出处未知' in render_world_rules(prepared, r.pid)[0]
    assert row == before


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['update', 'delete'])
async def test_stale_version_and_delete_then_recreate_cannot_modify_new_rule(rule_book, operation):
    r = rule_book; row = registered_rule(r); old_version = version(row)
    row['id'] = str(uuid4())  # A new node with the same human-readable key.
    before = deepcopy(r.graph.rules)
    async with r.sessions() as db:
        with pytest.raises(WorldRuleSourceError) as caught:
            await write_world_rule(db, r.graph, r.pid, operation, category=row['category'], text=row['text'],
                new_category=row['category'], new_text='旧页面不能写到新节点', expected_version=old_version)
    assert caught.value.code == 'world_rule_source_changed'
    assert r.graph.rules == before


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['update', 'delete'])
async def test_missing_expected_version_stops_before_any_graph_call(rule_book, operation):
    r = rule_book; row = registered_rule(r)
    async with r.sessions() as db:
        with pytest.raises(WorldRuleSourceError) as caught:
            await write_world_rule(db, r.graph, r.pid, operation, category=row['category'], text=row['text'],
                new_category=row['category'], new_text='未经来源核对的修改')
    assert caught.value.status == 428 and not r.graph.calls


@pytest.mark.asyncio
async def test_identical_registration_returns_the_real_stable_graph_id(rule_book):
    r = rule_book; row = registered_rule(r)
    async with r.sessions() as db:
        result = await write_world_rule(db, r.graph, r.pid, 'create', category=row['category'], text=row['text'])
    assert result['id'] == row['id'] and len(r.graph.rules) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('flags', [{'available': False}, {'available': 'false'}, {'parse_error': True},
                                 {'_parse_error': True}, {'_order_unavailable': True},
                                 {'_order_unavailable': True, '_author_text_revision': 'saved'},
                                 {'_chapter_id': '', '_author_text_revision': 'saved'}])
@pytest.mark.parametrize('legacy', [False, True])
async def test_unusable_current_book_cannot_validate_new_or_legacy_extraction(rule_book, flags, legacy):
    r = rule_book
    oid, content = book_outline(r, content_json={'raw_text': '作者留下的完整原书纲。', **flags})
    async with r.sessions() as db:
        old_source = await read_settings_source(db, r.pid, oid)
    values = {'source_outline_digest': old_source.digest}
    if not legacy:
        # A persisted fingerprint cannot certify an explicitly unusable source.
        values['source_outline_material_hash'] = settings_material_hash(content) or 'a' * 64
    registered_rule(r, origin='outline_extraction', source_outline_id=str(oid), **values)
    assert settings_material_hash(content) is None
    current = await source(r)
    assert world_rule_material(current, r.pid, require_current=False)[0]['source_status'] == 'needs_review'
    with pytest.raises(WorldRuleSourceError, match='核对'):
        render_world_rules(current, r.pid)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['target', 'original'])
async def test_merge_requires_both_target_adoption_and_original_deletion_confirmation(rule_book, missing):
    r = rule_book
    original = registered_rule(r)
    target = registered_rule(r, text='另一条完整规则。', origin='outline_extraction')
    before = deepcopy(r.graph.rules)
    async def change(query, params):
        if (missing == 'target' and 'SET w.id=' in query or missing == 'original' and 'DETACH DELETE' in query):
            r.graph.rules.remove(target if missing == 'target' else original)
    r.graph.before_run = change
    async with r.sessions() as db:
        with pytest.raises(WorldRuleSourceError) as caught:
            await write_world_rule(db, r.graph, r.pid, 'update', category=original['category'], text=original['text'],
                new_category=target['category'], new_text=target['text'], expected_version=version(original))
    assert caught.value.status == 503
    assert r.graph.rules == before


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'update', 'delete'])
async def test_duplicate_graph_keys_are_not_silently_adopted_or_modified(rule_book, operation):
    r = rule_book
    row = registered_rule(r)
    registered_rule(r, origin='outline_extraction')
    before = deepcopy(r.graph.rules)
    async with r.sessions() as db:
        with pytest.raises(WorldRuleSourceError):
            await write_world_rule(db, r.graph, r.pid, operation, category=row['category'], text=row['text'],
                new_category='渡口', new_text='不覆盖重复记录。', expected_version=version(row))
    assert r.graph.rules == before


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['update', 'delete'])
async def test_legacy_uncategorized_rule_uses_its_exact_original_key(rule_book, operation):
    r = rule_book
    row = registered_rule(r, category='', text='  原规则第一行。\n例外须保留。\n')
    old_id = row['id']
    async with r.sessions() as db:
        await write_world_rule(db, r.graph, r.pid, operation, category=row['category'], text=row['text'],
            new_category='渡口', new_text='作者核对后的规则。\n例外仍然保留。', expected_version=version(row))
    if operation == 'delete':
        assert r.graph.rules == []
    else:
        assert len(r.graph.rules) == 1 and r.graph.rules[0]['id'] == old_id
        assert r.graph.rules[0]['text'] == '作者核对后的规则。\n例外仍然保留。'
