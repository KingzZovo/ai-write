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
import os
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
from app.services.context_pack import ContextPackBuilder, fetch_writing_rules
from app.services.critic_service import run_critic
from app.services.prompt_registry import run_text_prompt
from app.services.tool_registry import run_tool
from app.services import run_bus

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
    # v0.8 ContextPack v3: 4th recall path — writing_rules scoped to genre_profile.
    try:
        pack.writing_rules = await fetch_writing_rules(db, str(run.project_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("writing_rules fetch skipped: %s", exc)
        pack.writing_rules = []
    pack_snapshot = {
        "rag_snippets": list(getattr(pack, "rag_snippets", []) or [])[:20],
        "style_samples": list(getattr(pack, "style_samples", []) or [])[:5],
        "writing_rules": list(getattr(pack, "writing_rules", []) or [])[:10],
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
    rules = pack.get("writing_rules") or []
    rule_block = "\n".join(f"- {r}" for r in rules) if rules else ""

    # v0.8 Agent tool loop (max 3 rounds, default off)
    tool_context = ""
    if os.getenv("AGENT_TOOL_LOOP_ENABLED", "false").lower() in ("true", "1", "yes"):
        tool_context = await _run_tool_loop(
            db,
            project_id=str(run.project_id),
            chapter_id=str(run.chapter_id) if run.chapter_id else None,
            context_block=context_block,
        )

    parts = [f"<上下文>\n{context_block}\n</上下文>"]
    if rule_block:
        parts.append(f"<写作规则>\n{rule_block}\n</写作规则>")
    if tool_context:
        parts.append(f"<工具核实>\n{tool_context}\n</工具核实>")
    parts.append("请撰写本章正文。")
    user_content = "\n\n".join(parts)
    result = await run_text_prompt(
        "generation",
        user_content,
        db,
        project_id=str(run.project_id),
        chapter_id=str(run.chapter_id) if run.chapter_id else None,
    )
    draft = getattr(result, "text", "") or str(result or "")
    return {"text": draft}


async def _run_tool_loop(
    db: AsyncSession,
    *,
    project_id: str,
    chapter_id: str | None,
    context_block: str,
    max_rounds: int = 3,
) -> str:
    """Minimal v0.8 tool loop.

    Runs up to ``max_rounds`` tool calls per drafting attempt and returns a
    flat text block that the main generation prompt can read. The concrete
    calls here are intentionally conservative so the feature can be enabled
    safely even without careful per-project tuning.
    """
    fragments: list[str] = []
    rounds = 0

    # Round 1: suggest_beat for current progress (best-effort 0.5 if unknown).
    if rounds < max_rounds:
        try:
            beat = await run_tool(
                "suggest_beat",
                {"chapter_progress": 0.5},
                db,
                project_id=project_id,
                chapter_id=chapter_id,
            )
            if isinstance(beat, dict) and beat.get("beat_title"):
                fragments.append(
                    f"beat: {beat.get('beat_title')} — {beat.get('beat_description','')}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("tool suggest_beat skipped: %s", exc)
        rounds += 1

    # Round 2+: check_character_fact for characters detected in context.
    try:
        from sqlalchemy import select as _sel
        from app.models.project import Character

        chars = (
            await db.execute(_sel(Character.name).where(Character.project_id == project_id))
        ).scalars().all()
        mentioned = [n for n in chars if n and n in (context_block or "")]
        for name in mentioned[: max_rounds - rounds]:
            fact = await run_tool(
                "check_character_fact",
                {"character_name": name},
                db,
                project_id=project_id,
                chapter_id=chapter_id,
            )
            if isinstance(fact, dict):
                loc = fact.get("location", "") or "?"
                lvl = fact.get("power_level", "") or "?"
                fragments.append(f"char[{name}]: 位置={loc} 实力={lvl}")
            rounds += 1
            if rounds >= max_rounds:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool check_character_fact loop skipped: %s", exc)

    return "\n".join(fragments)


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
    _rid = str(run.id)
    await run_bus.publish(_rid, agent="runner", event="run.start", payload={"phase": run.phase, "resume": bool(resume)})
    try:
        # planning
        if run.phase == PHASE_PLANNING or (resume and PHASE_PLANNING not in cp):
            await run_bus.publish(_rid, agent="runner", event="phase.start", payload={"phase": PHASE_PLANNING})
            data = await _phase_planning(run, db)
            _checkpoint_set(run, PHASE_PLANNING, data)
            run.phase = PHASE_DRAFTING
            await db.commit()
            cp[PHASE_PLANNING] = data
            await run_bus.publish(_rid, agent="runner", event="phase.end", payload={"phase": PHASE_PLANNING})

        # drafting
        if run.phase == PHASE_DRAFTING:
            await run_bus.publish(_rid, agent="runner", event="phase.start", payload={"phase": PHASE_DRAFTING})
            data = await _phase_drafting(run, db, cp.get(PHASE_PLANNING, {}))
            # v1.0 BVSR: optionally generate N-1 more drafts, critic each,
            # persist all variants, and promote the lowest-scoring one.
            from app.services.bvsr import (
                VariantCandidate,
                bvsr_n,
                is_bvsr_enabled,
                persist_variants,
                rank,
            )
            if is_bvsr_enabled() and run.chapter_id:
                candidates: list[VariantCandidate] = []
                # Critic the first draft
                first_text = data.get("text") or ""
                try:
                    first_critic = await _phase_critic(run, db, first_text, 0)
                except Exception:
                    first_critic = {}
                candidates.append(
                    VariantCandidate(variant_idx=0, content_text=first_text, critic_report=first_critic or {})
                )
                n = bvsr_n()
                for idx in range(1, n):
                    try:
                        d = await _phase_drafting(run, db, cp.get(PHASE_PLANNING, {}))
                        text_i = d.get("text") or ""
                        crit_i = await _phase_critic(run, db, text_i, 0)
                        candidates.append(
                            VariantCandidate(variant_idx=idx, content_text=text_i, critic_report=crit_i or {})
                        )
                    except Exception as _e:
                        logger.warning("BVSR variant %s failed: %s", idx, _e)
                ranked = rank(candidates)
                winner = ranked[0]
                try:
                    await persist_variants(
                        db,
                        chapter_id=run.chapter_id,
                        run_id=run.id,
                        candidates=candidates,
                        winner_idx=winner.variant_idx,
                        commit=False,
                    )
                except Exception as _e:
                    logger.warning("BVSR persist_variants failed: %s", _e)
                data = {"text": winner.content_text, "bvsr": {"n": len(candidates), "winner_idx": winner.variant_idx, "winner_score": winner.score}}
            _checkpoint_set(run, PHASE_DRAFTING, data)
            run.phase = PHASE_CRITIC
            await db.commit()
            cp[PHASE_DRAFTING] = data
            await run_bus.publish(_rid, agent="runner", event="phase.end", payload={"phase": PHASE_DRAFTING, "bvsr": (data or {}).get("bvsr")})

        draft_text = (cp.get(PHASE_DRAFTING) or {}).get("text") or ""

        # critic / rewrite loop
        while run.phase in (PHASE_CRITIC, PHASE_REWRITE):
            if run.phase == PHASE_CRITIC:
                round_num = run.rewrite_count + 1
                await run_bus.publish(_rid, agent="runner", event="phase.start", payload={"phase": PHASE_CRITIC, "round": round_num})
                critic_out = await _phase_critic(run, db, draft_text, round_num)
                cp[PHASE_CRITIC] = {"report": critic_out, "round": round_num}
                _checkpoint_set(run, PHASE_CRITIC, cp[PHASE_CRITIC])
                hard_count = critic_out.get("hard_count", 0)
                if hard_count > 0 and run.rewrite_count < run.max_rewrite_count:
                    run.phase = PHASE_REWRITE
                else:
                    run.phase = PHASE_FINALIZE
                await db.commit()
                await run_bus.publish(_rid, agent="runner", event="phase.end", payload={"phase": PHASE_CRITIC, "round": round_num, "hard_count": hard_count, "next": run.phase})

            if run.phase == PHASE_REWRITE:
                await run_bus.publish(_rid, agent="runner", event="phase.start", payload={"phase": PHASE_REWRITE, "round": run.rewrite_count + 1})
                rewrite_out = await _phase_rewrite(
                    run, db, draft_text, cp.get(PHASE_CRITIC, {}).get("report", {})
                )
                draft_text = rewrite_out.get("text") or draft_text
                cp[PHASE_DRAFTING] = {"text": draft_text}
                _checkpoint_set(run, PHASE_DRAFTING, cp[PHASE_DRAFTING])
                run.rewrite_count += 1
                run.phase = PHASE_CRITIC
                await db.commit()
                await run_bus.publish(_rid, agent="runner", event="phase.end", payload={"phase": PHASE_REWRITE, "round": run.rewrite_count})

        # finalize
        if run.phase == PHASE_FINALIZE:
            await run_bus.publish(_rid, agent="runner", event="phase.start", payload={"phase": PHASE_FINALIZE})
            cp[PHASE_FINALIZE] = {"final_text": draft_text}
            _checkpoint_set(run, PHASE_FINALIZE, cp[PHASE_FINALIZE])
            run.phase = PHASE_DONE
            run.status = STATUS_DONE
            await db.commit()
            await run_bus.publish(_rid, agent="runner", event="phase.end", payload={"phase": PHASE_FINALIZE})
            await run_bus.publish(_rid, agent="runner", event="run.done", payload={"status": run.status})

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
        try:
            await run_bus.publish(_rid, agent="runner", event="run.failed", payload={"error": str(exc)[:500]})
        except Exception:
            pass
        return {"status": STATUS_FAILED, "error": str(exc)[:500]}
