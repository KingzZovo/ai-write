"""v0.7 — Chapter generation state machine runner.

Pipeline: planning -> drafting -> critic -> [rewrite] -> finalize -> done.
Every phase persists a checkpoint on the GenerationRun row so the run can
resume after a crash.

Usage:
    run = await start_run(project_id=..., chapter_id=..., db=...)
    await execute_run(run_id=run.id, db=...)

    # resume after a crash
    await execute_run(run_id=run.id, db=..., resume=True)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_run import (
    PHASE_CRITIC,
    PHASE_DONE,
    PHASE_DRAFTING,
    PHASE_FAILED,
    PHASE_FINALIZE,
    PHASE_PLANNING,
    PHASE_REWRITE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    CriticReport,
    GenerationRun,
)
from app.services.context_pack import ContextPackBuilder
from app.services.critic_service import run_critic
from app.services.prompt_registry import run_text_prompt

logger = logging.getLogger(__name__)


async def start_run(
    *,
    project_id: str,
    chapter_id: str | None,
    db: AsyncSession,
    max_rewrite_count: int = 2,
) -> GenerationRun:
    run = GenerationRun(
        id=uuid.uuid4(),
        project_id=project_id,
        chapter_id=chapter_id,
        phase=PHASE_PLANNING,
        status=STATUS_RUNNING,
        checkpoint_data={},
        max_rewrite_count=max_rewrite_count,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _checkpoint_set(run: GenerationRun, phase: str, data: dict[str, Any]) -> None:
    cp = dict(run.checkpoint_data or {})
    cp[phase] = data
    run.checkpoint_data = cp
    run.updated_at = datetime.now(timezone.utc)


async def _phase_planning(run: GenerationRun, db: AsyncSession) -> dict[str, Any]:
    """Build the ContextPack for this run."""
    builder = ContextPackBuilder(db=db)
    pack = await builder.build(
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
    )
    pack_snapshot = {
        "rag_snippets": list(getattr(pack, "rag_snippets", []) or [])[:20],
        "style_samples": list(getattr(pack, "style_samples", []) or [])[:5],
        "meta_project_id": str(run.project_id),
    }
    return {"pack": pack_snapshot}


async def _phase_drafting(
    run: GenerationRun,
    db: AsyncSession,
    planning_data: dict[str, Any],
) -> dict[str, Any]:
    pack = planning_data.get("pack", {})
    context_block = "\n".join(pack.get("rag_snippets") or [])
    user_content = (
        f"<上下文>\n{context_block}\n</上下文>\n\n请撰写本章正文。"
    )
    result = await run_text_prompt(
        "generation",
        user_content,
        db,
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
    )
    draft = getattr(result, "text", "") or str(result or "")
    return {"text": draft}


async def _phase_critic(
    run: GenerationRun,
    db: AsyncSession,
    draft_text: str,
    round_num: int,
) -> dict[str, Any]:
    pack_summary = ""
    critic_out = await run_critic(
        draft=draft_text,
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
        db=db,
        pack_summary=pack_summary,
    )
    # Persist critic report row
    report = CriticReport(
        id=uuid.uuid4(),
        run_id=run.id,
        round=round_num,
        issues_json=critic_out.get("issues", []),
    )
    db.add(report)
    await db.commit()
    return critic_out


async def _phase_rewrite(
    run: GenerationRun,
    db: AsyncSession,
    draft_text: str,
    critic_report: dict[str, Any],
) -> dict[str, Any]:
    issues = critic_report.get("issues") or []
    issue_lines = "\n".join(
        f"- [{i.get('severity','?')}] {i.get('category','?')}: {i.get('desc','')}"
        for i in issues
        if i.get("severity") == "hard"
    )
    user_content = (
        f"<原初稿>\n{draft_text}\n</原初稿>\n\n"
        f"<必须修正的问题>\n{issue_lines}\n</必须修正的问题>\n\n"
        "保持原有情节主线，仅修正上述问题后重写全章。"
    )
    result = await run_text_prompt(
        "rewrite",
        user_content,
        db,
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
    )
    new_text = getattr(result, "text", "") or str(result or "")
    return {"text": new_text}


async def execute_run(
    *,
    run_id: str,
    db: AsyncSession,
    resume: bool = False,
) -> dict[str, Any]:
    """Run the state machine from the current phase until done or failed.

    The function is restartable: if called with resume=True it will continue
    from whichever phase is persisted on the row.
    """
    run = await db.get(GenerationRun, run_id)
    if run is None:
        raise ValueError(f"GenerationRun {run_id} not found")

    cp = dict(run.checkpoint_data or {})
    try:
        # planning
        if run.phase == PHASE_PLANNING or (resume and PHASE_PLANNING not in cp):
            data = await _phase_planning(run, db)
            _checkpoint_set(run, PHASE_PLANNING, data)
            run.phase = PHASE_DRAFTING
            await db.commit()
            cp[PHASE_PLANNING] = data

        # drafting
        if run.phase == PHASE_DRAFTING:
            data = await _phase_drafting(run, db, cp.get(PHASE_PLANNING, {}))
            _checkpoint_set(run, PHASE_DRAFTING, data)
            run.phase = PHASE_CRITIC
            await db.commit()
            cp[PHASE_DRAFTING] = data

        draft_text = (cp.get(PHASE_DRAFTING) or {}).get("text") or ""

        # critic / rewrite loop
        while run.phase in (PHASE_CRITIC, PHASE_REWRITE):
            if run.phase == PHASE_CRITIC:
                round_num = run.rewrite_count + 1
                critic_out = await _phase_critic(run, db, draft_text, round_num)
                cp[PHASE_CRITIC] = {"report": critic_out, "round": round_num}
                _checkpoint_set(run, PHASE_CRITIC, cp[PHASE_CRITIC])
                hard_count = critic_out.get("hard_count", 0)
                if hard_count > 0 and run.rewrite_count < run.max_rewrite_count:
                    run.phase = PHASE_REWRITE
                else:
                    run.phase = PHASE_FINALIZE
                await db.commit()

            if run.phase == PHASE_REWRITE:
                rewrite_out = await _phase_rewrite(
                    run, db, draft_text, cp.get(PHASE_CRITIC, {}).get("report", {})
                )
                draft_text = rewrite_out.get("text") or draft_text
                cp[PHASE_DRAFTING] = {"text": draft_text}
                _checkpoint_set(run, PHASE_DRAFTING, cp[PHASE_DRAFTING])
                run.rewrite_count += 1
                run.phase = PHASE_CRITIC
                await db.commit()

        # finalize
        if run.phase == PHASE_FINALIZE:
            cp[PHASE_FINALIZE] = {"final_text": draft_text}
            _checkpoint_set(run, PHASE_FINALIZE, cp[PHASE_FINALIZE])
            run.phase = PHASE_DONE
            run.status = STATUS_DONE
            await db.commit()

        return {
            "status": run.status,
            "phase": run.phase,
            "rewrite_count": run.rewrite_count,
            "final_text": (cp.get(PHASE_FINALIZE) or {}).get("final_text") or draft_text,
        }
    except Exception as exc:
        logger.exception("generation run failed")
        run.status = STATUS_FAILED
        run.phase = PHASE_FAILED
        run.last_error = str(exc)[:2000]
        await db.commit()
        return {"status": STATUS_FAILED, "error": str(exc)[:500]}
