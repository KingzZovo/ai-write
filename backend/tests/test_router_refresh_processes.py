"""Independent processes observe committed endpoint and prompt changes."""
import asyncio
import json
import os
from pathlib import Path
import sys
import textwrap

import pytest

from app.api import model_config
from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset
from app.services import model_router as mr
from tests.test_pipeline_control import runtime_db


CHILD = textwrap.dedent('''
    import asyncio, json, sys
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db import session
    from app.services import model_router as mr
    from app.services.prompt_registry import _resolve_route_and_tier_cached

    async def main():
        engine = create_async_engine(sys.argv[1], connect_args={
            "server_settings": {"search_path": sys.argv[2]}})
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        session.async_session_factory = sessions
        async def generate(self, messages, model, **kwargs):
            return mr.GenerationResult(model, mr.TokenUsage(), model, "offline-test")
        mr.OpenAIProvider.generate = generate
        retained = mr.get_model_router()
        async def report():
            try:
                result = await retained.generate("generation", [])
                model = result.text
            except ValueError:
                model = None
            async with sessions() as db:
                route, _ = await _resolve_route_and_tier_cached("generation", db)
            print(json.dumps({"model": model, "prompt": route.system_prompt,
                              "providers": len(mr._router.providers)}), flush=True)
        try:
            await report()
            for _ in range(3):
                command = await asyncio.to_thread(sys.stdin.readline)
                if command.strip() == "expire":
                    mr._router._loaded_at -= mr.ROUTER_TTL_SECONDS + 1
                await report()
        finally:
            await engine.dispose()
    asyncio.run(main())
''')


@pytest.mark.asyncio
async def test_endpoint_and_prompt_reads_share_one_committed_snapshot(runtime_db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.db import session

    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(LLMEndpoint.__table__.create)
        await conn.run_sync(PromptAsset.__table__.create)
    async with r.sessions() as db:
        endpoint = LLMEndpoint(name="测试", provider_type="openai_compatible",
            base_url="http://localhost:1/v1", api_key="test-only", default_model="old")
        db.add(endpoint)
        await db.flush()
        prompt = PromptAsset(task_type="generation", name="测试", endpoint_id=endpoint.id,
                             system_prompt="old prompt")
        db.add(prompt)
        await db.commit()
        endpoint_id, prompt_id = endpoint.id, prompt.id

    class InterleavedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if not getattr(self, "changed", False):
                self.changed = True
                async with r.sessions() as writer:
                    ep = await writer.get(LLMEndpoint, endpoint_id)
                    ep.default_model = "new"
                    asset = await writer.get(PromptAsset, prompt_id)
                    asset.system_prompt = "new prompt"
                    await writer.commit()
            return result

    monkeypatch.setattr(session, "async_session_factory", async_sessionmaker(
        r.engine, class_=InterleavedSession, expire_on_commit=False))
    snapshot = mr.ModelRouter()
    await snapshot.load_from_db()
    assert snapshot._endpoint_defaults[str(endpoint_id)] == "old"
    assert snapshot._prompt_snapshots[0].system_prompt == "old prompt"
    monkeypatch.setattr(session, "async_session_factory", r.sessions)
    fresh = mr.ModelRouter()
    await fresh.load_from_db()
    assert fresh._endpoint_defaults[str(endpoint_id)] == "new"
    assert fresh._prompt_snapshots[0].system_prompt == "new prompt"


@pytest.mark.asyncio
async def test_two_processes_refresh_default_prompt_and_disabled_endpoint(runtime_db):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(LLMEndpoint.__table__.create)
        await conn.run_sync(PromptAsset.__table__.create)
    async with r.sessions() as db:
        endpoint = LLMEndpoint(name="测试", provider_type="openai_compatible",
            base_url="http://localhost:1/v1", api_key="test-only-key", default_model="old")
        db.add(endpoint)
        await db.flush()
        prompt = PromptAsset(task_type="generation", name="测试", endpoint_id=endpoint.id,
                             model_name="", system_prompt="old prompt")
        db.add(prompt)
        await db.commit()
        endpoint_id, prompt_id = endpoint.id, prompt.id

    children = []

    async def response(child):
        raw = await asyncio.wait_for(child.stdout.readline(), 15)
        assert raw, "router subprocess exited before sending its result"
        return json.loads(raw)

    async def command(child, value):
        child.stdin.write((value + "\n").encode())
        await child.stdin.drain()
        return await response(child)

    try:
        for _ in range(2):
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", CHILD, os.environ["PIPELINE_TEST_DATABASE_URL"], r.schema,
                cwd=str(Path(__file__).resolve().parents[1]),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            children.append(child)
            assert await response(child) == {"model": "old", "prompt": "old prompt", "providers": 1}

        async with r.sessions() as db:
            # API invalidates only the parent process; children get no signal.
            await model_config.update_endpoint(endpoint_id,
                model_config.EndpointUpdate(default_model="new"), db)
            prompt = await db.get(PromptAsset, prompt_id)
            prompt.system_prompt = "new prompt"
            await db.commit()
        for child in children:
            assert await command(child, "warm") == {
                "model": "old", "prompt": "old prompt", "providers": 1}
            assert await command(child, "expire") == {
                "model": "new", "prompt": "new prompt", "providers": 1}

        async with r.sessions() as db:
            await model_config.update_endpoint(endpoint_id,
                model_config.EndpointUpdate(enabled=0), db)
        for child in children:
            assert await command(child, "expire") == {
                "model": None, "prompt": "new prompt", "providers": 0}
            assert await asyncio.wait_for(child.wait(), 10) == 0
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.wait()
