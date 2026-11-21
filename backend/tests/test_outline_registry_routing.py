from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from tests.test_pipeline_control import runtime_db
from tests.test_prompt_cache_transactions import tables
from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset
from app.services import prompt_cache, prompt_registry
from app.services.outline_prompt_runner import OutlinePromptRunner
from app.services.outline_generator import OutlineGenerator


@pytest.mark.asyncio
async def test_stages_inherit_routing_and_deliver_registered_prompt_after_session_close(runtime_db,monkeypatch):
    r=runtime_db; await tables(r); prompt_cache.reset_for_tests()
    async with r.sessions() as db:
        endpoint=LLMEndpoint(name='测试模型',provider_type='openai',default_model='test-model')
        db.add(endpoint); await db.flush()
        for task in ('outline_book','outline_volume'):
            db.add(PromptAsset(task_type=task,name=task,system_prompt='父级完整任务',endpoint_id=endpoint.id,
                               model_name='configured-model',temperature=.4,max_tokens=16000,model_tier='flagship'))
        await db.commit()
        await prompt_registry.PromptRegistry(db).seed_builtins()
        await db.commit()
        stage=await db.scalar(select(PromptAsset).where(PromptAsset.task_type=='outline_book_skeleton'))
        stage_id=stage.id
        assert stage.endpoint_id==endpoint.id and stage.max_tokens==16000
        stage.system_prompt='后台定制骨架要求'
        await db.commit()
        assert await prompt_registry.PromptRegistry(db).seed_builtins() == 0
        assert stage.system_prompt == '后台定制骨架要求'
    opened=[]
    @asynccontextmanager
    async def factory():
        opened.append(True)
        try:
            async with r.sessions() as db: yield db
        finally:
            opened.pop()
    monkeypatch.setattr('app.db.session.async_session_factory',factory)
    async def model(task,messages,**kwargs):
        assert not opened, 'model IO must not keep registry read session open'
        assert task=='outline_book_skeleton'
        assert messages[0]['content']=='后台定制骨架要求'
        assert messages[1]['content']=='本阶段约束'
        assert kwargs['route'].model=='configured-model' and kwargs['route'].max_tokens==16000
        assert kwargs['preferred_tier']=='flagship'
        assert kwargs['_log_meta']['prompt_id']==stage_id
        assert kwargs['_log_meta']['project_id']==str(r.project)
        return SimpleNamespace(text='骨架结果')
    monkeypatch.setattr(prompt_registry,'get_model_router_async',AsyncMock(return_value=SimpleNamespace(generate_with_tier_fallback=model)))
    result=await OutlinePromptRunner(r.project).generate('outline_book_skeleton',[{'role':'system','content':'本阶段约束'}])
    assert result.text=='骨架结果'
    assert isinstance(OutlineGenerator().router,OutlinePromptRunner)
    prompt_cache.reset_for_tests()


def test_registered_prompt_not_duplicated_by_legacy_preloaded_messages():
    messages=[{'role':'system','content':'注册提示词\n\n本阶段约束'},{'role':'user','content':'创意'}]
    result=prompt_registry._prepend_registered_system(messages,'注册提示词','额外约束')
    assert sum(m['content'].count('注册提示词') for m in result)==1
    assert result[0]['content']=='额外约束'
    assert len(messages)==2


@pytest.mark.asyncio
async def test_outline_stream_closes_registry_stream_on_early_close(monkeypatch):
    closed=[]
    async def stream(*args,**kwargs):
        assert args[0]=='outline_volume_batch' and kwargs['project_id']=='project'
        try:
            yield '片段'
            yield '后续'
        finally:
            closed.append(True)
    monkeypatch.setattr(prompt_registry,'stream_text_prompt',stream)
    output=OutlinePromptRunner('project').generate_stream('outline_volume_batch',[])
    assert await anext(output)=='片段'
    await output.aclose()
    assert closed==[True]
