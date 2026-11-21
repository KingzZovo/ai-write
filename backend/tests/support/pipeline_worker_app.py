"""Real Celery/Beat bootstrap with only model IO replaced for fault tests.

This module refuses default databases/brokers. It is intentionally located in
`tests`, with no production environment switch or fake-generation code path.
"""
import asyncio
import os
import re
from urllib.parse import urlparse

from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

schema = os.environ['PIPELINE_PROCESS_TEST_SCHEMA']
url = os.environ['PIPELINE_TEST_DATABASE_URL']
broker = os.environ['PIPELINE_TEST_REDIS_URL']
parsed = make_url(url)
redis_url = urlparse(broker)
if (not re.fullmatch(r'pipeline_test_[0-9a-f]{32}', schema)
        or parsed.host not in ('127.0.0.1', 'localhost') or parsed.database != 'aiwrite_pipeline_test'
        or redis_url.hostname not in ('127.0.0.1', 'localhost') or redis_url.port == 6379):
    raise RuntimeError('Process fixtures require explicit isolated local services')

from app.db import session
from app.models.generation_task import GenerationTask
from app.models.project import Chapter
from app.tasks import celery_app, generation_tasks


def build_test_engine():
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    session._state.engine = engine
    session._state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)


session._build = build_test_engine
celery_app.conf.update(
    broker_url=broker, result_backend=broker,
    task_default_queue=schema + '-prose',
    task_routes={'tasks.dispatch_pipeline_commands': {'queue': schema + '-control'}},
    beat_schedule={'test-pipeline-dispatch': {'task': 'tasks.dispatch_pipeline_commands', 'schedule': 0.5}},
    broker_transport_options={'global_keyprefix': schema + ':', 'visibility_timeout': 30},
    result_backend_transport_options={'global_keyprefix': schema + ':'},
)


async def fake_model(task_id, *, session_factory):
    """Keep adapter/fence/transactions real; replace expensive model service."""
    client = Redis.from_url(broker, decode_responses=True)
    try:
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            task.status = 'running'
            chapter_id = task.params_json['chapter_id']
            await db.commit()
        await client.rpush(schema + ':calls', chapter_id)
        await client.set(schema + ':entered:' + chapter_id, '1', ex=600)
        while await client.get(schema + ':hold') == chapter_id and not await client.get(schema + ':release'):
            await asyncio.sleep(0.05)
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            chapter = await db.get(Chapter, chapter_id)
            chapter.content_text = f'隔离测试正文：{chapter.title}。'
            chapter.word_count = len(chapter.content_text)
            chapter.status, task.status = 'completed', 'completed'
            await db.commit()
    finally:
        await client.aclose()


generation_tasks._run_async_generation_impl = fake_model
