"""Create one complete planned arc inside the caller's structure transaction."""
from copy import deepcopy
from dataclasses import asdict
from uuid import UUID, uuid4

from app.models.project import Chapter, Outline, Volume
from app.services.arc_loop import ArcState, serialize_arc_state
from app.services.arc_outline import validate_complete_arc_outline
from app.services.chapter_order import reindex_chapter_structure, require_free_index
from app.services.outline_order import bind_chapter_content, bind_volume_content


async def materialize_arc(db, project_id, material, volume_idx, outline):
    """Caller owns lock_chapter_structure and commits the receipt with these rows."""
    target = material["target_chapters"]
    plan = validate_complete_arc_outline(outline, target)
    await require_free_index(db, Volume, project_id, volume_idx)
    volume = Volume(id=uuid4(), project_id=UUID(str(project_id)), title=plan["title"],
                    volume_idx=volume_idx, summary=material["core_setup"])
    db.add(volume)
    chapters = []
    for item in plan["chapter_summaries"]:
        chapter = Chapter(id=uuid4(), volume_id=volume.id, title=item["title"],
                          chapter_idx=item["chapter_idx"], outline_json=deepcopy(item), status="draft")
        chapters.append(chapter)
        db.add(chapter)
    state = ArcState(title=plan["title"], core_setup=material["core_setup"], opening_scene=material["opening_scene"],
                     target_chapters=target, status="active", chapters_written=0, running_outline="", next_direction=None)
    content = serialize_arc_state(state, volume_idx=volume_idx, base=plan)
    content["_arc"]["input_material"] = {key: material[key] for key in ("idea", "background", "core_setup", "opening_scene")}
    content["_arc"]["chapter_order"] = [str(chapter.id) for chapter in chapters]
    content = bind_volume_content(content, volume, chapters)
    for chapter in chapters:
        chapter.outline_json = bind_chapter_content(chapter.outline_json, chapter, volume_idx=volume_idx)
    saved_outline = Outline(id=uuid4(), project_id=UUID(str(project_id)), level="volume", content_json=content, is_confirmed=1)
    db.add(saved_outline)
    await db.flush()
    await reindex_chapter_structure(db, project_id)
    return {"volume_id": str(volume.id), "volume_idx": volume_idx, "outline_id": str(saved_outline.id),
            "chapter_ids": [str(chapter.id) for chapter in chapters], "arc": asdict(state)}
