import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
pytestmark = pytest.mark.external_services
from sqlalchemy import select
from app.db.session import async_session_factory
from app.models.project import Chapter, ChapterVersion
from app.services.chapter_evaluator import ChapterEvaluator, EvaluationResult
from tests.support.chapter_fixture import create_chapter_target


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [('cognition_unavailable', 'needs_review'), ('failed', 'needs_review'), ('call_failed', 'needs_review'), ('timeout', 'needs_review'), ('empty_input', 'needs_review'), ('severe', 'needs_review'), ('passed', 'completed')])
async def test_sse_settles_acceptance_without_losing_draft(auth_client, monkeypatch, kind, expected):
    import app.api.generate as generate
    from app.services.scene_orchestrator import SceneOrchestrator
    project = (await auth_client.post('/api/projects', json={'title': '验收状态测试', 'genre': '测试'})).json()['id']
    volume = await create_chapter_target(project)
    async with async_session_factory() as db:
        chapter = await db.scalar(select(Chapter).where(Chapter.volume_id == uuid.UUID(volume)))
        chapter_id = str(chapter.id)
    text = '守卫拿走钱，没有接下信。'
    async def stream(self, **kwargs):
        yield text
    monkeypatch.setattr(SceneOrchestrator, 'orchestrate_chapter_stream', stream)
    monkeypatch.setattr(generate, 'build_outline_readiness_report', AsyncMock(return_value=SimpleNamespace(ready=True)))
    result = EvaluationResult(overall=9)
    if kind == 'failed':
        result = EvaluationResult(parse_failed=True)
    elif kind in ('call_failed', 'timeout', 'empty_input'):
        result = EvaluationResult(parse_failed=True, failure_reason=kind)
    elif kind == 'severe':
        result.issues = [{'violation_type': 'chapter_completion_violation', 'severity': 'high', 'description': '信未交付'}]
    evaluate = AsyncMock(return_value=result)
    monkeypatch.setattr(ChapterEvaluator, 'evaluate', evaluate)
    if kind == 'cognition_unavailable':
        monkeypatch.setattr('app.services.character_cognition.load_prompt_context', AsyncMock(side_effect=RuntimeError('private diagnostic')))
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_for_chapter', AsyncMock())
    monkeypatch.setattr('app.services.chapter_summarizer.summarize_and_save_chapter', AsyncMock(return_value=(True, '摘要')))
    monkeypatch.setattr('app.services.memory.backfill_prev_volume_summary', AsyncMock())
    cognition = AsyncMock()
    monkeypatch.setattr('app.services.character_cognition.extract_and_update', cognition)
    try:
        response = await auth_client.post('/api/generate/chapter', json={
            'project_id': project, 'chapter_id': chapter_id, 'use_scene_mode': True,
            'auto_revise': True, 'skip_polish': True, 'max_revise_rounds': 1,
        })
        assert response.status_code == 200
        if kind == 'cognition_unavailable':
            assert 'revision_context_unavailable' in response.text and 'private diagnostic' not in response.text
        else:
            assert '"error"' not in response.text, response.text
        assert 'chapter_acceptance' in response.text
        if result.parse_failed:
            import json
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith('data: {')]
            scored = next(e for e in events if e.get('event') == 'scored')
            skipped = next(e for e in events if e.get('event') == 'revise_skipped')
            reason = result.failure_reason or 'parse_failed'
            assert scored['evaluation_failure'] == reason
            assert skipped['reason'] == 'evaluation_' + reason
        if kind == 'cognition_unavailable':
            evaluate.assert_not_awaited()
        else:
            evaluate.assert_awaited_once()
        async with async_session_factory() as db:
            chapter = await db.get(Chapter, uuid.UUID(chapter_id))
            assert chapter.content_text == text
            assert chapter.status == expected
            versions = (await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id))).all()
            assert versions
            assert sum(v.is_active for v in versions) == (1 if expected == 'completed' else 0)
        if expected == 'needs_review':
            cognition.assert_not_awaited()
    finally:
        await auth_client.delete(f'/api/projects/{project}')
