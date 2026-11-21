"""Prompt mutation commit ordering and in-flight read invalidation."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import select
from tests.test_pipeline_control import runtime_db
from tests.test_c3_prompt_cache import _make_asset, _fake_db_returning
from app.services import prompt_cache
from app.api import prompts
from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset


@pytest.fixture(autouse=True)
def reset():
    prompt_cache.reset_for_tests()
    yield
    prompt_cache.reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('all_tasks', [False, True])
async def test_read_started_before_invalidation_cannot_overwrite_new_cache(missing, all_tasks):
    started=asyncio.Event(); resume=asyncio.Event()
    old=None if missing else _make_asset(task_type='outline_book', system_prompt='旧版')
    new=_make_asset(task_type='outline_book', system_prompt='新版')
    async def execute(*args):
        started.set()
        await resume.wait()
        result=MagicMock(); result.scalar_one_or_none.return_value=old
        return result
    stale=asyncio.create_task(prompt_cache.get_snapshot('outline_book', type('DB',(),{'execute':staticmethod(execute)})()))
    await started.wait()
    prompt_cache.invalidate(None if all_tasks else 'outline_book')
    fresh=await prompt_cache.get_snapshot('outline_book',_fake_db_returning(new))
    resume.set(); await stale
    no_db=AsyncMock()
    assert await prompt_cache.get_snapshot('outline_book',no_db) == fresh
    no_db.execute.assert_not_called()


async def tables(r):
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: LLMEndpoint.__table__.create(c))
        await conn.run_sync(lambda c: PromptAsset.__table__.create(c))


@pytest.mark.asyncio
async def test_crud_invalidates_after_commit_and_other_connections_see_changes(runtime_db,monkeypatch):
    r=runtime_db; await tables(r)
    invalidated=[]
    original=prompt_cache.invalidate
    async with r.sessions() as db:
        def invalidate(task=None):
            assert not db.in_transaction(), 'invalidation must follow database commit'
            invalidated.append(task); original(task)
        monkeypatch.setattr(prompt_cache,'invalidate',invalidate)
        created=await prompts.create_prompt(prompts.PromptCreate(task_type='outline_book',name='测试',system_prompt='旧版'),db,True)
        async with r.sessions() as reader:
            assert (await reader.get(PromptAsset,created.id)).system_prompt == '旧版'
        updated=await prompts.update_prompt(created.id,prompts.PromptUpdate(system_prompt='新版'),db,True)
        assert updated.system_prompt == '新版'
        async with r.sessions() as reader:
            assert (await reader.get(PromptAsset,created.id)).system_prompt == '新版'
        await prompts.delete_prompt(created.id,db)
        async with r.sessions() as reader:
            assert await reader.get(PromptAsset,created.id) is None
        assert invalidated == ['outline_book']*3


@pytest.mark.asyncio
async def test_failed_commit_does_not_invalidate(monkeypatch):
    invalidate=MagicMock(); monkeypatch.setattr(prompt_cache,'invalidate',invalidate)
    with pytest.raises(RuntimeError,match='commit failed'):
        await prompts._commit_prompt_change(AsyncMock(commit=AsyncMock(side_effect=RuntimeError('commit failed'))),'outline_book')
    invalidate.assert_not_called()


@pytest.mark.asyncio
async def test_auto_seed_commits_before_invalidating(runtime_db,monkeypatch):
    r=runtime_db; await tables(r)
    async def seed(registry):
        registry.db.add(PromptAsset(task_type='outline_book',name='种子',system_prompt='已提交种子'))
        return 1
    monkeypatch.setattr(prompts.PromptRegistry,'seed_builtins',seed)
    async with r.sessions() as db:
        invalidations=[]
        def invalidate(task=None):
            assert not db.in_transaction()
            invalidations.append(task)
        monkeypatch.setattr(prompt_cache,'invalidate',invalidate)
        results=await prompts.list_prompts(db)
        assert len(results)==1 and invalidations==[None]
        async with r.sessions() as reader:
            assert await reader.scalar(select(PromptAsset.system_prompt)) == '已提交种子'
