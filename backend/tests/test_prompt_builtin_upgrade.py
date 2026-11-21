"""Built-in prompt upgrade: an untouched default gets a new version, an
operator edit is preserved and flagged, and nothing is written to PostgreSQL.

The upgrade policy is exercised against a recording session stand-in, because
``seed_builtins``/``upgrade_builtins`` only needs ``execute``/``add``/``flush``.
"""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.prompt import PromptAsset
from app.services import prompt_registry
from app.services.prompt_registry import (
    BUILTIN_DEFAULT_HISTORY,
    BUILTIN_DEFAULTS_REVISION,
    BUILTIN_PROMPTS,
    PromptRegistry,
    builtin_default_fingerprint,
    builtin_prompt,
    default_content_fingerprint,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    """Records ORM writes so the policy is checkable without a database."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.added = []
        self.flushes = 0
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)

    def add(self, asset):
        self.added.append(asset)

    async def flush(self):
        self.flushes += 1


def _row(task_type, system_prompt, *, version=1, active=1, **route):
    route_fields = dict(endpoint_id=None, model_name='', temperature=0.7, max_tokens=8192, model_tier=None)
    route_fields.update(route)
    default = builtin_prompt(task_type)
    return PromptAsset(task_type=task_type, name=default['name'], mode=default.get('mode', 'text'),
                       system_prompt=system_prompt, version=version, is_active=active, **route_fields)


def _migration(filename, name):
    path = Path(__file__).parents[1] / 'alembic/versions' / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_untouched_previous_default_gets_a_new_active_version(monkeypatch):
    old_default = '旧默认场景规划:只输出 JSON 数组。'
    monkeypatch.setattr(prompt_registry, 'BUILTIN_DEFAULT_HISTORY',
                        {'scene_planner': (default_content_fingerprint('structured', old_default),)})
    endpoint = uuid4()
    # Trailing blank lines are normalisation noise, not an operator edit.
    row = _row('scene_planner', old_default + '\n\n', endpoint_id=endpoint, model_name='planner-model',
               temperature=0.2, max_tokens=3000, model_tier='standard')
    db = _FakeDB([row])

    report = await PromptRegistry(db).upgrade_builtins()

    assert report['revision'] == BUILTIN_DEFAULTS_REVISION
    assert report['upgraded'] == [{'task_type': 'scene_planner', 'from_version': 1, 'to_version': 2}]
    assert 'scene_planner' not in report['current'] and report['needs_review'] == []
    assert row.is_active == 0 and row.system_prompt == old_default + '\n\n'
    assert len(db.added) == 1
    upgraded = db.added[0]
    assert upgraded.task_type == 'scene_planner'
    assert (upgraded.version, upgraded.is_active) == (2, 1)
    assert upgraded.system_prompt == builtin_prompt('scene_planner')['system_prompt']
    assert upgraded.mode == 'structured'
    # The operator's routing survives the upgrade.
    assert (upgraded.endpoint_id, upgraded.model_name, upgraded.temperature,
            upgraded.max_tokens, upgraded.model_tier) == (endpoint, 'planner-model', 0.2, 3000, 'standard')
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_upgrade_then_upgrade_again_is_a_no_op(monkeypatch):
    old_default = '旧默认场景规划:只输出 JSON 数组。'
    monkeypatch.setattr(prompt_registry, 'BUILTIN_DEFAULT_HISTORY',
                        {'scene_planner': (default_content_fingerprint('structured', old_default),)})
    row = _row('scene_planner', old_default)
    db = _FakeDB([row])
    registry = PromptRegistry(db)
    await registry.upgrade_builtins()
    db.rows = db.rows + db.added

    second = await registry.upgrade_builtins()

    assert second['upgraded'] == [] and len(db.added) == 1
    assert second['current'] == ['scene_planner']


@pytest.mark.asyncio
async def test_operator_edited_row_is_kept_and_marked_for_review(monkeypatch):
    monkeypatch.setattr(prompt_registry, 'BUILTIN_DEFAULT_HISTORY',
                        {'scene_planner': (default_content_fingerprint('structured', '旧默认'),)})
    row = _row('scene_planner', '作者自己写的场景规划要求', version=3)
    db = _FakeDB([row])

    report = await PromptRegistry(db).upgrade_builtins()

    assert report['needs_review'] == [{'task_type': 'scene_planner', 'version': 3,
                                       'reason': 'operator_modified'}]
    assert report['upgraded'] == [] and db.added == []
    assert (row.system_prompt, row.version, row.is_active) == ('作者自己写的场景规划要求', 3, 1)


@pytest.mark.asyncio
async def test_row_already_on_the_shipped_default_is_left_alone(monkeypatch):
    monkeypatch.setattr(prompt_registry, 'BUILTIN_DEFAULT_HISTORY', {})
    default = builtin_prompt('scene_planner')
    row = _row('scene_planner', default['system_prompt'])
    db = _FakeDB([row])

    report = await PromptRegistry(db).upgrade_builtins()

    assert report['current'] == ['scene_planner']
    assert report['upgraded'] == [] and report['needs_review'] == [] and db.added == []
    assert row.is_active == 1


@pytest.mark.asyncio
async def test_every_active_version_is_retired_when_the_new_one_lands(monkeypatch):
    old_default = '旧默认场景规划:只输出 JSON 数组。'
    monkeypatch.setattr(prompt_registry, 'BUILTIN_DEFAULT_HISTORY',
                        {'scene_planner': (default_content_fingerprint('structured', old_default),)})
    first = _row('scene_planner', '更早的旧版本', version=1, active=0)
    active = _row('scene_planner', old_default, version=2, active=1)
    db = _FakeDB([first, active])

    report = await PromptRegistry(db).upgrade_builtins()

    assert report['upgraded'] == [{'task_type': 'scene_planner', 'from_version': 2, 'to_version': 3}]
    assert active.is_active == 0 and first.is_active == 0
    assert [(asset.version, asset.is_active) for asset in db.added] == [(3, 1)]


@pytest.mark.asyncio
async def test_task_types_without_any_row_are_left_to_seed_builtins():
    db = _FakeDB([])

    report = await PromptRegistry(db).upgrade_builtins()

    assert set(report['absent']) == {p['task_type'] for p in BUILTIN_PROMPTS}
    assert report['upgraded'] == [] and report['current'] == [] and report['needs_review'] == []
    assert db.added == []


def test_history_table_is_well_formed_and_never_repeats_the_current_default():
    assert BUILTIN_DEFAULTS_REVISION >= 2
    for task_type, fingerprints in BUILTIN_DEFAULT_HISTORY.items():
        assert builtin_prompt(task_type) is not None, task_type
        for fingerprint in fingerprints:
            assert len(fingerprint) == 64 and set(fingerprint) <= set('0123456789abcdef')
        assert builtin_default_fingerprint(task_type) not in fingerprints


def test_recorded_previous_defaults_match_the_shipped_migrations():
    cognition = _migration('a1001948_cognition_author_revisions.py', 'builtin_history_1948')
    polish = _migration('a1001935_polish_default_policy.py', 'builtin_history_1935')
    assert default_content_fingerprint('structured', cognition.OLD_PROMPT) in BUILTIN_DEFAULT_HISTORY['chapter_cognition']
    assert default_content_fingerprint('structured', cognition.NEW_PROMPT) == builtin_default_fingerprint('chapter_cognition')
    assert default_content_fingerprint('text', polish.OLD_PROMPT) in BUILTIN_DEFAULT_HISTORY['polishing']
    assert default_content_fingerprint('text', polish.NEW_PROMPT) == builtin_default_fingerprint('polishing')

@pytest.mark.asyncio
async def test_real_stale_chapter_cognition_row_is_upgraded_end_to_end():
    """a1001948 was never applied to a real database, so its OLD_PROMPT is
    exactly what an existing row still holds; the real history entry (not a
    monkeypatched one) must recognise and version-upgrade it."""
    cognition = _migration('a1001948_cognition_author_revisions.py', 'builtin_history_1948_stale')
    row = _row('chapter_cognition', cognition.OLD_PROMPT)
    db = _FakeDB([row])

    report = await PromptRegistry(db).upgrade_builtins()

    assert report['upgraded'] == [{'task_type': 'chapter_cognition', 'from_version': 1, 'to_version': 2}]
    assert row.is_active == 0 and row.system_prompt == cognition.OLD_PROMPT
    assert len(db.added) == 1
    assert db.added[0].system_prompt == builtin_prompt('chapter_cognition')['system_prompt']
    assert db.added[0].output_schema == builtin_prompt('chapter_cognition')['output_schema']


def test_every_recorded_default_is_older_than_the_shipped_one():
    shipped = prompt_registry.builtin_default_fingerprints()
    assert set(BUILTIN_DEFAULT_HISTORY) <= set(shipped)
    for task_type, fingerprints in BUILTIN_DEFAULT_HISTORY.items():
        assert fingerprints, task_type
        assert shipped[task_type] not in fingerprints
