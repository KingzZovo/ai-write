"""Real PostgreSQL acceptance for atomic volume edits (requires integration DB)."""
from copy import deepcopy

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from fastapi import HTTPException

from app.api.outlines import update_outline, OutlineUpdate
from app.models.project import Outline, Volume, Chapter
from app.services.outline_order import stub_payload
from tests.test_pipeline_control import runtime_db
from tests.test_volume_creation import creation


@pytest_asyncio.fixture
async def planned(creation):
    r = creation
    async with r.sessions() as db:
        v = Volume(project_id=r.pid, volume_idx=1, title='卷一')
        db.add(v); await db.flush()
        old = {'volume_id': str(v.id), 'volume_idx': 1, 'chapter_summaries': [
            {'chapter_idx': i, 'summary': f'旧摘要{i}'} for i in range(1, 4)]}
        outline = Outline(project_id=r.pid, level='volume', parent_id=r.bid, content_json=old)
        db.add(outline)
        for idx, item in enumerate(old['chapter_summaries'], 1):
            db.add(Chapter(volume_id=v.id, chapter_idx=idx, title=f'作者标题{idx}',
                outline_json={'scenes': ['作者细纲']} if idx == 2 else item,
                content_text='作者正文' if idx == 3 else '', status='draft'))
        await db.commit()
        r.vid, r.oid, r.old = v.id, outline.id, old
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['normal', 'raw_only', 'bad_index', 'commit_failure'])
async def test_volume_edit_updates_only_proven_stubs_atomically(planned, mode):
    r = planned
    new = deepcopy(r.old)
    if mode == 'raw_only': new['raw_text'] = '只改卷纲文字'
    else:
        for item in new['chapter_summaries']: item['summary'] = '新安排'
        new['chapter_summaries'].reverse()
    if mode == 'bad_index': new['chapter_summaries'][1]['chapter_idx'] = 3
    async with r.sessions() as db:
        body = OutlineUpdate(content_json=new, expected_content_json=deepcopy(r.old))
        if mode == 'bad_index':
            with pytest.raises(HTTPException) as exc:
                await update_outline(str(r.pid), str(r.oid), body, db)
            assert exc.value.status_code == 422
            await db.rollback()
        else:
            if mode == 'commit_failure':
                def fail(session): raise RuntimeError('commit failure')
                event.listen(db.sync_session, 'before_commit', fail)
                try:
                    with pytest.raises(HTTPException) as exc:
                        await update_outline(str(r.pid), str(r.oid), body, db)
                    assert exc.value.status_code == 503
                finally:
                    event.remove(db.sync_session, 'before_commit', fail)
                    await db.rollback()
            else:
                result = await update_outline(str(r.pid), str(r.oid), body, db)
                await db.commit()
                assert result.save_status == 'saved'
                assert result.synced_chapters == (1 if mode == 'normal' else 0)
                assert result.preserved_chapters == (2 if mode == 'normal' else 0)
    async with r.sessions() as db:
        outline = await db.get(Outline, r.oid)
        expected = deepcopy(r.old if mode in ('bad_index', 'commit_failure') else new)
        actual = {k: v for k, v in outline.content_json.items() if k != '_author_text_revision'}
        for value in (expected, actual):
            value['chapter_summaries'] = sorted(value['chapter_summaries'], key=lambda item: item['chapter_idx'])
        assert stub_payload(actual) == stub_payload(expected)
        assert actual['volume_id'] == str(r.vid) and actual['volume_idx'] == 1
        if mode == 'raw_only': assert outline.content_json.get('_author_text_revision')
        chapters = (await db.scalars(select(Chapter).where(Chapter.volume_id == r.vid).order_by(Chapter.chapter_idx))).all()
        assert chapters[0].outline_json['summary'] == ('新安排' if mode == 'normal' else '旧摘要1')
        assert chapters[1].outline_json == {'scenes': ['作者细纲']}
        assert chapters[2].outline_json['summary'] == '旧摘要3'
        assert chapters[2].content_text == '作者正文'
        assert [c.title for c in chapters] == ['作者标题1', '作者标题2', '作者标题3']
