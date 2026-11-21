import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

from sqlalchemy import create_engine, text
from app.services.polish_prompt import POLISH_SYSTEM
from app.services.prompt_registry import BUILTIN_PROMPTS
from app.services.polish_candidate import POLISH_SYSTEM as CANDIDATE_SYSTEM


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/a1001935_polish_default_policy.py'
    spec = importlib.util.spec_from_file_location('polish_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_defaults_share_same_editorial_guidance():
    default = next(p for p in BUILTIN_PROMPTS if p['task_type'] == 'polishing')
    assert default['system_prompt'] == CANDIDATE_SYSTEM == POLISH_SYSTEM == migration().NEW_PROMPT
    assert '视觉之外至少两种感官' not in default['system_prompt']


def test_migration_changes_only_exact_old_default_and_preserves_routing(monkeypatch):
    m = migration()
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE prompt_assets (id INTEGER, task_type TEXT, system_prompt TEXT, endpoint_id TEXT, max_tokens INTEGER)'))
        rows = [(1, 'polishing', m.OLD_PROMPT), (2, 'polishing', m.OLD_PROMPT+'作者自定义'), (3, 'generation', m.OLD_PROMPT)]
        for ident, task, prompt in rows:
            db.execute(text('INSERT INTO prompt_assets VALUES (:id,:task,:prompt,:endpoint,4096)'), dict(id=ident,task=task,prompt=prompt,endpoint='configured-route'))
        monkeypatch.setattr(m, 'op', NS(execute=db.execute))
        m.upgrade()
        m.upgrade()
        stored = db.execute(text('SELECT id,system_prompt,endpoint_id,max_tokens FROM prompt_assets ORDER BY id')).all()
        assert stored[0][1] == m.NEW_PROMPT
        assert stored[1][1] == rows[1][2] and stored[2][1] == m.OLD_PROMPT
        assert all(row[2:] == ('configured-route',4096) for row in stored)
        m.downgrade()
        assert db.scalar(text('SELECT system_prompt FROM prompt_assets WHERE id=1')) == m.NEW_PROMPT
    engine.dispose()
