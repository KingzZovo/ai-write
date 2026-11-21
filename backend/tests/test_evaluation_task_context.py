from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.models.project import Chapter, EvaluateTask, Volume
from app.services.chapter_evaluator import EvaluationResult
from app.services.evaluation_neighbors import EvaluationNeighbors
from app.tasks.evaluation_tasks import _run_evaluate_task_async


@pytest.fixture
def context_env(monkeypatch):
    row = NS(chapter_id='c', status='pending', result_json=None, error_text=None)
    chapter = NS(id='c', volume_id='v', content_text='当前章节正文。', outline_json={'summary': '交还钥匙'})
    volume = NS(project_id='p')
    values = {EvaluateTask: row, Chapter: chapter, Volume: volume}
    db = NS(get=AsyncMock(side_effect=lambda model, key: values[model]), commit=AsyncMock(), rollback=AsyncMock())
    opened = []
    @asynccontextmanager
    async def sessions():
        opened.append(True)
        try: yield db
        finally: opened.pop()
    lease = NS(session=sessions, check=AsyncMock())
    @asynccontextmanager
    async def lock(task_id): yield lease
    monkeypatch.setattr('app.services.evaluation_lock.evaluation_worker_lock', lock)

    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(return_value={}))
    monkeypatch.setattr('app.services.character_cognition.serialize_for_prompt', lambda value, **kwargs: '角色尚不知密码')
    monkeypatch.setattr('app.services.style_stat.load_style_stats_text', AsyncMock(return_value='统计参考'))
    neighbors = AsyncMock(return_value=EvaluationNeighbors('前章事实', '后章尚未发生', '前章尾部'))
    style = AsyncMock(return_value='作者绑定风格')
    monkeypatch.setattr('app.services.evaluation_neighbors.load_evaluation_neighbors', neighbors)
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', style)
    async def evaluate(**kwargs):
        assert not opened, 'No session may remain open during model evaluation'
        return EvaluationResult(overall=8)
    evaluate_call = AsyncMock(side_effect=evaluate)
    monkeypatch.setattr('app.services.chapter_evaluator.ChapterEvaluator.evaluate', evaluate_call)
    return NS(row=row, chapter=chapter, lease=lease, db=db, neighbors=neighbors, style=style, evaluate=evaluate_call,
              opened=opened)


@pytest.mark.asyncio
async def test_background_evaluation_uses_shared_neighbors_and_style_outside_session(context_env):
    env = context_env
    result = await _run_evaluate_task_async('t', 'c', 'test')
    assert result['status'] == 'ok' and env.row.status == 'completed'
    env.neighbors.assert_awaited_once_with(env.db, 'c', 'p')
    env.style.assert_awaited_once_with(env.db, 'p', 'c')
    values = env.evaluate.call_args.kwargs
    assert values['previous_summary'] == '前章事实'
    assert values['following_context'] == '后章尚未发生'
    assert values['style_profile'] == '作者绑定风格'
    assert values['cognition_ledger_text'] == '角色尚不知密码'
    assert values['chapter_text'] == '当前章节正文。'
    assert env.row.result_json['overall'] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['neighbors', 'style', 'cognition'])
async def test_context_failure_does_not_score_and_records_sanitized_failure(context_env, monkeypatch, failure):
    env = context_env
    if failure == 'cognition':
        monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(side_effect=RuntimeError('private diagnostic must not be stored')))
    else:
        getattr(env, failure).side_effect = RuntimeError('private diagnostic must not be stored')
    result = await _run_evaluate_task_async('t', 'c', 'test')
    assert result['reason'] == 'evaluation_context_unavailable'
    assert env.row.status == 'failed' and env.row.error_text == 'evaluation_context_unavailable'
    assert env.row.result_json is None
    env.db.rollback.assert_awaited_once()
    env.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_chapter_parameter_cannot_write_evaluation_for_bound_task(context_env):
    env = context_env
    result = await _run_evaluate_task_async('t', 'other', 'test')
    assert result['reason'] == 'chapter_mismatch'
    assert env.row.status == 'pending' and env.row.result_json is None
    env.db.get.assert_awaited_once_with(EvaluateTask, 't')
    env.evaluate.assert_not_awaited()
    env.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_failure_does_not_replace_a_concurrently_completed_result(context_env):
    env = context_env
    env.neighbors.side_effect = RuntimeError('read failed')
    async def rollback():
        env.row.status = 'completed'
        env.row.result_json = {'overall': 9}
    env.db.rollback.side_effect = rollback
    result = await _run_evaluate_task_async('t', 'c', 'test')
    assert result['reason'] == 'already_completed'
    assert env.row.result_json == {'overall': 9} and env.row.status == 'completed'
    env.evaluate.assert_not_awaited()
    env.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_keeps_original_source_when_author_edits_during_model_call(context_env):
    from app.services.evaluation_source import chapter_evaluation_source, evaluation_source_status
    env = context_env
    original = chapter_evaluation_source('c', env.chapter.content_text, env.chapter.outline_json)
    async def evaluate(**kwargs):
        env.chapter.content_text = '作者新稿。'
        return EvaluationResult(overall=9)
    env.evaluate.side_effect = evaluate
    await _run_evaluate_task_async('t', 'c', 'test')
    assert env.row.result_json['chapter_source'] == original
    assert evaluation_source_status(env.row.result_json, env.chapter) == 'changed'
    assert env.chapter.content_text == '作者新稿。'


@pytest.mark.asyncio
async def test_lost_lock_after_model_prevents_result_write(context_env):
    env = context_env
    env.lease.check.side_effect = [None, None, None, RuntimeError('lock lost')]
    with pytest.raises(RuntimeError, match='lock lost'):
        await _run_evaluate_task_async('t', 'c', 'test')
    env.evaluate.assert_awaited_once()
    assert env.row.result_json is None
    assert env.db.commit.await_count == 1  # Only the pre-model running state.


@pytest.mark.asyncio
async def test_background_retrieval_failure_is_context_failure_and_never_scores(context_env, monkeypatch):
    from app.services.context_errors import RequiredContextUnavailable
    from tests.test_cognition_retrieval import prompt
    env = context_env
    request = prompt()
    monkeypatch.setattr('app.services.character_cognition.load_prompt_context', AsyncMock(return_value=request))
    async def resolve(value):
        assert value is request and not env.opened
        raise RequiredContextUnavailable('character_cognition_retrieval_unavailable')
    monkeypatch.setattr('app.services.cognition_retrieval.resolve_prompt_context', resolve)
    with pytest.raises(RequiredContextUnavailable): await _run_evaluate_task_async('t', 'c', 'test')
    assert env.row.status == 'failed' and env.row.error_text == 'evaluation_context_unavailable'
    assert env.row.result_json is None
    env.evaluate.assert_not_awaited()
