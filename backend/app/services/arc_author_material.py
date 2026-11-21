"""Project an author's actual arc inputs without replacing them with a plan."""
from copy import deepcopy
from dataclasses import dataclass
import json
from uuid import UUID

from app.services.outline_authority import TEXT_REVISION, outline_for_generation


@dataclass(frozen=True)
class ArcWritingMaterial:
    text: str = ""
    replaces_volume_outline: bool = False


def _chapter_ids(value):
    if not isinstance(value, list) or not value or len(value) > 40:
        return False
    try:
        return all(isinstance(cid, str) and str(UUID(cid)) == cid for cid in value) and len(set(value)) == len(value)
    except ValueError:
        return False


def render_arc_author_source(content, *, chapter_id=None):
    """Original setup applies to the arc; its opening keeps its original ID.

    A later text revision is the complete current authority. Old inputs remain
    stored, including damaged values, but do not reappear beside that revision.
    This projection certifies neither the author's facts nor the prose itself.
    """
    if not isinstance(content, dict):
        return ArcWritingMaterial()
    current = outline_for_generation(content)
    raw = current.get("raw_text") if isinstance(current, dict) else None
    if isinstance(raw, str) and raw.strip():
        return ArcWritingMaterial("【作者当前弧大纲，安排尚待正文落实】\n" + raw, True)
    if content.get(TEXT_REVISION):
        raise ValueError("作者当前文字大纲为空，请先核对当前修订，不能启用旧材料")
    arc = content.get("_arc")
    if not isinstance(arc, dict) or arc.get("is_arc") is not True:
        return ArcWritingMaterial()
    if "input_material" in arc:
        if not isinstance(arc["input_material"], dict):
            raise ValueError("原始作者材料必须是完整对象")
        material = deepcopy(arc["input_material"])
    else:
        # Older arcs may have only these author fields. Do not invent a
        # missing idea/background or treat legacy progress as author setup.
        material = {key: deepcopy(arc[key]) for key in ("core_setup", "opening_scene") if key in arc}
    for key in ("idea", "background", "core_setup", "opening_scene"):
        if key in material and not isinstance(material[key], str):
            raise ValueError(f"原始作者材料的 {key} 必须为文字")
    opening = material.pop("opening_scene", "")
    original_order = arc.get("chapter_order")
    if opening.strip() and not _chapter_ids(original_order):
        raise ValueError("原开场没有可核对的章节身份，请在当前大纲中明确修订，不按现章号猜配")
    parts = []
    if any(material.values()):
        parts.append("【作者为本弧提供的原始材料：创作意图、背景与设定】\n"
            "作者后来明确修订的安排优先；哪些事已经发生，仍以前文保存稿为准。\n"
            + json.dumps(material, ensure_ascii=False, allow_nan=False))
    if opening.strip() and str(chapter_id) == original_order[0]:
        parts.append("【作者为本章给定的原开场，属于创作安排而非已发生事实】\n" + opening)
    return ArcWritingMaterial("\n\n".join(parts))
