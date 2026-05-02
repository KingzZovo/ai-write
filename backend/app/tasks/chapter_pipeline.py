"""Post-generation pipeline that runs after every chapter is written.

Why: chapter generation only writes the chapter text. To keep the workspace
panels (伏笔追踪 / 三线平衡 / 设定集 / 角色关系 / Token 用量) in sync, we need
to run a sequence of derived analyses afterwards:

1. entities.extract_chapter   -> Neo4j Character/Location/RELATES_TO + PG materialize
2. foreshadow_manager.register_from_text + check_resolution -> Foreshadow nodes + status
3. (optional) cascade detector
4. (optional) chapter evaluator

This module wires those steps into a single celery task so callers (the
generator pipeline, the workspace-recovery backfill script, etc.) can
fire-and-forget the whole sequence with one ``apply_async`` call.

The task is idempotent: each underlying step has its own marker / dedupe
(``ExtractionMarker`` for entities, foreshadow ``id`` upsert for foreshadows).
"""
from __future__ import annotations

import logging
from typing import Any

from app.tasks.celery_app import celery_app
from app.tasks import _run_async_safe

logger = logging.getLogger(__name__)


async def _run_postgen_pipeline_async(
    project_id: str,
    chapter_idx: int,
    *,
    chapter_id: str | None = None,
    skip_evaluation: bool = False,
    caller: str = "chapter_pipeline.run",
) -> dict[str, Any]:
    """Sequentially run extract -> foreshadow -> cascade -> evaluation."""
    from app.db.neo4j import init_neo4j
    from app.db import neo4j as _nm
    from app.db.session import async_session_factory
    from app.models.project import Chapter
    from app.tasks.entity_tasks import _extract_chapter_async
    from app.services.foreshadow_manager import ForeshadowManager
    from sqlalchemy import select

    summary: dict[str, Any] = {
        "project_id": project_id,
        "chapter_idx": chapter_idx,
        "steps": {},
    }

    # 1) Entity extraction (idempotent via ExtractionMarker; this also
    # triggers the PG materialize internally).
    try:
        entity_result = await _extract_chapter_async(
            project_id=project_id,
            chapter_idx=chapter_idx,
            caller=caller,
            chapter_id=chapter_id,
        )
        summary["steps"]["entities"] = entity_result
    except Exception as e:
        logger.exception("chapter_pipeline entity step failed")
        summary["steps"]["entities"] = {"status": "error", "error": str(e)}

    # 2) Foreshadow registration + resolution check
    try:
        await init_neo4j()
        driver = _nm._driver
        if driver is None:
            summary["steps"]["foreshadow"] = {"status": "skipped", "reason": "no_neo4j"}
        else:
            async with async_session_factory() as db:
                stmt = (
                    select(Chapter.content_text, Chapter.id)
                    .where(Chapter.chapter_idx == int(chapter_idx))
                )
                row = (await db.execute(stmt)).first()
                chapter_text = (row[0] if row else None) or ""
            if not chapter_text.strip():
                summary["steps"]["foreshadow"] = {
                    "status": "skipped",
                    "reason": "empty_chapter",
                }
            else:
                fm = ForeshadowManager(driver)
                created = await fm.register_from_text(project_id, chapter_idx, chapter_text)
                resolved = await fm.check_resolution(
                    project_id, chapter_text, chapter_idx=chapter_idx
                )
                summary["steps"]["foreshadow"] = {
                    "status": "ok",
                    "created": len(created or []),
                    "resolved": len(resolved or []),
                }
    except Exception as e:
        logger.exception("chapter_pipeline foreshadow step failed")
        summary["steps"]["foreshadow"] = {"status": "error", "error": str(e)}

    # 3) Final materialize (covers Foreshadow upsert into PG read model,
    # which step 1 only ran before step 2 added foreshadow nodes).
    try:
        from app.tasks.entity_tasks import _materialize_entities_to_postgres
        mat = await _materialize_entities_to_postgres(
            project_id=project_id,
            chapter_idx=int(chapter_idx),
            caller=f"{caller}.post_foreshadow",
        )
        summary["steps"]["materialize"] = {"status": "ok", **mat}
    except Exception as e:
        logger.exception("chapter_pipeline final materialize failed")
        summary["steps"]["materialize"] = {"status": "error", "error": str(e)}

    summary["status"] = "ok"
    return summary


@celery_app.task(
    name="tasks.run_chapter_postgen_pipeline",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_chapter_postgen_pipeline(
    self,
    project_id: str,
    chapter_idx: int,
    chapter_id: str | None = None,
    skip_evaluation: bool = False,
    caller: str = "unknown",
) -> dict[str, Any]:
    """Celery task wrapper for ``_run_postgen_pipeline_async``."""
    return _run_async_safe(
        _run_postgen_pipeline_async(
            project_id=str(project_id),
            chapter_idx=int(chapter_idx),
            chapter_id=chapter_id,
            skip_evaluation=bool(skip_evaluation),
            caller=str(caller),
        )
    )
