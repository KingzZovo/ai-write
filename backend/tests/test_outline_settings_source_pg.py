"""PostgreSQL source fence checks; keep separate from offline coverage."""
import asyncio

import pytest
from app.models.project import Outline
from app.services.outline_settings_source import read_settings_source, require_current_settings_source, SettingsSourceError
from app.api.outlines import update_outline, OutlineUpdate
from tests.test_pipeline_control import runtime_db
from tests.test_volume_creation import creation


@pytest.mark.asyncio
async def test_settings_recheck_refreshes_cached_outline_after_author_edit(creation):
    r = creation
    async with r.sessions() as db:
        source = await read_settings_source(db, r.pid, r.bid)
        await db.rollback()
        async with r.sessions() as writer:
            await update_outline(str(r.pid), str(r.bid), OutlineUpdate(content_json={'raw_text': '作者新的设定'}), writer)
            await writer.commit()
        with pytest.raises(SettingsSourceError, match='未写入'):
            await require_current_settings_source(db, source)
        await db.rollback()


@pytest.mark.asyncio
async def test_verified_source_blocks_author_edit_until_extraction_transaction_finishes(creation):
    r = creation
    started = asyncio.Event()
    async with r.sessions() as reader:
        source = await read_settings_source(reader, r.pid, r.bid)
        await reader.rollback()
        await require_current_settings_source(reader, source)
        async def edit():
            async with r.sessions() as writer:
                started.set()
                await update_outline(str(r.pid), str(r.bid), OutlineUpdate(content_json={'raw_text': '下一稿'}), writer)
                await writer.commit()
        task = asyncio.create_task(edit())
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            done, _ = await asyncio.wait([task], timeout=0.1)
            assert not done
            await reader.commit()
            await asyncio.wait_for(task, timeout=3)
        finally:
            await reader.rollback()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with r.sessions() as db:
        assert (await db.get(Outline, r.bid)).content_json['raw_text'] == '下一稿'
