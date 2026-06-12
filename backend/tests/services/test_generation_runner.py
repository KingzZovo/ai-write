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

import pytest

from app.models.project import Chapter
from app.services import generation_runner


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
        return SimpleNamespace(
            rag_snippets=["snippet-1"],
            style_samples=["style-1"],
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
