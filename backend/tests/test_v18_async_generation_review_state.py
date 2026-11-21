from types import SimpleNamespace

from app.tasks.knowledge_tasks import _stage_needs_review_chapter_text


def test_stage_needs_review_chapter_text_persists_review_state() -> None:
    task = SimpleNamespace(
        status="running",
        error_message=None,
        result_text="",
        progress_text="",
        char_count=0,
    )
    chapter = SimpleNamespace(
        content_text="",
        word_count=0,
        status="draft",
    )

    # NB: staging now runs sanitize_prose (2026-07-26 audit), so the sample
    # must be real prose — "第一章正文" would be stripped as meta leakage.
    text = _stage_needs_review_chapter_text(
        task,
        chapter,
        "  他把门关上了。  ",
        error_message="quality_gate blocked: improved_but_not_passed",
    )

    assert text == "他把门关上了。"
    assert task.status == "needs_review"
    assert task.error_message == "quality_gate blocked: improved_but_not_passed"
    assert task.result_text == "他把门关上了。"
    assert task.progress_text == "他把门关上了。"
    assert task.char_count == 7
    assert chapter.content_text == "他把门关上了。"
    assert chapter.word_count == 7
    assert chapter.status == "needs_review"
