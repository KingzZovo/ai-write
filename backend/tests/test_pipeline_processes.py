"""Real Redis, Beat and prefork workers: delivery, pause and SIGKILL recovery.

Requires a disposable local Redis on a non-default port. Subprocesses are
created in private process groups and only those groups are killed/cleaned.
All SQL uses runtime_db's random schema. No real model calls occur.
"""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

from celery import Celery
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db
from tests.test_pipeline_runtime import three_volumes
from app.models.pipeline import PipelineRun
from app.services.pipeline_service import get_pipeline_status
from app.services.production.control import request_pause, request_start


async def until(check, *, timeout=30):
    async with asyncio.timeout(timeout):
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.1)


@pytest_asyncio.fixture
async def cluster(runtime_db, tmp_path):
    broker = os.environ.get('PIPELINE_TEST_REDIS_URL')
    if not broker:
        pytest.skip('Requires explicit disposable PIPELINE_TEST_REDIS_URL')
    parsed = urlparse(broker)
    if parsed.hostname not in ('localhost', '127.0.0.1') or parsed.port == 6379:
        pytest.fail('Refusing default/non-local Redis')
    r = runtime_db
    await three_volumes(r)
    client = Redis.from_url(broker, decode_responses=True)
    await client.ping()
    env = {**os.environ, 'DATABASE_URL': os.environ['PIPELINE_TEST_DATABASE_URL'],
           'REDIS_URL': broker, 'PIPELINE_PROCESS_TEST_SCHEMA': r.schema, 'PYTHONPATH': '.',
           'C_FORCE_ROOT': '1', 'SECRET_KEY': 'isolated-process-tests'}
    processes, logs = [], []
    cwd = Path(__file__).resolve().parents[1]
    def launch(role):
        log = open(tmp_path / f'{role}-{len(processes)}.log', 'w+')
        args = [sys.executable, '-m', 'celery', '-A', 'tests.support.pipeline_worker_app:celery_app']
        if role == 'beat':
            args += ['beat', '--loglevel=WARNING', '--schedule', str(tmp_path / 'beat-schedule')]
        else:
            queue = r.schema + ('-control' if role == 'control' else '-prose')
            args += ['worker', '--pool=prefork', '--concurrency=1', '--loglevel=WARNING',
                     '--without-gossip', '--without-mingle', '--without-heartbeat', '-Q', queue,
                     '-n', role + '@%h']
        process = subprocess.Popen(args, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        processes.append(process)
        logs.append(log)
        return process
    def kill(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    sender = Celery('pipeline-test-sender', broker=broker, backend=broker)
    sender.conf.broker_transport_options = {'global_keyprefix': r.schema + ':'}
    sender.conf.result_backend_transport_options = {'global_keyprefix': r.schema + ':'}
    def deliver(token):
        return sender.send_task('tasks.run_pipeline_generation', args=[str(r.pipeline), str(token)],
                         queue=r.schema + '-prose')
    try:
        worker = launch('prose')
        control = launch('control')
        beat = launch('beat')
        yield SimpleNamespace(r=r, redis=client, worker=worker, control=control, beat=beat, launch=launch, kill=kill, deliver=deliver)
    finally:
        for process in processes:
            kill(process)
        for log in logs:
            log.flush()
            log.seek(0)
            # Captured by pytest; emitted only on failure. Fixture-only logs.
            print(log.read()[-12000:])
            log.close()
        keys = [key async for key in client.scan_iter(match=r.schema + ':*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()
        sender.close()


async def status(r):
    async with r.sessions() as db:
        return await get_pipeline_status(db, r.pipeline)


async def start(r):
    async with r.sessions() as db:
        row, _ = await request_start(db, r.pipeline)
        token = row.command_id
        await db.commit()
        return token


async def state_is(r, expected):
    result = await status(r)
    return result if result['state'] == expected else None


@pytest.mark.asyncio
async def test_real_beat_delivery_and_duplicate_messages(cluster):
    c, r = cluster, cluster.r
    token = await start(r)
    completed = await until(lambda: state_is(r, 'completed'))
    assert completed['completed_chapters'] == 6
    for _ in range(3):
        delivery = await asyncio.to_thread(c.deliver, token)
        assert await asyncio.to_thread(delivery.get, timeout=10) is False
    calls = await c.redis.lrange(r.schema + ':calls', 0, -1)
    assert calls == [chapter['chapter_id'] for chapter in completed['chapters']]


@pytest.mark.asyncio
async def test_real_pause_finishes_current_chapter_and_resumes(cluster):
    c, r = cluster, cluster.r
    chapters = (await status(r))['chapters']
    held = chapters[0]['chapter_id']
    await c.redis.set(r.schema + ':hold', held)
    await start(r)
    await until(lambda: c.redis.get(r.schema + ':entered:' + held))
    async with r.sessions() as db:
        await request_pause(db, r.pipeline)
        await db.commit()
    await c.redis.set(r.schema + ':release', '1')
    paused = await until(lambda: state_is(r, 'paused'))
    assert paused['completed_chapters'] == 1
    await start(r)
    completed = await until(lambda: state_is(r, 'completed'))
    assert completed['completed_chapters'] == 6
    calls = await c.redis.lrange(r.schema + ':calls', 0, -1)
    assert Counter(calls) == Counter(chapter['chapter_id'] for chapter in chapters)


@pytest.mark.asyncio
async def test_sigkill_recovers_checkpoint_and_rejects_old_token(cluster):
    c, r = cluster, cluster.r
    chapters = (await status(r))['chapters']
    held = chapters[1]['chapter_id']
    await c.redis.set(r.schema + ':hold', held)
    token = await start(r)
    await until(lambda: c.redis.get(r.schema + ':entered:' + held))
    assert (await status(r))['completed_chapters'] == 1
    c.kill(c.worker)
    # Advance only the disposable row's heartbeat to exercise the 90s policy
    # promptly; the real process and PostgreSQL session lock are already gone.
    async with r.sessions() as db:
        row = await db.get(PipelineRun, r.pipeline)
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        await db.commit()
    await until(lambda: state_is(r, 'queued'))
    async with r.sessions() as db:
        assert (await db.get(PipelineRun, r.pipeline)).command_id != token
    await c.redis.set(r.schema + ':release', '1')
    old_delivery = await asyncio.to_thread(c.deliver, token)
    c.launch('replacement')
    completed = await until(lambda: state_is(r, 'completed'))
    assert completed['completed_chapters'] == 6
    counts = Counter(await c.redis.lrange(r.schema + ':calls', 0, -1))
    assert await asyncio.to_thread(old_delivery.get, timeout=10) is False
    assert counts[chapters[0]['chapter_id']] == 1
    assert counts[held] == 2
    assert sum(counts.values()) == 7


@pytest.mark.asyncio
async def test_real_broker_connection_failure_keeps_command_for_redelivery(cluster):
    from app.services.production.dispatch import dispatch_pending
    c, r = cluster, cluster.r
    # Stop only fixture-owned schedulers so no successful dispatch races this
    # deliberate connection failure. The real prose worker stays available.
    c.kill(c.beat)
    c.kill(c.control)
    token = await start(r)
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))  # Reserved but not listening: ECONNREFUSED.
        port = reserved.getsockname()[1]
        broken = Celery('offline-test', broker=f'redis://127.0.0.1:{port}/15')
        broken.conf.broker_transport_options = {'socket_connect_timeout': 0.2, 'socket_timeout': 0.2}
        async def unavailable(pipeline_id, command_id):
            await asyncio.to_thread(broken.send_task, 'tasks.run_pipeline_generation',
                                    args=[pipeline_id, command_id], retry=False)
        try:
            assert await asyncio.wait_for(dispatch_pending(r.sessions, unavailable), 10) == 0
        finally:
            broken.close()
    async with r.sessions() as db:
        row = await db.get(PipelineRun, r.pipeline)
        assert row.state == 'queued' and row.command_id == token and row.error_message
        row.dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        await db.commit()
    async def available(pipeline_id, command_id):
        assert command_id == str(token)
        await asyncio.to_thread(c.deliver, command_id)
    assert await dispatch_pending(r.sessions, available) == 1
    assert (await until(lambda: state_is(r, 'completed')))['completed_chapters'] == 6
