"""Real PostgreSQL coverage of aggregate source receipts and concurrent refresh."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update, delete, func
from app.models.project import Chapter, Volume, VolumeSummary, Project
from app.services import aggregate_memory as am
from app.services.summary_provenance import summary_source_hash
from tests.test_pipeline_control import runtime_db

async def seed(r):
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: VolumeSummary.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.content_text = '主人公尚未渡河。' * 100
        ch.summary = '主人公在岸边等候。'
        ch.status = 'completed'
        ch.global_idx = 1
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        first = ch.volume_id
        second = Volume(project_id=r.project, volume_idx=2, title='第二卷')
        db.add(second)
        await db.commit()
        return first, second.id

def model(monkeypatch, text='第一卷事实梗概。'):
    generate = AsyncMock(return_value=SimpleNamespace(text=text))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    return generate

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'body', 'summary', 'title', 'draft', 'add', 'delete', 'reorder', 'volume_title', 'tamper', 'legacy'])
async def test_volume_read_requires_current_sources(runtime_db, monkeypatch, change):
    r = runtime_db
    first, _ = await seed(r)
    model(monkeypatch)
    async with r.sessions() as db:
        assert (await am.generate_volume_memory(db, first, persist=True))[0]
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        if change == 'body': ch.content_text += '作者改稿'
        if change == 'summary': ch.summary = '被篡改摘要'
        if change == 'title': ch.title = '新标题'
        if change == 'draft': ch.status = 'draft'
        if change == 'reorder': ch.global_idx = 99
        if change == 'add': db.add(Chapter(volume_id=first, chapter_idx=2, title='新增未写章'))
        if change == 'delete': await db.execute(delete(Chapter).where(Chapter.id == ch.id))
        if change == 'volume_title': (await db.get(Volume, first)).title = '新卷名'
        if change in ('tamper', 'legacy'):
            vs = await db.scalar(select(VolumeSummary))
            if change == 'tamper': vs.summary_text = '未绑定的新输出'
            else: vs.source_manifest = None
        await db.commit()
    async with r.sessions() as db:
        rows = await am.current_volume_summaries(db, r.project, before_volume_idx=2, before_global_idx=2)
        assert bool(rows) == (change == 'none')

@pytest.mark.asyncio
async def test_refresh_stale_and_reuse_current(runtime_db, monkeypatch):
    r = runtime_db
    first, _ = await seed(r)
    gen = model(monkeypatch)
    async with r.sessions() as db:
        db.add(VolumeSummary(volume_id=first, summary_text='未知来源'))
        await db.commit()
        assert (await am.generate_volume_memory(db, first, persist=True))[0]
        assert not (await am.generate_volume_memory(db, first, persist=True))[0]
        assert await db.scalar(select(func.count()).select_from(VolumeSummary)) == 1
    gen.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize('edit', ['body', 'new_chapter', 'pending', 'empty'])
async def test_generation_rejects_changed_source(runtime_db, monkeypatch, edit):
    r = runtime_db
    first, _ = await seed(r)
    async def generate(**kwargs):
        async with r.sessions() as author:
            if edit == 'body': await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='新稿'))
            if edit == 'new_chapter': author.add(Chapter(volume_id=first, chapter_idx=2, title='漏章'))
            await author.commit()
        return SimpleNamespace(text='' if edit == 'empty' else '迟到汇总')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    async with r.sessions() as db:
        if edit == 'pending': (await db.get(Chapter, r.chapter)).title = '待提交'
        assert await am.generate_volume_memory(db, first, persist=True) == (False, '')
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(VolumeSummary)) == 0

@pytest.mark.asyncio
async def test_concurrent_backfill_creates_one_row(runtime_db, monkeypatch):
    r = runtime_db
    first, _ = await seed(r)
    count = 0
    barrier = asyncio.Event()
    async def generate(**kwargs):
        nonlocal count
        count += 1
        if count == 2: barrier.set()
        await asyncio.wait_for(barrier.wait(), 10)
        return SimpleNamespace(text='并发生成梗概')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    async def run():
        async with r.sessions() as db: return await am.generate_volume_memory(db, first, persist=True)
    results = await asyncio.gather(run(), run())
    assert sum(written for written, _ in results) == 1
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VolumeSummary)) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'body', 'future', 'tamper', 'legacy', 'missing_middle'])
async def test_book_read_validates_sources_and_boundary(runtime_db, monkeypatch, change):
    r = runtime_db
    first, _ = await seed(r)
    model(monkeypatch, '完整梗概' * 300)
    async with r.sessions() as db:
        await am.generate_volume_memory(db, first, persist=True)
        assert len(await am.generate_book_memory(db, r.project)) > 800
    async with r.sessions() as db:
        if change == 'body': (await db.get(Chapter, r.chapter)).content_text += '改稿'
        if change == 'missing_middle': db.add(Volume(project_id=r.project, volume_idx=0, title='新前卷'))
        if change in ('tamper', 'legacy'):
            project = await db.get(Project, r.project)
            settings = dict(project.settings_json or {})
            stored = dict(settings['book_synopsis'])
            if change == 'tamper': stored['text'] = '换了输出'
            else: stored.pop('sources')
            settings['book_synopsis'] = stored
            project.settings_json = settings
        await db.commit()
        result = await am.current_book_synopsis(db, r.project,
            before_volume_idx=1 if change == 'future' else 2, before_global_idx=2)
        assert bool(result) == (change == 'none')

@pytest.mark.asyncio
@pytest.mark.parametrize('edit', ['source', 'settings'])
async def test_book_does_not_overwrite_concurrent_edits(runtime_db, monkeypatch, edit):
    r = runtime_db
    first, _ = await seed(r)
    model(monkeypatch)
    async with r.sessions() as db: await am.generate_volume_memory(db, first, persist=True)
    async def generate(**kwargs):
        async with r.sessions() as author:
            if edit == 'source': await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='作者改稿'))
            else: await author.execute(update(Project).where(Project.id == r.project).values(settings_json={'author': '保留'}))
            await author.commit()
        return SimpleNamespace(text='过期梗概')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    async with r.sessions() as db: assert await am.generate_book_memory(db, r.project) == ''
    async with r.sessions() as db:
        settings = (await db.get(Project, r.project)).settings_json or {}
        assert 'book_synopsis' not in settings
        if edit == 'settings': assert settings['author'] == '保留'

@pytest.mark.asyncio
async def test_backfill_wrapper_refreshes_synopsis(runtime_db, monkeypatch):
    from app.services.memory import backfill_prev_volume_summary
    r = runtime_db
    _, second = await seed(r)
    model(monkeypatch)
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    assert await backfill_prev_volume_summary(str(second))
    assert not await backfill_prev_volume_summary(str(second))
    async with r.sessions() as db:
        assert await am.current_book_synopsis(db, r.project, before_volume_idx=2, before_global_idx=2)

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'body', 'future'])
async def test_verified_rollups_reach_context_only_before_target(runtime_db, monkeypatch, change):
    from app.models.project import Outline
    from app.services.context_pack import ContextPack, ContextPackBuilder
    r = runtime_db
    first, second = await seed(r)
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: Outline.__table__.create(sync, checkfirst=True))
    generate = model(monkeypatch, '来自完整来源的梗概')
    async with r.sessions() as db:
        db.add(Chapter(volume_id=second, chapter_idx=1, global_idx=2, title='当前章'))
        await db.commit()
        await am.generate_volume_memory(db, first, persist=True)
        await am.generate_book_memory(db, r.project)
        await am.generate_book_memory(db, r.project)
        assert generate.await_count == 2  # Reuse neither calls model nor rewrites settings.
    async with r.sessions() as db:
        if change == 'body':
            await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='新稿'))
            await db.commit()
        pack = ContextPack(history_before_global_idx=1 if change == 'future' else 2)
        await ContextPackBuilder(db=db)._build_proximity(pack, r.project, second, 1)
        assert bool(pack.book_synopsis) == (change == 'none')
        assert any('[前卷摘要]' in text for text in pack.recent_summaries) == (change == 'none')

@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['no_previous', 'empty', 'error'])
async def test_backfill_failures_do_not_create_memory(runtime_db, monkeypatch, case):
    from app.services.memory import backfill_prev_volume_summary
    r = runtime_db
    first, second = await seed(r)
    generate = model(monkeypatch, '')
    if case == 'error': generate.side_effect = RuntimeError('provider down')
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    assert not await backfill_prev_volume_summary(str(first if case == 'no_previous' else second))
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VolumeSummary)) == 0

@pytest.mark.asyncio
async def test_migration_does_not_stamp_legacy_summaries(runtime_db):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pathlib import Path
    r = runtime_db
    first, _ = await seed(r)
    async with r.sessions() as db:
        db.add(VolumeSummary(volume_id=first, summary_text='作者已有摘要'))
        await db.commit()
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/a1001923_aggregate_memory_sources.py'
    spec = importlib.util.spec_from_file_location('aggregate_migration', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    async with r.engine.begin() as conn:
        def migrate(sync):
            module.op = Operations(MigrationContext.configure(sync))
            module.downgrade(); module.upgrade(); module.upgrade()
        await conn.run_sync(migrate)
    async with r.sessions() as db:
        row = await db.scalar(select(VolumeSummary))
        assert row.summary_text == '作者已有摘要'
        assert row.source_manifest is None and row.source_hash is None
        assert await am.current_volume_summaries(db, r.project) == []

@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['normal', 'unknown', 'future'])
async def test_legacy_reader_checks_global_boundary(runtime_db, monkeypatch, case):
    from app.services.memory import HierarchicalMemory
    r = runtime_db
    first, second = await seed(r)
    model(monkeypatch)
    async with r.sessions() as db:
        db.add(Chapter(volume_id=second, chapter_idx=1, title='当前章',
                       global_idx={'normal': 2, 'unknown': None, 'future': 1}[case]))
        await db.commit()
        if case == 'unknown':
            # Chapter insert listeners normally assign an index; emulate legacy unknown metadata.
            await db.execute(update(Chapter).where(Chapter.volume_id == second).values(global_idx=None))
            await db.commit()
        await am.generate_volume_memory(db, first, persist=True)
        result = await HierarchicalMemory(db)._gather_volume_summaries(str(r.project), str(second))
        assert bool(result) == (case == 'normal')

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['volume', 'book'])
async def test_generation_does_not_commit_changes_added_during_model_io(runtime_db, monkeypatch, kind):
    r = runtime_db
    first, _ = await seed(r)
    model(monkeypatch)
    if kind == 'book':
        async with r.sessions() as db: await am.generate_volume_memory(db, first, persist=True)
    async def generate(**kwargs):
        ch = await kwargs['db'].get(Chapter, r.chapter)
        ch.content_text = '尚未提交的新稿'
        return SimpleNamespace(text='模型摘要')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    async with r.sessions() as db:
        result = await (am.generate_volume_memory(db, first, persist=True) if kind == 'volume'
                        else am.generate_book_memory(db, r.project))
        assert result == ((False, '') if kind == 'volume' else '')
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text != '尚未提交的新稿'

@pytest.mark.asyncio
async def test_book_prefix_stops_at_nonmonotonic_global_order(runtime_db, monkeypatch):
    r = runtime_db
    first, second = await seed(r)
    model(monkeypatch)
    async with r.sessions() as db:
        ch = Chapter(volume_id=second, chapter_idx=1, global_idx=1, title='重复全书章号',
                     status='completed', content_text='正文', summary='摘要')
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        db.add(ch)
        await db.commit()
        await am.generate_volume_memory(db, first, persist=True)
        await am.generate_volume_memory(db, second, persist=True)
        assert len(await am.volume_prefix(db, r.project)) == 1
