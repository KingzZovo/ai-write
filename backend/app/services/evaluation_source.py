"""Content identity for historical evaluation results; never an acceptance gate."""
import hashlib
import json


def chapter_evaluation_source(chapter_id, text, outline):
    body = json.dumps([str(chapter_id), text or "", outline or {}],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {"version": 1, "chapter_id": str(chapter_id),
            "sha256": hashlib.sha256(body.encode()).hexdigest()}


def evaluation_source_status(result, chapter):
    source = result.get("chapter_source") if isinstance(result, dict) else None
    if (not isinstance(source, dict) or type(source.get("version")) is not int or source.get("version") != 1
            or not isinstance(source.get("chapter_id"), str)
            or not isinstance(source.get("sha256"), str) or len(source["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in source["sha256"])):
        return "unknown"
    if chapter is None:
        return "changed"
    current = chapter_evaluation_source(chapter.id, chapter.content_text, chapter.outline_json)
    return "unchanged" if source == current else "changed"
