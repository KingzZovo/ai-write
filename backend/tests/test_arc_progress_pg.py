"""PG serialization and actual context-builder wiring; skipped while offline."""
import asyncio
from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.api.arc_progress import ArcProgressBody, write_arc_progress
from app.models.arc_progress import ArcProgressWrite
from app.models.project import Chapter, Outline, VolumeSummary
from app.services.arc_loop import ArcState, serialize_arc_state
from app.services.arc_progress_reader import load_arc_progress
from app.services.chapter_order import lock_chapter_structure
from app.services.context_pack import ContextPack, ContextPackBuilder
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


@pytest_asyncio.fixture
async def arc_book(runtime_db):
    r = runtime_db
    async with r.engine.begin() as connection:
        await connection.run_sync(lambda db: ArcProgressWrite.__table__.create(db))
        await connection.run_sync(lambda db: VolumeSummary.__table__.create(db))
    async with r.sessions() as db:
        first = await db.get(Chapter, r.chapter)
        r.vid = first.volume_id
        first.global_idx = 1
        more = [Chapter(id=uuid4(), volume_id=r.vid, chapter_idx=i, global_idx=i,
                        title=f"第{i}章", content_text="本章原稿。", status="draft") for i in range(2, 5)]
        db.add_all(more)
        content = serialize_arc_state(ArcState(title="修书", core_setup="归还旧书", opening_scene="检查封面", target_chapters=4),
            volume_idx=1, base={"volume_id": str(r.vid), "raw_text": "修复旧书并归还林禾。"})
        content["_arc"]["chapter_order"] = [str(first.id), *[str(c.id) for c in more]]
        outline = Outline(project_id=r.project, level="volume", is_confirmed=1, content_json=content)
        db.add(outline); await db.commit()
        r.oid = outline.id
        r.source = (await load_arc_progress(db, r.project, r.vid)).source
        r.title, r.prose = first.title, first.content_text
    return r


def request(r, note="作者读过这份保存稿。"):
    return ArcProgressBody(request_id=str(uuid4()), expected_source=deepcopy(r.source), action="acknowledge",
        chapter_id=str(r.chapter), expected_title=r.title, expected_content_text=r.prose, author_note=note)


async def write(r, body):
    async with r.sessions() as db:
        try: return 200, await write_arc_progress(r.project, r.vid, body, db)
        except HTTPException as exc: return exc.status_code, exc.detail


@pytest.mark.asyncio
async def test_identical_request_race_appends_once(arc_book):
    r = arc_book; body = request(r)
    results = await asyncio.gather(write(r, body), write(r, body))
    assert any(code == 200 for code, _ in results) and all(code in (200, 409) for code, _ in results)
    result = next(value for code, value in results if code == 200)
    assert await write(r, body) == (200, result)
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ArcProgressWrite)) == 1


@pytest.mark.asyncio
async def test_competing_requests_cannot_both_use_the_same_original_source(arc_book):
    r = arc_book
    results = await asyncio.gather(write(r, request(r, "甲作者说明")), write(r, request(r, "乙作者说明")))
    assert sorted(code for code, _ in results) == [200, 409]
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ArcProgressWrite)) == 1


@pytest.mark.asyncio
async def test_structure_and_manuscript_writers_exclude_arc_progress(arc_book):
    r = arc_book
    async with r.sessions() as owner:
        await lock_chapter_structure(owner, r.project)
        assert (await write(r, request(r)))[0] == 409
        await owner.rollback()
    assert (await write(r, request(r)))[0] == 200


@pytest.mark.asyncio
async def test_lost_pg_commit_confirmation_replays_after_a_later_manuscript_edit(arc_book):
    r = arc_book; body = request(r)
    async with r.sessions() as db:
        commit = db.commit
        async def lose_ack():
            await commit()
            raise RuntimeError("private diagnostic")
        db.commit = lose_ack
        with pytest.raises(HTTPException) as exc: await write_arc_progress(r.project, r.vid, body, db)
        assert exc.value.status_code == 503
    async with r.sessions() as db:
        (await db.get(Chapter, r.chapter)).content_text = "作者后来修改的正文。"
        await db.commit()
    result = await write(r, body)
    assert result[0] == 200 and result[1]["sequence"] == 1
    async with r.sessions() as db:
        assert (await load_arc_progress(db, r.project, r.vid)).public()["confirmed_count"] == 0


@pytest.mark.asyncio
async def test_stale_identity_map_cannot_confirm_changed_title_and_prose(arc_book):
    r = arc_book
    async with r.sessions() as stale:
        await stale.get(Chapter, r.chapter)
        async with r.sessions() as author:
            chapter = await author.get(Chapter, r.chapter)
            chapter.title, chapter.content_text = "新的标题", "新的正文"
            await author.commit()
        with pytest.raises(HTTPException) as exc:
            await write_arc_progress(r.project, r.vid, request(r), stale)
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_actual_context_builder_carries_author_direction_into_required_prompt_material(arc_book):
    r = arc_book
    body = ArcProgressBody(request_id=str(uuid4()), expected_source=r.source,
                           action="direction", direction="先把借书缘由讲明白，允许正常合作。")
    assert (await write(r, body))[0] == 200
    async with r.sessions() as db:
        pack = ContextPack(history_before_global_idx=1)
        await ContextPackBuilder(db=db)._build_proximity(pack, r.project, r.vid, 1)
        assert body.direction in pack.arc_author_guidance
        prompt = pack.to_system_prompt(token_budget=9000)
        assert body.direction in prompt
        assert pack.arc_replaces_volume_outline and prompt.count("修复旧书并归还林禾。") == 1
        assert any(row["source"] == "arc_author_guidance" and row["required"] for row in pack.budget_trace)
