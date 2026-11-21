"""Backward-compatibility facade for the former monolithic knowledge_tasks.

The 2300-line module was split (2026-07-26) into:

- app.tasks.generation_tasks — run_async_generation / run_pipeline_generation
- app.tasks.book_tasks       — vectorize / upload / batch-test / crawl
- app.tasks.analysis_tasks   — extract_features / run_quality_score
- app.tasks.common           — _run_async / _make_session shared helpers

Celery task names (``tasks.*``) are unchanged. Import sites and tests that
referenced ``app.tasks.knowledge_tasks`` keep working through these
re-exports; new code should import from the specific module.
"""

from app.tasks.common import _make_session, _run_async  # noqa: F401
from app.tasks.generation_tasks import (  # noqa: F401
    _build_chinese_prose_preflight_prompt,
    _resolve_task_chapter_target_words,
    _single_shot_llm_timeout_kwargs,
    _stage_needs_review_chapter_text,
    run_async_generation,
    run_pipeline_generation,
)
from app.tasks.book_tasks import (  # noqa: F401
    batch_test_sources_task,
    crawl_book,
    process_uploaded_book,
    vectorize_book_task,
)
from app.tasks.analysis_tasks import (  # noqa: F401
    extract_features,
    run_quality_score,
)
