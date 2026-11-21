"""Task A3 — generation_runner must call ContextPackBuilder.build() with the
current ``(project_id, volume_id, chapter_idx)`` signature.

History: v0.8 wired the runner against the old ``build(project_id, chapter_id)``
signature. The builder later evolved to ``build(project_id, volume_id,
chapter_idx)`` but the runner call site never followed, so every planning
phase died with TypeError. These tests pin the contract: the runner resolves
``chapter_id -> (volume_id, chapter_idx)`` via the Chapter row and passes the
new kwargs.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.project import Chapter
from app.services import generation_runner
from app.services.context_pack import ContextPack


class _RecordingBuilder:
    """Stub exposing the REAL ContextPackBuilder.build signature.

    A call still using the removed ``chapter_id=`` kwarg raises TypeError
    here exactly as the real builder would — that is the red condition.
    """

    last_call: dict | None = None

    def __init__(self, db=None):
        self._db = db

    async def build(self, project_id, volume_id, chapter_idx, db=None):
        type(self).last_call = {
            "project_id": project_id,
            "volume_id": volume_id,
            "chapter_idx": chapter_idx,
        }
        return ContextPack(
            rag_snippets=["snippet-1"],
            style_samples=["style-1"],
            cognition_boundaries='沈青当时怀疑陈川在推托，车况尚未证实。',
        )


class _FakeDB:
    """Just enough AsyncSession surface for _phase_planning."""

    def __init__(self, chapter=None):
        self._chapter = chapter
        self.get_calls: list[tuple] = []

    async def get(self, model, pk):
        self.get_calls.append((model, pk))
        if model is Chapter:
            return self._chapter
        return None

    async def commit(self):
        pass


async def _fake_rules(db, project_id, **kwargs):
    return ["规则A"]


def _make_run(chapter_id):
    return SimpleNamespace(
        project_id=uuid.uuid4(),
        chapter_id=chapter_id,
    )


@pytest.mark.asyncio
async def test_phase_planning_passes_volume_id_and_chapter_idx(monkeypatch):
    """Runner must resolve the Chapter row and call build() with the new kwargs."""
    _RecordingBuilder.last_call = None
    monkeypatch.setattr(generation_runner, "ContextPackBuilder", _RecordingBuilder)
    monkeypatch.setattr(generation_runner, "fetch_writing_rules", _fake_rules)

    volume_id = uuid.uuid4()
    chapter = SimpleNamespace(id=uuid.uuid4(), volume_id=volume_id, chapter_idx=7)
    db = _FakeDB(chapter=chapter)
    run = _make_run(chapter_id=chapter.id)

    data = await generation_runner._phase_planning(run, db)

    call = _RecordingBuilder.last_call
    assert call is not None, "ContextPackBuilder.build was never reached"
    assert call["project_id"] == str(run.project_id)
    assert call["volume_id"] == str(volume_id)
    assert call["chapter_idx"] == 7
    # Chapter lookup must go through the provided session.
    assert (Chapter, chapter.id) in [(m, p) for m, p in db.get_calls]
    # Snapshot shape consumed by _phase_drafting stays intact.
    pack = data["pack"]
    assert pack["rag_snippets"] == ["snippet-1"]
    assert pack["style_samples"] == ["style-1"]
    assert pack["writing_rules"] == ["规则A"]
    assert isinstance(pack['system_prompt'], str) and pack['system_prompt']
    assert '车况尚未证实' in pack['system_prompt']


@pytest.mark.asyncio
async def test_phase_planning_chapter_not_found_raises(monkeypatch):
    """Missing Chapter row fails the run loudly (execute_run marks it FAILED)."""
    monkeypatch.setattr(generation_runner, "ContextPackBuilder", _RecordingBuilder)
    monkeypatch.setattr(generation_runner, "fetch_writing_rules", _fake_rules)

    db = _FakeDB(chapter=None)
    run = _make_run(chapter_id=uuid.uuid4())

    with pytest.raises(ValueError, match="[Cc]hapter"):
        await generation_runner._phase_planning(run, db)


@pytest.mark.asyncio
async def test_phase_planning_requires_chapter_id(monkeypatch):
    """chapter_id=None cannot satisfy build(volume_id, chapter_idx) — fail fast."""
    monkeypatch.setattr(generation_runner, "ContextPackBuilder", _RecordingBuilder)
    monkeypatch.setattr(generation_runner, "fetch_writing_rules", _fake_rules)

    db = _FakeDB(chapter=None)
    run = _make_run(chapter_id=None)

    with pytest.raises(ValueError, match="chapter_id"):
        await generation_runner._phase_planning(run, db)


def test_context_pack_builder_signature_pinned():
    """If build()'s signature changes, the runner stub in this file must be updated too."""
    import inspect

    from app.services.context_pack import ContextPackBuilder

    assert list(inspect.signature(ContextPackBuilder.build).parameters) == [
        "self", "project_id", "volume_id", "chapter_idx", "db",
    ]


@pytest.mark.asyncio
async def test_drafting_critic_and_rewrite_receive_the_prepared_cognition(monkeypatch):
    from app.models.generation_run import PHASE_PLANNING
    content = '沈青当时怀疑陈川在推托，车况尚未证实。'
    data = {'pack': {'system_prompt': content, 'rag_snippets': ['诊断快照'], 'writing_rules': ['重复规则']}}
    run = _make_run(uuid.uuid4())
    run.id = uuid.uuid4(); run.checkpoint_data = {PHASE_PLANNING: data}
    db = SimpleNamespace(commit=AsyncMock(), add=lambda row: None)
    text_call = AsyncMock(return_value=SimpleNamespace(text='正文。'))
    critic_call = AsyncMock(return_value={'issues': []})
    monkeypatch.setattr(generation_runner, 'run_text_prompt', text_call)
    monkeypatch.setattr(generation_runner, 'run_critic', critic_call)
    refresh = AsyncMock(return_value={'pack': {'system_prompt': '本轮重新核对：' + content}})
    monkeypatch.setattr(generation_runner, '_phase_planning', refresh)
    monkeypatch.setenv('AGENT_TOOL_LOOP_ENABLED', 'false')
    await generation_runner._phase_drafting(run, db, data)
    await generation_runner._phase_critic(run, db, '正文。', 1)
    await generation_runner._phase_rewrite(run, db, '正文。', {})
    assert text_call.await_args_list[0].kwargs['extra_system'] == content
    assert text_call.await_args_list[1].kwargs['extra_system'] == '本轮重新核对：' + content
    assert '诊断快照' not in text_call.await_args_list[0].args[1]
    assert '重复规则' not in text_call.await_args_list[0].args[1]
    assert critic_call.await_args.kwargs['pack_summary'] == '本轮重新核对：' + content
    assert [call.kwargs['candidate_text'] for call in refresh.await_args_list] == ['正文。', '正文。']


@pytest.mark.asyncio
async def test_resumed_run_refreshes_the_stored_context_before_critic(monkeypatch):
    from app.models.generation_run import PHASE_PLANNING, PHASE_DRAFTING, PHASE_CRITIC, STATUS_RUNNING
    run = _make_run(uuid.uuid4())
    run.id = uuid.uuid4(); run.phase = PHASE_CRITIC; run.status = STATUS_RUNNING
    run.rewrite_count = 0; run.max_rewrite_count = 2
    run.checkpoint_data = {PHASE_PLANNING: {'pack': {'system_prompt': '过期的认知'}}, PHASE_DRAFTING: {'text': '旧草稿'}}
    from tests.support.generation_run_fixture import verified_history
    verified_history(run)
    fresh = {'pack': {'system_prompt': '本次重新核对的认知'}}
    planning = AsyncMock(return_value=fresh)
    monkeypatch.setattr(generation_runner, '_phase_planning', planning)
    async def critic(**kwargs):
        assert kwargs['pack_summary'] == fresh['pack']['system_prompt']
        assert kwargs['draft'] == '旧草稿'
        return {'issues': [], 'hard_count': 0}
    monkeypatch.setattr(generation_runner, 'run_critic', critic)
    monkeypatch.setattr(generation_runner.run_bus, 'publish', AsyncMock())
    db = SimpleNamespace(get=AsyncMock(return_value=run), commit=AsyncMock(), add=lambda row: None)
    result = await generation_runner._execute_run_locked(run_id=str(run.id), db=db, resume=True)
    planning.assert_awaited_once_with(run, db, candidate_text='旧草稿')
    assert result['final_text'] == '旧草稿'


@pytest.mark.asyncio
async def test_candidate_preparation_uses_complete_current_draft_for_recall(monkeypatch):
    monkeypatch.setattr(generation_runner, 'ContextPackBuilder', _RecordingBuilder)
    monkeypatch.setattr(generation_runner, 'fetch_writing_rules', _fake_rules)
    chapter = SimpleNamespace(id=uuid.uuid4(), volume_id=uuid.uuid4(), chapter_idx=7)
    db = _FakeDB(chapter=chapter)
    db.commit = AsyncMock()
    candidate = '沈青仍不知道通知早已寄到。' * 600 + '末尾：陈川把回执递给了她。'
    received = []

    async def resolve(pack, *, db, user_instruction):
        db.commit.assert_awaited_once()
        received.append(user_instruction)
        assert pack.current_content == candidate

    monkeypatch.setattr(ContextPack, 'resolve_cognition', resolve)
    await generation_runner._phase_planning(_make_run(chapter.id), db, candidate_text=candidate)
    assert received == [candidate]


@pytest.mark.asyncio
async def test_each_candidate_review_and_rewrite_refreshes_decisions_and_stops_on_failure(monkeypatch):
    from app.services.context_errors import RequiredContextUnavailable
    run = _make_run(uuid.uuid4())
    run.id = uuid.uuid4()
    run.checkpoint_data = {'planning': {'pack': {'system_prompt': '过期：沈青不知通知已到。'}}}
    db = SimpleNamespace(commit=AsyncMock(), add=lambda row: None)
    refresh = AsyncMock(side_effect=[
        {'pack': {'system_prompt': '首轮：她只怀疑车坏是借口。'}},
        {'pack': {'system_prompt': '次轮作者校订：车况未证实。'}},
        RequiredContextUnavailable('character_cognition_decision_changed'),
    ])
    critic = AsyncMock(return_value={'issues': []})
    rewrite = AsyncMock(return_value=SimpleNamespace(text='新版正文'))
    monkeypatch.setattr(generation_runner, '_phase_planning', refresh)
    monkeypatch.setattr(generation_runner, 'run_critic', critic)
    monkeypatch.setattr(generation_runner, 'run_text_prompt', rewrite)

    await generation_runner._phase_critic(run, db, '第一轮完整候选', 1)
    await generation_runner._phase_critic(run, db, '第二轮完整候选', 2)
    with pytest.raises(RequiredContextUnavailable):
        await generation_runner._phase_rewrite(run, db, '第二轮完整候选', {})

    assert [call.kwargs['candidate_text'] for call in refresh.await_args_list] == [
        '第一轮完整候选', '第二轮完整候选', '第二轮完整候选',
    ]
    assert [call.kwargs['pack_summary'] for call in critic.await_args_list] == [
        '首轮：她只怀疑车坏是借口。', '次轮作者校订：车况未证实。',
    ]
    rewrite.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_at', ['first_critic', 'variant_preparation', 'variant_critic'])
async def test_variant_context_failure_preserves_delivered_draft_and_stops(monkeypatch, failure_at):
    from app.models.generation_run import PHASE_PLANNING, PHASE_DRAFTING, STATUS_RUNNING, STATUS_FAILED
    from app.services.context_errors import RequiredContextUnavailable
    run = _make_run(uuid.uuid4())
    run.id = uuid.uuid4()
    run.phase = PHASE_PLANNING
    run.status = STATUS_RUNNING
    run.rewrite_count = 0
    run.max_rewrite_count = 2
    run.checkpoint_data = {}
    prepared = {'pack': {'system_prompt': '当前核实的前文'}}
    failure = RequiredContextUnavailable('character_cognition_decision_changed')
    planning = AsyncMock(side_effect=[prepared, failure] if failure_at == 'variant_preparation' else None)
    planning.return_value = prepared
    drafting = AsyncMock(side_effect=[{'text': '完整首稿，保留逐字原文。'}, {'text': '另一候选。'}])
    critic = AsyncMock(side_effect=[failure] if failure_at == 'first_critic' else [
        {'issues': [], 'hard_count': 0}, failure,
    ])
    monkeypatch.setattr(generation_runner, '_phase_planning', planning)
    monkeypatch.setattr(generation_runner, '_phase_drafting', drafting)
    monkeypatch.setattr(generation_runner, '_phase_critic', critic)
    monkeypatch.setattr(generation_runner.run_bus, 'publish', AsyncMock())
    monkeypatch.setenv('BVSR_ENABLED', 'true')
    monkeypatch.setenv('BVSR_N', '3')
    from tests.support.generation_run_fixture import verified_history
    verified_history(run, bvsr_enabled=True, variant_count=3)
    db = SimpleNamespace(get=AsyncMock(return_value=run), commit=AsyncMock(), rollback=AsyncMock(), add=lambda row: None)

    result = await generation_runner._execute_run_locked(run_id=str(run.id), db=db)

    assert result['status'] == STATUS_FAILED
    assert run.checkpoint_data[PHASE_DRAFTING]['text'] == '完整首稿，保留逐字原文。'
    assert drafting.await_count == (2 if failure_at == 'variant_critic' else 1)
    assert critic.await_count == (2 if failure_at == 'variant_critic' else 1)
