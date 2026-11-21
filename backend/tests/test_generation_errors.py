import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.services.generation_errors import generation_error_payload
from app.services.production.control import PipelineConflict
from app.services.production.fencing import StaleChapterWrite


@pytest.mark.parametrize('operation', ['chapter', 'outline', 'revision'])
@pytest.mark.parametrize('error,reason', [
    (RuntimeError('private endpoint key and full prompt'), 'failed'),
    (TimeoutError('private endpoint'), 'timeout'),
    (PipelineConflict('private details'), 'busy'),
    (StaleChapterWrite('private details'), 'source_changed'),
    (ValueError('provider_incomplete_response'), 'incomplete_response'),
    (ValueError('provider_empty_response'), 'empty_response'),
])
def test_generation_errors_are_bounded_without_claiming_persistence(operation, error, reason):
    result = generation_error_payload(error, operation)
    assert result['code'] == f'{operation}_{reason}'
    assert 'private' not in str(result)
    assert '保存状态以保存确认或刷新核查结果为准' in result['error']
    assert '原文未被修改' not in result['error']
    assert len(result['error']) < 140


@pytest.mark.asyncio
async def test_actual_terminal_and_revision_handlers_emit_public_errors():
    tree = ast.parse((Path(__file__).parents[1] / 'app/api/generate.py').read_text())
    # Execute the real exception-handler bodies without database/model setup.
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)
                and any(isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield)
                        and 'generation_error_payload(' in ast.unparse(stmt) for stmt in n.body)]
    assert len(handlers) == 4  # chapter, guarded chapter, outer revision, outline
    codes = []
    for handler in handlers:
        env = dict(json=json, logger=MagicMock(), generation_error_payload=generation_error_payload)
        env[handler.name] = RuntimeError('private endpoint and key')
        source = 'async def exercise():\n'
        source += '\n'.join('    ' + line for stmt in handler.body for line in ast.unparse(stmt).splitlines())
        exec(source, env)
        frames = [frame async for frame in env['exercise']()]
        assert all('private' not in frame for frame in frames)
        events = [json.loads(frame.removeprefix('data: ')) for frame in frames if '[DONE]' not in frame]
        assert len(events) == 1
        codes.append(events[0]['code'])
        if handler.name == 'revise_err':
            assert events[0]['event'] == 'revise_error'
        assert not any(e.get('status') in ('saved', 'completed') for e in events)
    assert sorted(codes) == ['chapter_failed', 'chapter_failed', 'outline_failed', 'revision_failed']


def test_inner_scene_revision_error_keeps_metadata_without_provider_details():
    tree = ast.parse((Path(__file__).parents[1] / 'app/api/generate.py').read_text())
    payload = next(n for n in ast.walk(tree) if isinstance(n, ast.Dict)
                   and 'generation_error_payload(revise_scene_err' in ast.unparse(n))
    env = dict(generation_error_payload=generation_error_payload, round_idx=2,
               revise_scene_err=RuntimeError('private endpoint key'))
    value = eval(compile(ast.Expression(payload), '<actual revision event>', 'eval'), env)
    assert value['event'] == 'revise_error' and value['round'] == 2
    assert value['reason'] == 'scene_orchestrator_failed'
    assert value['code'] == 'revision_failed'
    assert 'private' not in json.dumps(value)
