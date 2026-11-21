"""Execute worker stop branch; full database/queue integration remains separate."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

SOURCE = Path(__file__).parents[1] / 'app/tasks/generation_tasks.py'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['targeted_revision_unapplied', 'revision_incomplete_response', 'full_revision_failed', 'full_revision_empty', 'revision_context_unavailable'])
async def test_stop_retains_exact_candidate_and_returns_before_downstream_work(reason):
    tree = ast.parse(SOURCE.read_text())
    branches = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                and ast.unparse(n.test) == 'revision_stop_reason is not None']
    assert len(branches) == 1
    branch = branches[0]
    # Verify production placement, not only the isolated branch's behavior.
    parents = [n for n in ast.walk(tree) if isinstance(getattr(n, 'body', None), list) and branch in n.body]
    assert len(parents) == 1
    following = parents[0].body[parents[0].body.index(branch) + 1:]
    assert ast.unparse(following[0]) == 'full_text = current_text'
    assert 'apply_chapter_quality_gate' in ast.unparse(following[1])
    current = '  原候选第一段。\n\n她把钥匙还了回去。\n'
    task = NS(params_json={'existing': 1}, status='running', result_text='', progress_text='失败改写片段')
    chapter = NS(content_text='作者当前稿。', status='draft')
    db = NS(commit=AsyncMock())
    downstream = []
    env = dict(task=task, ch=chapter, db=db, revision_stop_reason=reason, current_text=current, downstream=downstream)
    from app.services.context_errors import RequiredContextUnavailable
    from app.services.generation_errors import task_generation_error
    env.update(revision_context_error=RequiredContextUnavailable('character_cognition_retrieval_unavailable'),
               task_generation_error=task_generation_error)
    code = 'async def exercise():\n' + '\n'.join('    '+line for line in ast.unparse(branch).splitlines())
    code += '\n    downstream.append(True)\n'
    exec(code, env)
    await env['exercise']()
    assert task.status == 'needs_review'
    assert task.result_text == task.progress_text == current and task.char_count == len(current)
    assert task.params_json == {'existing': 1, 'revision_stop_reason': reason}
    assert ('嵌入' if reason == 'revision_context_unavailable' else '请复核') in task.error_message
    assert chapter.content_text == '作者当前稿。' and chapter.status == 'draft'
    assert not downstream
    db.commit.assert_awaited_once()


def test_all_unapplied_revision_paths_set_local_stop_reason():
    tree = ast.parse(SOURCE.read_text())
    values = {n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == 'revision_stop_reason' for t in n.targets)
              and isinstance(n.value, ast.Constant)}
    assert {None, 'targeted_revision_unapplied', 'full_revision_empty'} <= values
    branches = [n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == 'last_regen_err is not None']
    assert any('revision_incomplete_response' in ast.unparse(n) and 'full_revision_failed' in ast.unparse(n) for n in branches)
