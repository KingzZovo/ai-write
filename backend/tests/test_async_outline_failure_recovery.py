"""An interrupted outline keeps recovery text without reporting success."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tests.test_pipeline_control import runtime_db
from app.models.generation_task import GenerationTask
from app.models.project import Outline
from app.services.outline_generator import OutlineGenerator
from app.tasks.generation_tasks import _run_async_generation_impl


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['stream', 'save', None])
async def test_outline_failure_retains_text_and_existing_outline(runtime_db, monkeypatch, failure):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: Outline.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        original = Outline(project_id=r.project, level='book', content_json={'raw_text': '作者原大纲。'})
        task = GenerationTask(project_id=r.project, task_type='outline_book', params_json={'user_input': '写大纲'})
        db.add_all([original, task])
        await db.commit()
        task_id, original_id = str(task.id), original.id

    async def generate(self, **kwargs):
        async def stream():
            yield '第一部分。'
            if failure == 'stream':
                raise RuntimeError('private upstream diagnostic')
            yield '第二部分。'
        return stream()

    monkeypatch.setattr(OutlineGenerator, 'generate_book_outline', generate)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    fired = False

    def fail_outline_save(session):
        nonlocal fired
        if failure == 'save' and not fired and any(isinstance(obj, Outline) for obj in session.new):
            fired = True
            raise RuntimeError('private persistence diagnostic')

    event.listen(Session, 'before_commit', fail_outline_save)
    try:
        await _run_async_generation_impl(task_id, session_factory=r.sessions)
    finally:
        event.remove(Session, 'before_commit', fail_outline_save)

    expected = '第一部分。' if failure == 'stream' else '第一部分。第二部分。'
    async with r.sessions() as db:
        saved = await db.get(GenerationTask, task_id)
        outlines = list((await db.scalars(select(Outline).where(Outline.project_id == r.project))).all())
        assert saved.progress_text == saved.result_text == expected
        assert saved.char_count == len(expected)
        assert len(outlines) == 1
        if failure:
            assert saved.status == 'failed'
            assert '未完成' in saved.error_message
            assert 'private' not in saved.error_message
            assert outlines[0].id == original_id
            assert outlines[0].content_json['raw_text'] == '作者原大纲。'
        else:
            assert saved.status == 'completed'
            assert not saved.error_message
            assert outlines[0].content_json['raw_text'] == expected
    if failure == 'save':
        assert fired


@pytest.mark.asyncio
async def test_staged_done_metadata_creates_planned_volume(runtime_db, monkeypatch):
    from app.models.project import Volume
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: Outline.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        task = GenerationTask(project_id=r.project, task_type='outline_book', params_json={})
        db.add(task)
        await db.commit()
        task_id = str(task.id)
    plan = [{'idx': 2, 'title': '约定', 'theme': '守约', 'core_conflict': '修书与关店', 'est_chapters': 3}]
    structured = {'sections': {'characters': '林禾与周砚'}}
    final_text = '一、修补以后\n七、分卷规划\n第二卷：约定。'

    async def generate(self, **kwargs):
        async def stream():
            yield {'event': 'stage_chunk', 'delta': '临时阶段文字'}
            yield {'event': 'done', 'full_outline': final_text,
                   'volume_plan': plan, 'structured': structured}
        return stream()

    monkeypatch.setattr(OutlineGenerator, 'generate_book_outline', generate)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    await _run_async_generation_impl(task_id, session_factory=r.sessions)
    async with r.sessions() as db:
        task = await db.get(GenerationTask, task_id)
        assert task.status == 'completed'
        outline = (await db.scalars(select(Outline).where(Outline.project_id == r.project))).one()
        assert outline.content_json['raw_text'] == final_text
        assert outline.content_json['volume_plan'] == plan
        assert outline.content_json['structured'] == structured
        volume = (await db.scalars(select(Volume).where(Volume.project_id == r.project, Volume.volume_idx == 2))).one()
        assert volume.title == '约定'


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    '我会把菜谱带回去。\n如果你明天来，我就在这里等你。\n让我再看一眼。\n',
    '当然，她听见了。\n希望你记得今天说过的话。\n注意：桌上的家谱还没干。\n',
    '\n'.join(
        f'陈青沿着旧纸巷慢慢往前走了第{i}遍，巷子两侧的门板都合着口，'
        '雨水顺着瓦沿往下滴，他把伞压得更低了一些，脚步没有停，拐角那家铺子的灯还亮着半扇窗。'
        for i in range(50)
    ) + '\n他把灯关了，而',
])
async def test_async_save_preserves_speech_paragraphs_and_punctuation(runtime_db, monkeypatch, body):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: Outline.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        task = GenerationTask(project_id=r.project, task_type='outline_book', params_json={})
        db.add(task)
        await db.commit()
        task_id = str(task.id)
    text = '一、故事片段\n' + body

    async def generate(self, **kwargs):
        async def stream():
            yield text
        return stream()

    monkeypatch.setattr(OutlineGenerator, 'generate_book_outline', generate)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    await _run_async_generation_impl(task_id, session_factory=r.sessions)
    async with r.sessions() as db:
        saved = await db.get(GenerationTask, task_id)
        assert saved.status == 'completed'
        assert saved.result_text == text
        outline = (await db.scalars(select(Outline).where(Outline.project_id == r.project))).one()
        assert outline.content_json['raw_text'] == text


@pytest.mark.asyncio
async def test_async_book_passes_project_scale_and_explicit_counts(runtime_db, monkeypatch):
    from app.models.project import Project
    r = runtime_db
    async with r.sessions() as db:
        project = await db.get(Project, r.project)
        project.target_word_count = 5400
        project.settings_json = {'target_chapter_words': 1800, 'chapters_per_volume_min': 3,
                                 'chapters_per_volume_max': 3}
        task = GenerationTask(project_id=r.project, task_type='outline_book',
                              params_json={'target_volumes': 1, 'target_chapters_per_volume': 3})
        db.add(task); await db.commit(); tid = str(task.id)
    captured = {}
    async def generate(self, **kwargs):
        captured.update(kwargs)
        raise RuntimeError('capture only')
    monkeypatch.setattr(OutlineGenerator, 'generate_book_outline', generate)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    await _run_async_generation_impl(tid, session_factory=r.sessions)
    assert captured['scale'] == {'target_word_count': 5400, 'n_chapters': 3,
                                 'n_volumes': 1, 'chapters_per_volume': 3, 'chapter_words': 1800}


@pytest.mark.asyncio
@pytest.mark.parametrize('chapter_counts,existing_budget', [([3], None), ([2, 3], None), ([2, 3], 1234)])
async def test_new_volume_budgets_follow_book_plan_without_overwriting_existing(runtime_db, monkeypatch, chapter_counts, existing_budget):
    from app.models.project import Project, Volume
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: Outline.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        project = Project(title='短篇预算', target_word_count=5401)
        db.add(project); await db.flush(); pid = project.id
        if existing_budget is not None:
            db.add(Volume(project_id=pid, volume_idx=1, title='作者卷名', target_word_count=existing_budget))
        task = GenerationTask(project_id=pid, task_type='outline_book', params_json={})
        db.add(task); await db.commit(); tid = str(task.id)
    plan = [{'idx': idx, 'title': f'第{idx}卷', 'est_chapters': count}
            for idx, count in enumerate(chapter_counts, 1)]
    async def generate(self, **kwargs):
        async def stream():
            yield {'event': 'done', 'full_outline': '一、故事\n兑现承诺。', 'volume_plan': plan}
        return stream()
    monkeypatch.setattr(OutlineGenerator, 'generate_book_outline', generate)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    await _run_async_generation_impl(tid, session_factory=r.sessions)
    async with r.sessions() as db:
        saved = await db.get(GenerationTask, tid)
        assert saved.status == 'completed'
        volumes = (await db.scalars(select(Volume).where(Volume.project_id == pid).order_by(Volume.volume_idx))).all()
        expected = [5401] if len(chapter_counts) == 1 else [2160, 3241]
        if existing_budget is not None:
            expected[0] = existing_budget
            assert volumes[0].title == '作者卷名'
        else:
            assert sum(v.target_word_count for v in volumes) == 5401
        assert [v.target_word_count for v in volumes] == expected
