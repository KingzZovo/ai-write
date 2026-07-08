"""
Celery tasks for knowledge base operations.

- Crawling novels via book source engine
- Text cleaning and slicing
- Feature extraction (plot + style)
- Quality scoring
- Style clustering
"""

import asyncio
import json
import logging

from app.tasks import celery_app
from app.services.chapter_quality_gate import apply_chapter_quality_gate

logger = logging.getLogger(__name__)


# chapter_evaluator emits a single issue with dimension="system" (and overall=0)
# when the judge response cannot be parsed or the eval errors. These placeholders
# must never leak into the prose-mechanics issue_focus, or the generation-before
# hint degrades to ["system"] and loses its issue-aware value.
_PROSE_PREFLIGHT_PLACEHOLDER_DIMENSIONS = {"system"}


def _single_shot_llm_timeout_kwargs(fallback_timeout: float) -> dict[str, float | int | bool]:
    """Return generic LLM-call kwargs for direct/fallback chapter prose.

    ``fallback_timeout_seconds`` wraps the whole single-shot path. The underlying
    OpenAI-compatible call also needs an explicit per-request timeout and retry
    count so long chapter drafts do not fail at 0 chars on the global default.
    In addition, direct chapter prose now defaults to non-streaming via
    ``SINGLE_SHOT_LLM_STREAM=0`` because some proxy/model routes can raise HTTP/2
    stream INTERNAL_ERROR after minutes of generation, leaving no recoverable
    text in the database. The env toggle keeps the behavior reusable across
    chapters/projects without hard-coding a chapter-specific workaround.
    """
    from app.config import settings

    configured = settings.SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS
    retry_attempts = settings.SINGLE_SHOT_LLM_RETRY_ATTEMPTS
    use_stream = settings.SINGLE_SHOT_LLM_STREAM
    outer = max(float(fallback_timeout or 0), 60.0)
    leave_commit_margin = max(30.0, outer - 30.0)

    if use_stream:
        assumed_endpoints = max(1, settings.SINGLE_SHOT_LLM_BUDGET_ENDPOINTS)
    else:
        assumed_endpoints = max(1, settings.SINGLE_SHOT_LLM_BUDGET_ENDPOINTS)
    endpoint_budget = max(30.0, (outer - 50.0) / float(assumed_endpoints))
    request_timeout = max(30.0, min(configured, leave_commit_margin, endpoint_budget))
    return {"request_timeout": request_timeout, "retry_attempts": retry_attempts, "stream": use_stream}


def _stage_needs_review_chapter_text(task, chapter, text: str, *, error_message: str | None = None) -> str:
    """Stage a generated draft everywhere the UI reads manual-review content."""
    final_text = (text or "").strip()
    task.status = "needs_review"
    task.error_message = ((error_message or "")[:1500] or None)
    task.result_text = final_text
    task.progress_text = final_text
    task.char_count = len(final_text)
    if chapter is not None:
        chapter.content_text = final_text
        chapter.word_count = len(final_text)
        chapter.status = "needs_review"
    return final_text


def _resolve_task_chapter_target_words(params, chapter_target_word_count, project_settings) -> int:
    """Resolve the effective chapter target word count for Celery generation.

    Mirrors the API path (api/generate.py): explicit task param wins over the
    chapter row value, then legacy 50k defaults are remapped via
    resolve_chapter_target_word_count to the project setting or 4000 fallback.
    """
    from app.services.chapter_target_words import resolve_chapter_target_word_count

    return resolve_chapter_target_word_count(
        (params or {}).get("target_words") or chapter_target_word_count,
        (project_settings or {}).get("target_chapter_words"),
    )


def _run_async(coro):
    """Run async function in sync Celery task context.

    v1.7 X2: delegates to the unified _run_async_safe from app.tasks,
    which calls reset_model_router + reset_engine before the new loop and
    dispose_current_engine_async in the finally block. This unifies the
    8 call-sites here with the rest of the codebase and guarantees the
    same loop-bound cache hygiene used by tasks/__init__.py.
    """
    from app.tasks import _run_async_safe
    return _run_async_safe(coro)

async def _build_chinese_prose_preflight_prompt(ch, db) -> tuple[str, dict]:
    """Build diagnostic-only prose-mechanics feedback for the next draft.

    Uses the current stored chapter text and latest non-zero evaluation issues as
    generation-before context. Does not block, repair, or print prose snippets.
    """
    from sqlalchemy import desc, select

    from app.models.project import ChapterEvaluation, EvaluateTask
    from app.services.chinese_prose_mechanics_checker import (
        analyze_chinese_prose_mechanics,
        build_generation_preflight_prompt,
    )

    previous_text = (getattr(ch, "content_text", "") or "").strip()
    if not previous_text:
        return "", {}

    report = analyze_chinese_prose_mechanics(previous_text)
    issue_focus: list[str] = []

    def _extract_issue_focus(issues) -> list[str]:
        labels: list[str] = []
        for issue in issues or []:
            if isinstance(issue, dict):
                dimension = str(issue.get("dimension") or "").strip().lower()
                if dimension in _PROSE_PREFLIGHT_PLACEHOLDER_DIMENSIONS:
                    continue
                raw = (
                    issue.get("violation_type")
                    or issue.get("type")
                    or issue.get("category")
                    or issue.get("dimension")
                )
                label = str(raw).strip() if raw else ""
                if label and label.lower() not in _PROSE_PREFLIGHT_PLACEHOLDER_DIMENSIONS:
                    labels.append(label)
            elif issue:
                labels.append(str(issue)[:80])
        seen: set[str] = set()
        deduped: list[str] = []
        for label in labels:
            if label not in seen:
                seen.add(label)
                deduped.append(label)
        return deduped

    try:
        # Prefer the most recent completed EvaluateTask that actually scored the
        # chapter (overall > 0) and carries non-placeholder issues. Recent celery
        # parse/timeout failures persist as overall=0 with a single
        # dimension="system" issue; selecting them blindly degraded issue_focus to
        # ["system"] and starved the generation-before hint of real violations.
        task_result = await db.execute(
            select(EvaluateTask)
            .where(EvaluateTask.chapter_id == ch.id)
            .where(EvaluateTask.status == "completed")
            .where(EvaluateTask.result_json.is_not(None))
            .order_by(desc(EvaluateTask.completed_at), desc(EvaluateTask.created_at))
            .limit(5)
        )
        for task in task_result.scalars().all():
            result_json = task.result_json if isinstance(task.result_json, dict) else {}
            try:
                overall = float(result_json.get("overall") or 0)
            except (TypeError, ValueError):
                overall = 0.0
            if overall <= 0:
                continue
            focus = _extract_issue_focus(result_json.get("issues") or [])
            if focus:
                issue_focus = focus
                break
        if not issue_focus:
            eval_result = await db.execute(
                select(ChapterEvaluation)
                .where(ChapterEvaluation.chapter_id == ch.id)
                .where(ChapterEvaluation.overall > 0)
                .order_by(desc(ChapterEvaluation.created_at))
                .limit(1)
            )
            latest_eval = eval_result.scalar_one_or_none()
            if latest_eval is not None:
                issue_focus = _extract_issue_focus(
                    getattr(latest_eval, "issues_json", None) or []
                )
    except Exception as err:  # pragma: no cover - prompt feedback must be best effort.
        logger.warning(
            "chinese_prose_preflight issue focus lookup failed chapter_id=%s err=%s",
            getattr(ch, "id", None),
            err,
        )

    safe_metrics = report.to_safe_dict()
    return build_generation_preflight_prompt(report, issue_focus=issue_focus, version_label="v4.40"), {
        "version": "v4.40_mundane_naturalness_age_logic_feedback",
        "diagnostic_only": True,
        "hard_gate": False,
        "metrics": safe_metrics,
        "issue_focus": issue_focus[:12],
        "generation_targets": {
            "short_sentence_run_max": 4,
            "short_sentence_runs_over_target_ratio": 0.25,
            "verb_watchlist_hits_ratio": 0.35,
            "exposition_cluster_risk_ratio": 0.0,
            "exposition_cluster_risk_max": 0,
            "nominal_construction_ban": True,
            "micro_measure_ban": ["半寸", "半息", "半指"],
            "action_trajectory_downgrade": True,
            "dialogue_asymmetry_required": True,
            "few_shot_conflict_flow": True,
            "action_verb_budget_per_paragraph": 1,
            "exposition_reset_target": 0,
            "space_watchlist_hits_max": 0,
            "forbidden_collocation_count_max": 0,
            "zero_tolerance_regression_recovery": True,
            "short_sentence_run_max_target": 6,
            "exposition_cluster_risk_hard_target_for_generation": 0,
            "paragraph_exposition_proxy_reset": True,
            "v4_22_exposition_cluster_split": True,
            "environment_reaction_substitution": True,
            "action_density_clamp_from_v4_15": True,
            "short_run_clamp_from_v4_18": True,
            "length_floor_from_v4_20": True,
            "paragraph_exposition_cluster_target": 2,
            "short_sentence_runs_over_target_max": 6,
            "v4_23_rhythm_reclamp_from_v4_21": True,
            "keep_v4_22_exposition_length_recovery": True,
            "v4_24_length_recovery_without_short_run_chains": True,
            "v4_24_restore_space_and_forbidden_zero": True,
            "v4_25_constraint_priority_order": ["zero_tolerance", "short_sentence_rhythm", "exposition_split", "action_result_only", "dialogue_asymmetry", "length"],
            "v4_25_length_yields_to_rhythm": True,
            "v4_25_length_yields_to_zero_tolerance": True,
            "v4_25_no_short_sentence_chain_for_length": True,
            "v4_25_target_chars_soft_range": [6500, 7500],
            "v4_26_length_pressure_disabled": True,
            "v4_26_target_chars_soft_range": [5200, 6500],
            "v4_26_short_sentence_run_max_target": 4,
            "v4_26_short_sentence_runs_over_target_max": 3,
            "v4_26_zero_tolerance_before_length": True,
            "v4_26_forbidden_and_space_must_be_zero": True,
            "v4_26_no_scene_expansion_for_length": True,
            "v4_27_rhythm_only_no_length_pressure": True,
            "v4_27_ban_comma_chopped_short_clause_chain": True,
            "v4_27_require_medium_causal_sentences": True,
            "v4_27_sentence_count_soft_max": 430,
            "v4_28_restore_strict_rhythm_clamp": True,
            "v4_28_no_scene_expansion_for_rhythm": True,
            "v4_28_sentence_count_soft_max": 520,
            "v4_28_short_sentence_run_max_target": 4,
            "v4_28_short_sentence_runs_over_target_max": 3,
            "v4_29_zero_tolerance_before_rhythm": True,
            "v4_29_forbidden_and_space_must_be_zero": True,
            "v4_29_long_quote_segments_gt80_max": 0,
            "v4_29_target_chars_soft_range": [5800, 6800],
            "v4_29_no_invented_official_terms": True,
            "v4_29_dialogue_fragmentation_without_long_quote": True,
            "v4_30_canonical_terms_not_forbidden": True,
            "v4_30_space_watchlist_terms_must_be_zero": ["半寸", "半息", "半指", "半步", "寸许", "尺许", "肘下", "腋下"],
            "v4_30_surviving_terms_from_v429": {"半息": 1, "半步": 1},
            "v4_30_target_chars_soft_range": [5600, 6600],
            "v4_31_residual_half_xi_must_be_zero": True,
            "v4_31_exposition_cluster_risk_must_be_zero": True,
            "v4_31_reduce_shoubei_repetition": True,
            "v4_31_target_chars_soft_range": [5000, 6200],
            "v4_32_length_must_return_under_6500": True,
            "v4_32_exposition_cluster_risk_must_be_zero": True,
            "v4_32_keep_space_and_forbidden_zero": True,
            "v4_32_target_chars_soft_range": [5200, 6500],
            "v4_32_no_scene_expansion_for_length": True,
            "v4_33_return_internal_self_check_no_hard_gate": True,
            "v4_33_zero_micro_measure_terms": True,
            "v4_33_target_chars_soft_range": [5200, 6200],
            "v4_33_delete_whole_exposition_blocks_over_6500": True,
            "v4_33_no_scene_expansion_for_length": True,
            "v4_36_lexical_zero_first_before_generation": True,
            "v4_36_target_chars_soft_range": [2000, 6000],
            "v4_36_lower_bound_chars_guard": 2000,
            "v4_36_short_sentence_runs_over_target_max": 0,
            "v4_36_zero_residual_micro_measure_terms_first": True,
            "v4_36_no_hard_gate_generation_before_only": True,
            "mundane_scene_plausibility": True,
            "plain_modern_register": True,
            "age_plausibility": True,
            "abstract_reasoning_zero": True,
            "limited_pov_only": True,
            "semantic_density_budget": True,
            "resource_continuity": True,
            "action_causality": True,
            "motivation_bridge": True,
            "awkward_register_count_max": 0,
            "limited_pov_leak_count_max": 0,
            "mundane_logic_violation_count_max": 0,
            "hardship_stack_count_max": 0,
            "resource_continuity_count_max": 0,
            "mundane_register_count_max": 0,
            "action_causality_count_max": 0,
            "motivation_gap_count_max": 0,
            "scene_plausibility_count_max": 0,
            "no_action_chain_for_explanation": True,
            "diagnostic_only": True,
        },
    }


def _make_session():
    """Create a fresh async session factory for Celery tasks (avoids event loop conflicts)."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.config import settings
    eng = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True, pool_size=3)
    return async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)



# ---------------------------------------------------------------------------
# Backward-compatible re-exports (Celery discovers tasks via module imports)
# ---------------------------------------------------------------------------
from app.tasks.generation_tasks import run_async_generation, run_pipeline_generation  # noqa: F401
from app.tasks.book_tasks import vectorize_book_task, process_uploaded_book, crawl_book, batch_test_sources_task  # noqa: F401
from app.tasks.analysis_tasks import extract_features, run_quality_score  # noqa: F401
