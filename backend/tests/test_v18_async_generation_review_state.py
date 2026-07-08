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

    text = _stage_needs_review_chapter_text(
        task,
        chapter,
        "  第一章正文  ",
        error_message="quality_gate blocked: improved_but_not_passed",
    )

    assert text == "第一章正文"
    assert task.status == "needs_review"
    assert task.error_message == "quality_gate blocked: improved_but_not_passed"
    assert task.result_text == "第一章正文"
    assert task.progress_text == "第一章正文"
    assert task.char_count == 5
    assert chapter.content_text == "第一章正文"
    assert chapter.word_count == 5
    assert chapter.status == "needs_review"
