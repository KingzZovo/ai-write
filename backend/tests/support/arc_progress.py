"""Exercise the real reader and writer with explicitly saved test manuscripts."""
from uuid import uuid4

from sqlalchemy import select, update

from app.api.arc_progress import ArcProgressBody, write_arc_progress
from app.models.project import Chapter
from app.services.arc_progress_reader import load_arc_progress


async def request(r, volume, action, **values):
    view = await load_arc_progress(r.db, r.pid, volume.id)
    return ArcProgressBody(request_id=str(uuid4()), expected_source=view.source, action=action, **values)


async def submit(r, volume, action, **values):
    body = await request(r, volume, action, **values)
    return await write_arc_progress(r.pid, volume.id, body, r.db)


def chapter_ids(r, volume):
    return list(r.session.scalars(select(Chapter.id).where(Chapter.volume_id == volume.id).order_by(Chapter.chapter_idx)))


def save_manuscript(r, cid, text="宋岚将旧书合上，交回林禾手中。", *, status="draft", title=None):
    values = {"content_text": text, "content_hash": None, "status": status}
    if title is not None:
        values["title"] = title
    r.session.execute(update(Chapter).where(Chapter.id == cid).values(**values))
    r.session.commit()


async def confirmation_request(r, volume, cid, **extra):
    chapter = r.session.execute(select(Chapter.title, Chapter.content_text).where(Chapter.id == cid)).one()
    return await request(r, volume, "acknowledge", chapter_id=str(cid), expected_title=chapter.title,
                         expected_content_text=chapter.content_text or "", **extra)


async def acknowledge(r, volume, cid, **extra):
    return await write_arc_progress(r.pid, volume.id, await confirmation_request(r, volume, cid, **extra), r.db)
