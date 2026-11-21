"""Celery tasks for async generation (chapter/outline pipelines).

Split out of knowledge_tasks.py (2026-07-26); task names are unchanged.
"""

from contextlib import aclosing

import asyncio
import json
import logging
import re
import time
from typing import Any

from app.tasks import celery_app
from app.tasks.common import _make_session, _run_async
from app.services.chapter_quality_gate import (
    apply_chapter_quality_gate,
    looks_like_refusal,
    looks_truncated,
)
from app.services.prose_sanitizer import sanitize_prose
from app.services.established_facts import load_established_facts_block
from app.services.production.control import PipelineConflict
from app.services.generation_errors import task_generation_error

logger = logging.getLogger(__name__)


class _ProgressWriter:
    """Bound progress commits by elapsed time, not provider token granularity.

    Callers still own terminal commits; no background writer or shared session.
    """
    def __init__(self, interval_seconds: float = 2.0):
        self.interval_seconds = interval_seconds
        self.next_write_at = 0.0

    async def write(self, db, task, text_factory):
        if time.monotonic() < self.next_write_at:
            return
        text = text_factory()
        if not text.strip():
            # Empty keepalives/control events are not returned prose. Keep
            # any earlier candidate intact; the execution has its own pulse.
            return
        task.progress_text = text
        task.progress_kind = 'candidate'
        task.char_count = len(text)
        await db.commit()
        self.next_write_at = time.monotonic() + self.interval_seconds


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
    from app.config import settings as _settings

    configured = _settings.SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS
    retry_attempts = _settings.SINGLE_SHOT_LLM_RETRY_ATTEMPTS
    use_stream = _settings.SINGLE_SHOT_LLM_STREAM
    outer = max(float(fallback_timeout or 0), 60.0)
    leave_commit_margin = max(30.0, outer - 30.0)

    # Non-streaming single-shot chapter generation only receives text after the
    # whole draft is complete. Splitting the outer budget evenly across the
    # tier-fallback endpoints made long chapter drafts fail deterministically at
    # 0 chars: each endpoint timed out before it could return a full response.
    # Prefer giving the primary route enough wall-clock budget to finish; tier
    # fallback still works for fast failures while avoiding two guaranteed long
    # timeouts. Streaming keeps the split budget because it can surface chunks.
    if use_stream:
        assumed_endpoints = max(1, _settings.SINGLE_SHOT_LLM_BUDGET_ENDPOINTS)
    else:
        assumed_endpoints = max(1, _settings.SINGLE_SHOT_LLM_BUDGET_ENDPOINTS)
    endpoint_budget = max(30.0, (outer - 50.0) / float(assumed_endpoints))
    request_timeout = max(30.0, min(configured, leave_commit_margin, endpoint_budget))
    return {"request_timeout": request_timeout, "retry_attempts": retry_attempts, "stream": use_stream}


async def _dispatch_chapter_entities(chapter, db, project_id) -> None:
    """Enqueue Neo4j entity extraction after a chapter save (audit 2026-07-26).

    Mirrors the SSE path (api/generate.py ``_persist_chapter_now``), which
    dispatches on every persist including needs_review/draft saves: the saved
    text IS the chapter's stored content, and the extraction task re-runs (and
    overwrites) on any later re-save, so drafts never leave stale graph state.
    Never raises — the graph extension being down must not fail chapter saves.
    """
    try:
        from app.services.entity_dispatch import dispatch_for_chapter

        await dispatch_for_chapter(
            chapter,
            db,
            caller="tasks.run_async_generation",
            project_id_hint=project_id,
        )
    except Exception as dispatch_err:
        logger.warning(
            "Entity dispatch after async-generation save failed: %s", dispatch_err
        )


async def _backfill_volume_summary_safe(volume_id) -> None:
    """Fire-safe cross-volume memory backfill after a chapter persist.

    E2E defect (2026-07-26): the needs_review early-return branches (quality
    gate / fact contract / salvage persists) saved the chapter and dispatched
    summarize + entity extraction but returned before the
    ``backfill_prev_volume_summary`` call on the final-save path — so a volume
    whose chapters all landed as needs_review never produced the previous
    volume's VolumeSummary row (the L2 bridge context_pack reads for chapters
    1-3 of the next volume). Threaded into every persist branch, mirroring
    ``_dispatch_chapter_entities``. Opens its own DB session downstream and
    never raises — it must not fail the save.
    """
    if not volume_id:
        return
    try:
        from app.services.memory import backfill_prev_volume_summary

        await backfill_prev_volume_summary(str(volume_id))
    except Exception as volsum_err:
        logger.warning(
            "Volume-summary backfill failed after chapter save: %s", volsum_err
        )


def _persist_pipeline_chapter_text(chapter, text: str) -> bool:
    """Sanitize + persist whole-book pipeline output; True when saved as draft.

    Minimal hardening of the legacy pipeline path (audit 2026-07-26): the text
    now goes through the same sanitizer and truncation/refusal checks as the
    main generation paths instead of being saved "completed" unconditionally.
    A full merge with the real chapter generator is intentionally deferred.
    """
    cleaned = (text or "").strip()
    cleaned, leak_hits = sanitize_prose(cleaned)
    if leak_hits:
        logger.warning(
            "prose_sanitizer stripped meta leakage in pipeline chapter_id=%s hits=%s",
            getattr(chapter, "id", None),
            leak_hits,
        )
    if not cleaned.strip():
        raise ValueError("模型未返回有效正文，已保留原有内容")
    chapter.content_text = cleaned
    chapter.word_count = len(cleaned)
    flagged = looks_truncated(cleaned) or looks_like_refusal(cleaned)
    chapter.status = "draft" if flagged else "completed"
    return flagged


def _purify_generated_text(text: str, task: Any, task_id: str, chapter_id: Any) -> str:
    """剥掉混进正文的**非叙事文本**(服务话术 / 中转诊断)。

    2026-11-20 实测:正文尾部被追加
    "I seem to be encountering an error. Can I try something else for you?"
    (见 docs/evaluation/style-ref-10/ch01.txt:正文 1923 字 + 话术 69 字)。
    旧守卫只认"整章拒答 / 头部拒答",这类"正文 + 尾随话术"一路通过验收并落库。

    净化位置无关且**幂等**;剥完不足以成章时返回残句,交给后续质量门判 needs_review
    —— 绝不把话术当正文,也绝不因为一句尾随话术丢掉整章正文。
    """
    if not text or not str(text).strip():
        return text
    try:
        from app.services.prose_purity import strip_service_noise

        result = strip_service_noise(text)
        if not result.removed:
            return text
        logger.warning(
            "prose_noise_stripped task_id=%s chapter_id=%s removed=%d fatal=%s kept=%d",
            task_id, chapter_id, len(result.removed), result.fatal,
            len(re.sub(r"\s", "", result.text or "")),
        )
        if isinstance(getattr(task, "params_json", None), dict):
            task.params_json = {**(task.params_json or {}),
                                "prose_purity": result.to_dict()}
        return result.text
    except Exception as purify_err:  # noqa: BLE001 - 净化失败不该停掉交付
        logger.warning("prose purity failed task_id=%s err=%s", task_id, purify_err)
        return text


def _stage_needs_review_chapter_text(task, chapter, text: str, *, error_message: str | None = None) -> str:
    """Stage a generated draft everywhere the UI reads manual-review content."""
    final_text = (text or "").strip()
    # Meta/structure leakage guard (audit 2026-07-26): needs_review staging is
    # a persistence path too — strip 第X章/[CH-n] leakage like every other save.
    final_text, _leak_hits = sanitize_prose(final_text)
    if _leak_hits:
        logger.warning(
            "prose_sanitizer stripped meta leakage in needs_review staging hits=%s",
            _leak_hits,
        )
    task.status = "needs_review"
    task.error_message = ((error_message or "")[:1500] or None)
    task.result_text = final_text
    task.progress_text = final_text
    task.progress_kind = 'candidate'
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
    from app.services.chapter_target_words import (
        resolve_chapter_target_word_count,
        resolve_chapter_words,
    )

    return resolve_chapter_target_word_count(
        (params or {}).get("target_words") or chapter_target_word_count,
        resolve_chapter_words(project_settings),
    )


async def _build_chinese_prose_preflight_prompt(ch, db) -> tuple[str, dict]:
    """Build diagnostic-only prose-mechanics feedback for the next draft.

    Uses the current stored chapter text and latest non-zero evaluation issues as
    generation-before context. Does not block, repair, or print prose snippets.
    """
    from sqlalchemy import desc, select

    from app.config import settings as _settings
    from app.models.project import Chapter, ChapterEvaluation, EvaluateTask, Project
    from app.services.chinese_prose_mechanics_checker import (
        aggregate_recurring_defects,
        analyze_chinese_prose_mechanics,
        build_generation_preflight_prompt,
    )

    previous_text = (getattr(ch, "content_text", "") or "").strip()
    if not previous_text:
        return "", {}

    # Era/register bleed detector (docs/DEFECT_GOVERNANCE.md) is genre-gated:
    # resolve the project's genre string best-effort; None keeps it inactive.
    genre_hint: str | None = None
    try:
        project = await db.get(Project, ch.project_id)
        if project is not None:
            genre_hint = " ".join(
                str(part)
                for part in (
                    getattr(project, "genre", None),
                    getattr(project, "genre_profile_code", None),
                )
                if part
            ).strip() or None
    except Exception:
        genre_hint = None

    report = analyze_chinese_prose_mechanics(previous_text, genre_hint=genre_hint)
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

    # Recurring-defect ledger (docs/DEFECT_GOVERNANCE.md): deterministic
    # per-project immunization loop. Collect the latest scored evaluation of
    # each of the project's last N evaluated chapters; tags recurring in >=2 of
    # them become a 【本项目高发问题】 preflight block with their repair action.
    recurring_issue_history: list[list[dict]] = []
    try:
        window = max(1, int(_settings.PREFLIGHT_RECURRING_DEFECT_CHAPTERS))
        eval_rows = await db.execute(
            select(ChapterEvaluation)
            .join(Chapter, ChapterEvaluation.chapter_id == Chapter.id)
            .where(Chapter.project_id == ch.project_id)
            .where(ChapterEvaluation.overall > 0)
            .order_by(desc(ChapterEvaluation.created_at))
            .limit(window * 6)
        )
        latest_issues_by_chapter: dict[str, list[dict]] = {}
        for row in eval_rows.scalars().all():
            chapter_key = str(row.chapter_id)
            if chapter_key in latest_issues_by_chapter:
                continue
            if len(latest_issues_by_chapter) >= window:
                break
            issues = row.issues_json if isinstance(row.issues_json, list) else []
            latest_issues_by_chapter[chapter_key] = [x for x in issues if isinstance(x, dict)]
        recurring_issue_history = list(latest_issues_by_chapter.values())
    except Exception as err:  # pragma: no cover - ledger must be best effort.
        logger.warning(
            "chinese_prose_preflight recurring-defect ledger lookup failed chapter_id=%s err=%s",
            getattr(ch, "id", None),
            err,
        )

    recurring_min = max(1, int(_settings.PREFLIGHT_RECURRING_DEFECT_MIN_CHAPTERS))
    recurring_defects = aggregate_recurring_defects(
        recurring_issue_history, min_chapters=recurring_min
    )
    safe_metrics = report.to_safe_dict()
    return build_generation_preflight_prompt(
        report,
        issue_focus=issue_focus,
        version_label="v4.41",
        recurring_issue_history=recurring_issue_history,
        recurring_min_chapters=recurring_min,
    ), {
        "version": "v4.41_defect_class_immunity_era_seam_ledger",
        "diagnostic_only": True,
        "hard_gate": False,
        "metrics": safe_metrics,
        "issue_focus": issue_focus[:12],
        "genre_hint": genre_hint,
        "era_register_class": report.era_register_class,
        "recurring_defect_window_chapters": len(recurring_issue_history),
        "recurring_defects": {
            tag: int(info.get("chapters") or 0) for tag, info in recurring_defects.items()
        },
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
            "era_register_conflict_count_max": 0,
            "seam_duplication_count_max": 0,
            "no_action_chain_for_explanation": True,
            "diagnostic_only": True,
        },
    }


@celery_app.task(
    name="tasks.run_async_generation",
    # v1.12 L4: This task can run for a long time and previously got stuck in
    # Redis "unacked" state (acked-late) when a worker subprocess stalled.
    # Ack early so a stalled worker won't permanently wedge the message.
    acks_late=False,
    autoretry_for=(PipelineConflict,),
    retry_kwargs={"max_retries": None},
    retry_backoff=5,
    retry_backoff_max=60,
)
def run_async_generation(task_id: str):
    """Run outline/chapter generation in background with progress tracking."""
    _run_async(_run_guarded_generation(task_id))


@celery_app.task(name='tasks.recover_async_generation', acks_late=False)
def recover_async_generation(task_id: str):
    """Explicit periodic recovery; ordinary deliveries never take over a run."""
    try:
        _run_async(_run_guarded_generation(task_id, recover=True))
    except PipelineConflict:
        # The durable task remains discoverable after this worker exits.
        return


_BASE_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
)


def _transient_errors() -> tuple[type[BaseException], ...]:
    """可恢复的基础设施错误:超时/连接类 + sqlalchemy 的 DBAPI/Operational/Interface。"""
    kinds: list[type[BaseException]] = list(_BASE_TRANSIENT_ERRORS)
    try:
        from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

        kinds.extend([DBAPIError, InterfaceError, OperationalError])
    except Exception:  # noqa: BLE001
        pass
    return tuple(kinds)


async def _run_guarded_generation(task_id: str, *, recover=False):
    from sqlalchemy import update
    from uuid import UUID
    from app.db import session
    from app.models.generation_task import GenerationTask
    from app.services.generation_submissions import (
        claim_generation_submission, generation_task_results, valid_chapter_submission_source,
    )
    from app.services.production.writer import guarded_call
    from app.services.production.control import PipelineConflict
    from app.services.production.fencing import StaleChapterWrite
    from app.services.batch_submissions import batch_execution_allows, verify_batch_execution, StaleBatchExecution
    from app.services.generation_task_recovery import recovery_due

    async with session.async_session_factory() as db:
        task = await db.get(GenerationTask, task_id)
        if task is None or (task.status != 'pending' and not (recover and recovery_due(task))):
            return
        project_id, task_type = task.project_id, task.task_type
        # 2026-11-17:后台任务没有请求上下文,若提交时带了 user_id 就沿用它,
        # 让异步生成的用量能归属到真实用户(否则全部记到默认账户)。
        try:
            from app.middlewares.request_identity import set_current_user

            _params = task.params_json if isinstance(task.params_json, dict) else {}
            set_current_user(_params.get("user_id") or _params.get("username"))
        except Exception:  # noqa: BLE001 - 归属失败不应影响生成
            pass
        params = task.params_json if isinstance(task.params_json, dict) else {}
        if params.get('cancelled_at'):
            return
        if params.get('pipeline_id') is not None:
            # The production chapter executor owns these children and calls
            # the core directly. A separate delivery cannot change them.
            return
        if recover and params.get('batch_request_id') is not None:
            return
        if params.get('batch_request_id') is not None and not batch_execution_allows(task_id, params):
            return
    if params.get('batch_request_id') is None:
        from app.services.generation_task_execution import execute_independent_task
        async def independent(writers, readers):
            return await _run_async_generation_impl(task_id, session_factory=writers, read_session_factory=readers)
        return await execute_independent_task(task_id, project_id,
            sessions=session.async_session_factory, generate=independent, recover=recover)
    claimed = False

    async def generate():
        nonlocal claimed
        async with session.async_session_factory() as db:
            if params.get('batch_request_id') is not None:
                await verify_batch_execution(db, task_id, params)
            if task_type == 'outline_volume':
                from app.services.volume_outline_writes import VolumeOutlineError, require_volume_task_source
                try:
                    await require_volume_task_source(db, project_id, params)
                except VolumeOutlineError as exc:
                    raise StaleChapterWrite(str(exc)) from None
            claimed = await claim_generation_submission(db, task_id)
        if not claimed:
            return
        with generation_task_results(task_id):
            return await _run_async_generation_impl(task_id, session_factory=session.async_session_factory)

    try:
        if task_type == 'outline_volume':
            return await guarded_call(session.engine, session.async_session_factory, project_id, generate, metadata_only=True)
        if task_type != 'chapter':
            return await generate()
        submission = params.get('submission')
        source = submission.get('chapter_source') if isinstance(submission, dict) else None
        if not valid_chapter_submission_source(source, params.get('chapter_id')):
            raise StaleChapterWrite('任务缺少提交时的原稿基准，请核对保存稿后重新提交；未开始生成')
        return await guarded_call(session.engine, session.async_session_factory, project_id, generate,
            chapter_id=params.get('chapter_id'), volume_id=params.get('volume_id'),
            chapter_idx=params.get('chapter_idx'), generation_task_id=task_id, expected_chapter_source=source)
    except Exception as exc:
        if isinstance(exc, StaleBatchExecution):
            return
        if isinstance(exc, PipelineConflict) and not claimed:
            # Contention before claim may retry. An interrupted claimed
            # execution must not be silently restarted by broker redelivery.
            raise
        terminal = ('completed', 'needs_review', 'needs_repair', 'cancelled', 'failed')
        async with session.async_session_factory() as db:
            if params.get('batch_request_id') is not None:
                # This Core UPDATE bypasses the ORM result listener. Recheck
                # the batch nonce before a late error can affect a successor.
                try:
                    await verify_batch_execution(db, task_id, params)
                except StaleBatchExecution:
                    return
            condition = GenerationTask.status.notin_(terminal) if claimed else GenerationTask.status == 'pending'
            message = str(exc) if isinstance(exc, StaleChapterWrite) else '生成任务中断,已保留当前正文和候选,请核对后再操作。'
            # 2026-11-17:瞬时基础设施故障不该把任务判死。原先任何异常都写 failed(终态),
            # 而重投/恢复扫描只接管 pending —— 一次 DB 抖动就永久终止这个任务。
            if (any(isinstance(exc, kind) for kind in _transient_errors())
                    and not isinstance(exc, StaleChapterWrite)):
                await db.execute(update(GenerationTask)
                    .where(GenerationTask.id == UUID(str(task_id)), condition)
                    .values(error_message=f'生成任务暂时中断,将自动重试({type(exc).__name__})')
                    .execution_options(synchronize_session=False))
                await db.commit()
            else:
                await db.execute(update(GenerationTask).where(GenerationTask.id == UUID(str(task_id)), condition)
                    .values(status='failed', error_message=message).execution_options(synchronize_session=False))
                await db.commit()
        raise


async def _run_async_generation_impl(task_id: str, *, session_factory=None, read_session_factory=None):
    from app.models.generation_task import GenerationTask
    from app.models.project import Outline
    from app.services.model_router import get_model_router_async
    from app.services.outline_generator import OutlineGenerator

    router = await get_model_router_async()
    session_factory = session_factory or _make_session()
    read_session_factory = read_session_factory or session_factory

    async with session_factory() as db:
        task = await db.get(GenerationTask, task_id)
        if not task or task.status in {'completed', 'needs_review', 'needs_repair', 'cancelled', 'failed'}:
            return
        if isinstance(task.params_json, dict) and task.params_json.get('cancelled_at'):
            return

        task.status = "running"
        await db.commit()

        # 每章 token 预算闸门(默认关闭:`limit=0` 视为不限制)。桌面 SSE 路径
        # (api/generate.py)已经接了它,后台任务此前**没接** —— 同一本书走队列生成时
        # 预算完全不生效。这里对齐。`chapter_budget_verdict` 对未知章节不抛错。
        try:
            from app.services.chapter_budget import chapter_budget_verdict

            _budget = await chapter_budget_verdict(
                db, (task.params_json or {}).get("chapter_id"))
            if _budget.exceeded:
                task.status = "failed"
                task.error_message = "chapter_budget_exceeded"
                task.params_json = {**(task.params_json or {}),
                                    "chapter_budget": _budget.as_event()}
                await db.commit()
                logger.warning(
                    "chapter_budget_exceeded task_id=%s used=%d limit=%d",
                    task_id, _budget.used_tokens, _budget.limit_tokens,
                )
                return
        except Exception as budget_err:  # noqa: BLE001 - 闸门自身故障不该停掉生成
            logger.warning("chapter budget check failed task_id=%s err=%s", task_id, budget_err)

        params = task.params_json or {}
        user_input = params.get("user_input", "")
        project_id = str(task.project_id)
        project_settings = {}
        project_target_word_count = None
        try:
            from app.models.project import Project as _Project
            project = await db.get(_Project, project_id)
            if project:
                project_target_word_count = project.target_word_count
                if isinstance(project.settings_json, dict):
                    project_settings = project.settings_json
        except Exception as _settings_err:
            logger.warning("Async generation project settings load failed: %s", _settings_err)

        def _settings_style_profile_id(settings: dict) -> str | None:
            sid = settings.get("style_profile_id")
            if not sid:
                style_ref = settings.get("style_reference") or {}
                if isinstance(style_ref, dict):
                    sid = style_ref.get("profile_id")
            return sid if isinstance(sid, str) and sid.strip() else None

        def _settings_structure_book_id(settings: dict) -> str | None:
            plot_structure = settings.get("plot_structure") or {}
            if not isinstance(plot_structure, dict):
                return None
            sid = plot_structure.get("structure_book_id")
            return sid if isinstance(sid, str) and sid.strip() else None

        try:
            # Resolve style: explicit task param > project settings > style runtime fallback.
            style_text = ""
            style_id = params.get("style_id") or _settings_style_profile_id(project_settings)
            if style_id:
                from app.models.project import StyleProfile
                from app.services.style_runtime import production_style_text_for_profile
                profile = await db.get(StyleProfile, style_id)
                if profile:
                    # Production style chain (PR-NO-RAW-INJECT): prefer the
                    # dossier style_block, else compile WITHOUT sample-passage
                    # few-shot — same [风格要求] content as the SSE path.
                    style_text, _style_source, _style_ref = (
                        await production_style_text_for_profile(db, profile)
                    )
            if not style_text:
                from app.services.style_runtime import resolve_style_prompt
                style_text = await resolve_style_prompt(db, project_id) or ""

            # Optional: compile selected extracted plot structure. Prefer the stored
            # ReferenceBook.metadata_json.plot_structure produced by decompile; only
            # fall back to re-extracting chunks when that profile is missing.
            structure_text = ""
            structure_book_id = params.get("structure_book_id") or _settings_structure_book_id(project_settings)
            if structure_book_id:
                try:
                    from app.models.project import ReferenceBook as _ReferenceBook, TextChunk as _TC
                    from app.services.plot_structure import extract_plot_structure, compile_structure_prompt
                    from sqlalchemy import select as _sel
                    ps = None
                    ref_book = await db.get(_ReferenceBook, structure_book_id)
                    if ref_book and isinstance(ref_book.metadata_json, dict):
                        stored = ref_book.metadata_json.get("plot_structure")
                        if isinstance(stored, dict) and "error" not in stored:
                            ps = stored
                    if ps is None:
                        tc_result = await db.execute(
                            _sel(_TC).where(_TC.book_id == structure_book_id).order_by(_TC.sequence_id)
                        )
                        tc_chunks = list(tc_result.scalars().all())
                        if tc_chunks:
                            n = len(tc_chunks)
                            tc_samples = [tc_chunks[i].content for i in range(0, n, max(1, n // 6))][:6]
                            ps = await extract_plot_structure("\n\n".join(tc_samples))
                    if isinstance(ps, dict) and "error" not in ps:
                        structure_text = compile_structure_prompt(ps)
                except Exception as e:
                    logger.warning("Plot structure compile failed: %s", e)

            # Build enhanced input
            enhanced = user_input
            if style_text:
                enhanced = f"{style_text}\n\n{user_input}"
            if structure_text:
                enhanced = f"{enhanced}\n\n{structure_text}"

            # Generate based on task_type. project_id must be threaded so the
            # staged volume path can read settings_json (chapters_per_volume_*)
            # — without it the E2E chapters-per-volume fix is a no-op on the
            # celery path (and outline LLM calls skip usage logging).
            generator = OutlineGenerator(project_id=project_id)
            collected = []
            book_outline_metadata: dict = {}

            # v4.6 reliability: normalize streamed chunks before appending/joining.
            # Some prompt paths yield structured dict events (for example
            # {"text": "..."}) while older code assumed each chunk was str,
            # causing TypeError: sequence item 0: expected str instance, dict found.
            # Keep this generic for all async task types, not a Chapter 5 special case.
            def _chunk_to_text(chunk) -> str:
                if chunk is None:
                    return ""
                if isinstance(chunk, str):
                    return chunk
                if isinstance(chunk, dict):
                    for key in ("text", "content", "delta", "message", "data"):
                        value = chunk.get(key)
                        if isinstance(value, str):
                            return value
                    return ""
                return str(chunk)

            def _append_chunk(chunk) -> str:
                text = _chunk_to_text(chunk)
                if text:
                    collected.append(text)
                return text

            def _joined_collected() -> str:
                return "".join(_chunk_to_text(item) for item in collected)
            progress_writer = _ProgressWriter()
            generated_chapter = None
            generated_chapter_outline: dict = {}
            generated_chapter_user_instr = ""
            generated_chapter_target_words: int | None = None
            generated_chapter_id_safe: str | None = None
            generated_chapter_volume_id_safe: str | None = None
            generated_chapter_idx_safe: int | None = None

            if task.task_type == "outline_from_reference":
                # v1.5.0 D-2: async outline-from-reference. Wraps the
                # service-layer single-shot LLM call as a single chunk so the
                # downstream Markdown-strip / humanize / auto-save-outline
                # logic still applies uniformly. Required params (in
                # task.params_json): reference_book_id, intent, style_hint,
                # target_volumes, target_chapters_per_volume.
                from app.services.outline_from_reference import (
                    build_outline_from_reference,
                )

                ref_id = params.get("reference_book_id")
                if not ref_id:
                    raise ValueError("reference_book_id missing in params_json")
                wizard = {
                    "intent": params.get("intent", ""),
                    "style_hint": params.get("style_hint", ""),
                    "target_volumes": params.get("target_volumes", 5),
                    "target_chapters_per_volume": params.get(
                        "target_chapters_per_volume", 30
                    ),
                }
                fr = await build_outline_from_reference(
                    reference_book_id=ref_id,
                    wizard_params=wizard,
                    db=db,
                    project_id=project_id,
                )
                if fr.get("status") != "ok":
                    raise RuntimeError(
                        "build_outline_from_reference failed: "
                        f"reason={fr.get('reason')} detail={fr.get('detail')}"
                    )
                ot = fr.get("outline_text") or ""
                collected.append(ot)
                # Persist sketch metadata so the polling endpoint can show
                # progress context to the UI without re-running the query.
                task.params_json = {
                    **(task.params_json or {}),
                    "sketch_line_count": fr.get("sketch_line_count"),
                    "reference_book": fr.get("reference_book"),
                }
                task.progress_text = ot
                task.progress_kind = 'candidate'
                task.char_count = len(ot)
                await db.commit()

            elif task.task_type == "outline_book":
                from app.services.chapter_target_words import build_planning_scale

                # 每章字数 / 每卷章数统一走 chapter_target_words(规范键与简写键
                # 双侧回退),避免只认规范键让简写键写的设置被静默忽略。
                book_scale = build_planning_scale(
                    project_target_word_count, project_settings
                )
                if book_scale and params.get("target_volumes") is not None:
                    requested_volumes = int(params["target_volumes"])
                    if requested_volumes <= 0:
                        raise ValueError("目标卷数必须为正数")
                    book_scale["n_volumes"] = requested_volumes
                    book_scale["chapters_per_volume"] = max(1, round(book_scale["n_chapters"] / requested_volumes))
                if book_scale and params.get("target_chapters_per_volume") is not None:
                    requested_chapters = int(params["target_chapters_per_volume"])
                    if requested_chapters <= 0:
                        raise ValueError("每卷目标章数必须为正数")
                    book_scale["chapters_per_volume"] = requested_chapters
                    book_scale["n_chapters"] = book_scale["n_volumes"] * requested_chapters
                async with aclosing(await generator.generate_book_outline(user_input=enhanced, stream=True, scale=book_scale)) as owned_stream:
                    async for chunk in owned_stream:
                        if isinstance(chunk, dict) and chunk.get("event") == "done":
                            full_outline = chunk.get("full_outline")
                            if isinstance(full_outline, str) and full_outline.strip():
                                collected.clear()
                                collected.append(full_outline)
                                # Staged generation strips control tags from text;
                                # its final event owns the parsed plan and sections.
                                book_outline_metadata = {
                                    key: chunk[key] for key in ("volume_plan", "structured")
                                    if key in chunk
                                }
                        else:
                            _append_chunk(chunk)
                        # UI progress is time-bounded; tiny tokens must not each incur a DB round trip.
                        await progress_writer.write(db, task, _joined_collected)

            elif task.task_type == "outline_volume":
                from app.services.volume_outline_writes import require_volume_task_source
                submission = params.get('submission')
                generated_volume_source = submission.get('volume_source') if isinstance(submission, dict) else None
                generated_volume_idx = params.get('volume_idx', 1)
                if generated_volume_source is not None:
                    generated_volume = await require_volume_task_source(db, task.project_id, params)
                    generated_volume_idx = generated_volume.volume_idx
                # Get book outline for context
                from sqlalchemy import select
                from app.services.outline_readiness import has_meaningful_outline_content
                result = await db.execute(
                    select(Outline)
                    .where(Outline.project_id == task.project_id, Outline.level == "book")
                    .order_by(Outline.is_confirmed.desc(), Outline.created_at.asc())
                )
                book_rows = list(result.scalars().all())
                book_ol = next(
                    (
                        row
                        for row in book_rows
                        if has_meaningful_outline_content(
                            getattr(row, "content_json", None)
                        )
                    ),
                    None,
                )
                if book_ol is None:
                    raise RuntimeError("outline_chain_incomplete: 缺少：全书大纲 (book)")
                book_data = book_ol.content_json if book_ol else {}

                async with aclosing(await generator.generate_volume_outline(book_outline=book_data, volume_idx=generated_volume_idx, user_notes=enhanced, stream=True)) as owned_stream:
                    async for chunk in owned_stream:
                        _append_chunk(chunk)
                        await progress_writer.write(db, task, _joined_collected)

            elif task.task_type == "outline_chapter":
                chapter_id = params.get("chapter_id")
                if not chapter_id:
                    raise ValueError("chapter_id missing for outline_chapter")

                from app.services.chapter_outline_expander import expand_chapter_outline
                from app.services.outline_readiness import build_outline_readiness_report

                readiness = await build_outline_readiness_report(
                    db,
                    project_id=project_id,
                    chapter_id=str(chapter_id),
                )
                upstream_missing = [
                    layer for layer in readiness.missing_layers if layer in {"book", "volume"}
                ]
                if upstream_missing:
                    missing = ",".join(upstream_missing)
                    raise RuntimeError(
                        f"outline_chain_incomplete: {readiness.block_message()} ({missing})"
                    )

                outline_json = await expand_chapter_outline(
                    project_id=project_id,
                    chapter_id=str(chapter_id),
                    db=db,
                )
                outline_text = json.dumps(outline_json, ensure_ascii=False, indent=2)
                collected.clear()
                collected.append(outline_text)
                task = await db.get(GenerationTask, task_id)
                if task is not None:
                    task.progress_text = outline_text
                    task.progress_kind = 'candidate'
                    task.char_count = len(outline_text)
                    await db.commit()

            elif task.task_type == "chapter":
                from app.models.project import Chapter
                ch = await db.get(Chapter, params.get("chapter_id", ""))
                if not ch:
                    raise ValueError("章节不存在")
                from app.services.scene_orchestrator import SceneOrchestrator
                from app.services.chapter_generator import ChapterGenerator

                generated_chapter = ch
                # Capture primitive identifiers immediately. Any later rollback
                # can expire the ORM object, and touching ch.id/ch.volume_id then
                # may trigger async lazy IO from a sync attribute accessor.
                generated_chapter_id_safe = str(ch.id)
                generated_chapter_volume_id_safe = str(ch.volume_id)
                generated_chapter_idx_safe = ch.chapter_idx
                generated_chapter_outline = ch.outline_json or {}
                generated_chapter_target_words = _resolve_task_chapter_target_words(
                    params, ch.target_word_count, project_settings
                )
                user_instr = (enhanced + ("\n\n[风格要求] " + style_text if style_text else "")).strip()
                _prose_preflight_prompt, _prose_preflight_meta = await _build_chinese_prose_preflight_prompt(ch, db)
                if _prose_preflight_prompt:
                    user_instr = (user_instr + _prose_preflight_prompt).strip()
                    try:
                        task.params_json = {
                            **(task.params_json or {}),
                            "chinese_prose_mechanics_preflight": _prose_preflight_meta,
                        }
                        await db.commit()
                    except Exception as preflight_meta_err:
                        logger.warning(
                            "chinese_prose_preflight metadata persist failed task_id=%s err=%s",
                            task_id,
                            preflight_meta_err,
                        )
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                generated_chapter_user_instr = user_instr
                import asyncio as _asyncio
                from app.config import settings as _settings
                # Scene-mode is preferred for quality, but operators must be able to
                # bypass the unstable JSON scene_planner when it repeatedly causes
                # running+0 chars. When force_direct_chapter/skip_scene_planner is set,
                # go straight to the single-shot prose generator and still run the
                # downstream evaluation/revise loop.
                _force_direct = bool(
                    params.get("force_direct_chapter")
                    or params.get("skip_scene_planner")
                    or _settings.FORCE_DIRECT_CHAPTER
                )
                last_scene_err: Exception | None = None
                # v1.12 L2: Hard timeout guard to prevent Celery tasks from
                # getting stuck in an unacked state when upstream LLM calls hang.
                # This timeout covers the whole scene-mode pipeline for a chapter.
                _scene_timeout_requested = float(params.get("scene_timeout_seconds") or 900)
                # The API may request a very long scene timeout, but a chapter task must not
                # sit at running+0 chars while planner/writer is stuck. Clamp by default and
                # fall back to single-shot prose if scene-mode cannot emit.
                _scene_timeout_cap = _settings.SCENE_MODE_TIMEOUT_HARD_CAP_SECONDS
                _scene_timeout = min(_scene_timeout_requested, _scene_timeout_cap)

                if _force_direct:
                    logger.warning(
                        "Async generation: force_direct_chapter enabled; bypassing scene planner task_id=%s",
                        task_id,
                    )
                    try:
                        task.progress_text = "force_direct_chapter: 正在跳过 scene_planner，直接生成完整正文。"
                        task.progress_kind = 'status'
                        task.char_count = 0
                        task.error_message = None
                        await db.commit()
                    except Exception as hb_err:
                        logger.warning("force_direct_chapter heartbeat failed task_id=%s err=%s", task_id, hb_err)
                    try:
                        fallback_timeout = float(
                            params.get("fallback_timeout_seconds")
                            or _settings.SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS
                        )
                        llm_timeout_kwargs = _single_shot_llm_timeout_kwargs(fallback_timeout)
                        fallback_instr = (
                            user_instr
                            + "\n\n【强制直出要求】本轮跳过 scene_planner/scene_writer。"
                            + "请以全书大纲→分卷大纲→章节大纲的层级来源链为唯一剧情来源生成完整小说正文；"
                            + "章节大纲就是本章全部剧情骨架，先在内部拆成 outline_execution_units / chapter_outline_unit_ledger / outline_beat_execution_ledger，"
                            + "并对每个执行单元完成 foreshadow_control_ledger、character_state_ledger、pacing_budget_ledger、evidence_permission_ledger、mechanism_boundary_ledger、inference_uncertainty_ledger、time_window_budget、spatial_feasibility_ledger、channel_occlusion_ledger、coincidence_friction_ledger、dialogue_density_ledger、anchor_audit_before_prose 与 micro_continuity_budget；"
                            + "同时执行 chinese_prose_mechanics：长短句结合，连续短句不超过四句；环境随人物视线/行动铺陈；基础动作使用看、走、停、拿、放、退、走到、站在、试探等朴素动词；禁止生造动宾、物理不通的方位动作、清单式环境说明、长段复述、动作切片和生僻动词堆砌；"
                            + "必须执行 story_bible_leakage_zero：隐藏世界不能通过广告、海报、新闻、路人闲聊或旁白一次性列出设定词；POV 不知道的血脉体系、执行者、等级、能力名、奥丁源头等专名不得提前命名，只能先写异常、误认、局部痕迹和人物反应；"
                            + "必须执行 setting_name_dialogue_zero：路人、新闻、店员、邻居、广告、海报和闲聊不能字正腔圆讨论核心世界观名词；超自然影响必须降维成封路、停电、绕路、物价、黑车、查得紧、上面、那帮人、那种事、清道等生活抱怨和代词；"
                            + "必须执行 directional_listing_zero：禁止左边/右边/东头/西头/前后导览式罗列，环境只抓一个与当前氛围或剧情冲突的核心反差点；"
                            + "必须执行 dialogue_topology_limit：连续含引号段落不得超过四段，连续纯短对白不得超过两段，每个场景紧贴问答不得超过三组；超过时用未答、抢白、误解、环境声、动作结果、证物变化或概括性侧写打断，不得把整章写成问答剧本；"
                            + "每个执行单元必须有大纲来源、目标、原因、可见行动、直接后果、承接状态、路径耗时、信息来源、证据接触权限、机制边界、替代解释、时间窗口、资源压力、人物反应、结果上限和未解尾巴；"
                            + "还必须为 unit_movement_budget、unit_resource_budget、unit_information_ladder、unit_expression_role、unit_result_delta_cap、evidence_permission_ledger、mechanism_boundary_ledger、inference_uncertainty_ledger、time_window_budget、spatial_feasibility_ledger、channel_occlusion_ledger、coincidence_friction_ledger、dialogue_density_ledger 逐项扣账；"
                            + "正文只能按执行单元顺序扩写、润色、动作化、场景化和补足因果支撑，"
                            + "不得新增大纲外关键剧情、钥匙/暗门/逃生捷径、强线索、强结论、章末硬奖励或提前兑现后续内容；钥匙/锁舌/门路/机关必须写触发条件、物理边界、失败可能和代价；伏笔必须标记 preserve/seed_only/weak_hint/partial_reveal/deferred_payoff/payoff，未获大纲授权不得 payoff；"
                            + "若缺少来源锚点、转移路径、观察窗口、反派失手动机、证据权限、机制边界、代价、信息阶梯、替代解释、时间预算或结果上限，必须按 no_budget_no_upgrade 降级为疑点、残片、半句、误差或待验痕迹；"
                            + "必须有开端、推进、结果与章末钩子；禁止输出说明、提纲或 JSON。"
                        )
                        await db.commit()  # Publish inputs before the detached context/model call.
                        fallback_text = await _asyncio.wait_for(
                            ChapterGenerator().generate(
                                project_id=project_id,
                                volume_id=generated_chapter_volume_id_safe,
                                chapter_idx=generated_chapter_idx_safe,
                                db=None,
                                chapter_id=generated_chapter_id_safe,
                                user_instruction=fallback_instr,
                                **llm_timeout_kwargs,
                            ),
                            timeout=fallback_timeout,
                        )
                        fallback_text = (fallback_text or "").strip()
                        if not fallback_text:
                            raise RuntimeError("force_direct_chapter_empty")
                        collected.clear()
                        collected.append(fallback_text)
                        task.progress_text = fallback_text
                        task.progress_kind = 'candidate'
                        task.char_count = len(fallback_text)
                        task.error_message = None
                        task_persisted_on_original_session = False
                        try:
                            await db.commit()
                            task_persisted_on_original_session = True
                        except Exception as persist_err:
                            # Long force_direct LLM calls can return valid prose, then
                            # the checked-out asyncpg connection may be closed when
                            # persisting progress_text/char_count. Do not convert that
                            # into a failed/0-char task; retry via a fresh session.
                            logger.warning(
                                "force_direct_chapter persist retry task_id=%s chars=%d err=%s",
                                task_id,
                                len(fallback_text),
                                persist_err,
                            )
                            try:
                                await db.rollback()
                            except Exception:
                                pass
                            async with session_factory() as persist_db:
                                persist_task = await persist_db.get(GenerationTask, task_id)
                                if persist_task is None:
                                    raise RuntimeError("force_direct_chapter_persist_task_missing") from persist_err
                                persist_task.progress_text = fallback_text
                                persist_task.progress_kind = 'candidate'
                                persist_task.result_text = fallback_text
                                persist_task.char_count = len(fallback_text)
                                persist_task.error_message = None
                                persist_task.status = "completed"
                                if generated_chapter_id_safe:
                                    persist_chapter = await persist_db.get(Chapter, generated_chapter_id_safe)
                                    if persist_chapter is None:
                                        raise RuntimeError("force_direct_chapter_persist_chapter_missing") from persist_err
                                    persist_chapter.content_text = fallback_text
                                    persist_chapter.word_count = len(fallback_text)
                                    persist_chapter.status = "needs_review"
                                await persist_db.commit()
                                if generated_chapter_id_safe:
                                    from app.services.chapter_finalize import finalize_chapter

                                    # 章后收尾走统一实现(唯一执行链):摘要 / 实体派发 /
                                    # 卷桥接 / 伏笔 / 事实检查 —— 三入口共用同一条链。
                                    await finalize_chapter(
                                        db=persist_db, chapter=persist_chapter,
                                        project_id=project_id,
                                        chapter_id=getattr(persist_chapter, "id", None),
                                        volume_id=getattr(persist_chapter, "volume_id", None),
                                        full_text=getattr(persist_chapter, "content_text", "") or "",
                                        caller="tasks.run_async_generation")
                            logger.warning(
                                "force_direct_chapter persisted via fresh session task_id=%s chapter_id=%s chars=%d",
                                task_id,
                                generated_chapter_id_safe,
                                len(fallback_text),
                            )
                            # The original long-lived session may still be bound to a
                            # dead asyncpg connection after a long LLM call. Return now
                            # after durable task+chapter persistence so downstream
                            # evaluation/revision does not reuse the stale session.
                            return
                        logger.warning(
                            "Async generation: force_direct_chapter produced prose task_id=%s chars=%d",
                            task_id,
                            len(fallback_text),
                        )
                    except Exception as force_err:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        task = await db.get(GenerationTask, task_id)
                        if task is not None:
                            task.status = "needs_repair"
                            task.error_message = task_generation_error(force_err)[:1500]
                            await db.commit()
                        return

                if not _force_direct:
                    for _attempt in (1, 2):
                        orchestrator = SceneOrchestrator()

                        async def _consume_scene_stream() -> None:
                            await db.commit()  # Publish inputs before the detached context/model call.
                            async with aclosing(orchestrator.orchestrate_chapter_stream(project_id=project_id, volume_id=generated_chapter_volume_id_safe, chapter_idx=generated_chapter_idx_safe, db=None, chapter_id=generated_chapter_id_safe, user_instruction=user_instr, target_words=generated_chapter_target_words)) as owned_stream:
                                async for chunk in owned_stream:
                                    _append_chunk(chunk)
                                    await progress_writer.write(db, task, _joined_collected)

                        try:
                            await _asyncio.wait_for(
                                _consume_scene_stream(),
                                timeout=_scene_timeout,
                            )
                            last_scene_err = None
                            break
                        except _asyncio.TimeoutError as scene_timeout_err:
                            last_scene_err = scene_timeout_err
                            logger.warning(
                                "Async generation: SceneOrchestrator timed out (attempt=%d/%d timeout=%.1fs); task_id=%s",
                                _attempt,
                                2,
                                _scene_timeout,
                                task_id,
                            )
                            if any(collected):
                                break
                            if _attempt == 1:
                                await _asyncio.sleep(2.0)
                                continue
                            break
                        except Exception as scene_err:
                            last_scene_err = scene_err
                            if any(collected):
                                break
                            from app.services.context_errors import RequiredContextUnavailable
                            if isinstance(scene_err, RequiredContextUnavailable):
                                raise scene_err
                            if _attempt == 1:
                                logger.warning(
                                    "Async generation: SceneOrchestrator failed (attempt=%d/%d); retrying task_id=%s err=%s",
                                    _attempt,
                                    2,
                                    task_id,
                                    scene_err,
                                )
                                await _asyncio.sleep(2.0)
                                continue
                            break
                if (not _force_direct) and last_scene_err is not None and any(collected):
                    # A failed partial generation is a recovery artifact, not
                    # the prefix of a fresh retry or an accepted chapter.
                    task.status = "needs_review"
                    task.result_text = _joined_collected()
                    task.progress_text = task.result_text
                    task.progress_kind = 'candidate'
                    task.char_count = len(task.result_text)
                    task.error_message = "本次正文生成中断，已保留收到的片段；内容可能不完整，请复核后再决定如何续写。"
                    task.params_json = {**(task.params_json or {}), "generation_stop_reason": "partial_generation_interrupted"}
                    await db.commit()
                    return
                if (not _force_direct) and last_scene_err is not None:
                    # Reliability gate: do not leave chapter generation at running+0 chars.
                    # If scene planner/writer cannot emit, fall back to the older single-shot
                    # generation path so the fixed loop can still produce prose, score it, and
                    # use the evaluation report for the next engineering iteration.
                    _root = str(last_scene_err) if last_scene_err is not None else "unknown"
                    logger.warning(
                        "Async generation: SceneOrchestrator failed; attempting single-shot fallback task_id=%s err=%s",
                        task_id,
                        last_scene_err,
                    )
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    try:
                        task = await db.get(GenerationTask, task_id)
                        if task is not None:
                            task.progress_text = "scene_mode fallback: 正在切换为单段正文直出，避免 0 字卡死。"
                            task.progress_kind = 'status'
                            task.char_count = 0
                            task.error_message = "scene_mode fallback running: " + _root[:900]
                            await db.commit()
                    except Exception as hb_err:
                        logger.warning("single-shot fallback heartbeat failed task_id=%s err=%s", task_id, hb_err)
                    try:
                        fallback_timeout = float(
                            params.get("fallback_timeout_seconds")
                            or _settings.SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS
                        )
                        llm_timeout_kwargs = _single_shot_llm_timeout_kwargs(fallback_timeout)
                        fallback_instr = (
                            user_instr
                            + "\n\n【兜底生成要求】scene_planner/scene_writer 未能稳定输出。"
                            + "请直接按本章大纲、上下文和连续性约束生成完整正文；"
                            + "必须有开端、推进、结果与章末钩子；禁止输出说明、提纲或 JSON。"
                        )
                        await db.commit()  # Publish inputs before the detached context/model call.
                        fallback_text = await _asyncio.wait_for(
                            ChapterGenerator().generate(
                                project_id=project_id,
                                volume_id=generated_chapter_volume_id_safe,
                                chapter_idx=generated_chapter_idx_safe,
                                db=None,
                                chapter_id=generated_chapter_id_safe,
                                user_instruction=fallback_instr,
                                **llm_timeout_kwargs,
                            ),
                            timeout=fallback_timeout,
                        )
                        fallback_text = (fallback_text or "").strip()
                        if not fallback_text:
                            raise RuntimeError("single_shot_fallback_empty")
                        collected.clear()
                        collected.append(fallback_text)
                        task = await db.get(GenerationTask, task_id)
                        if task is not None:
                            task.progress_text = fallback_text
                            task.progress_kind = 'candidate'
                            task.char_count = len(fallback_text)
                            task.error_message = None
                            await db.commit()
                        logger.warning(
                            "Async generation: single-shot fallback produced prose task_id=%s chars=%d",
                            task_id,
                            len(fallback_text),
                        )
                    except Exception as fallback_err:
                        logger.warning(
                            "Async generation: scene_mode and single-shot fallback both failed task_id=%s scene_err=%s fallback_err=%s",
                            task_id,
                            _root,
                            fallback_err,
                        )
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        task = await db.get(GenerationTask, task_id)
                        if task is not None:
                            task.status = "needs_repair"
                            task.error_message = (
                                "scene_mode and single-shot fallback blocked: "
                                + task_generation_error(fallback_err)[:900]
                                + "; scene_err="
                                + _root[:600]
                            )[:1500]
                            await db.commit()
                        return

            full_text = _joined_collected()
            if task.task_type == "chapter" and generated_chapter is not None and not full_text.strip():
                # Defensive final guard: a stream can technically complete without chunks.
                # Treat that as a generation failure and force the same direct prose path.
                logger.warning("Async generation: scene-mode completed with empty text; running direct fallback task_id=%s", task_id)
                fallback_timeout = float(
                    params.get("fallback_timeout_seconds")
                    or _settings.SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS
                )
                llm_timeout_kwargs = _single_shot_llm_timeout_kwargs(fallback_timeout)
                await db.commit()  # Publish inputs before the detached context/model call.
                fallback_text = await _asyncio.wait_for(
                    ChapterGenerator().generate(
                        project_id=project_id,
                        volume_id=generated_chapter_volume_id_safe,
                        chapter_idx=generated_chapter_idx_safe,
                        db=None,
                        chapter_id=generated_chapter_id_safe,
                        user_instruction=generated_chapter_user_instr
                        + "\n\n【兜底生成要求】请直接输出完整小说正文，禁止说明、提纲或 JSON。",
                        **llm_timeout_kwargs,
                    ),
                    timeout=fallback_timeout,
                )
                full_text = (fallback_text or "").strip()
                collected.clear()
                collected.append(full_text)
                task.progress_text = full_text
                task.progress_kind = 'candidate'
                task.char_count = len(full_text)
                await db.commit()

            # Post-process: strip Markdown + AI fluff
            import re as _re
            # Volume generators already validate their complete JSON. Keep
            # literal punctuation inside narrative fields intact for adoption.
            if task.task_type != 'outline_volume':
                full_text = _re.sub(r'\*\*([^*]+)\*\*', r'\1', full_text)  # **bold**
                full_text = _re.sub(r'\*([^*]+)\*', r'\1', full_text)  # *italic*
                full_text = _re.sub(r'^#{1,6}\s*', '', full_text, flags=_re.MULTILINE)  # # headers
                full_text = _re.sub(r'^---+\s*$', '', full_text, flags=_re.MULTILINE)  # --- hr
                full_text = _re.sub(r'^>\s*', '', full_text, flags=_re.MULTILINE)  # > blockquote
                full_text = _re.sub(r'`([^`]+)`', r'\1', full_text)  # `code`
            # Ordinary first-person speech and conditionals are story content.
            # Do not delete paragraphs or vary punctuation to simulate style;
            # editorial revision requires context and remains a separate step.

            # Auto evaluation + regeneration for project/background chapter generation.
            # This mirrors the SSE /api/generate auto_revise path so chapters created
            # from the project workflow no longer require manual intervention.
            if generated_chapter is not None and full_text:
                from app.models.project import Chapter as _ChapterModel
                from app.models.project import ChapterEvaluation, ChapterVersion, Volume
                from app.services.auto_revise import (
                    issues_to_revise_instruction,
                    merge_revise_into_user_instruction,
                    revise_spans,
                    should_revise,
                    targeted_revision_enabled,
                    RevisionContext,
                    DEFAULT_REVISE_THRESHOLD,
                    DEFAULT_MAX_REVISE_ROUNDS,
                )
                from app.services.chapter_evaluator import ChapterEvaluator
                from app.services.chapter_summarizer import summarize_and_save_chapter
                from app.services.scene_orchestrator import SceneOrchestrator
                from sqlalchemy import select as _sql_select, update as _sql_update

                threshold = float(params.get("revise_threshold") or DEFAULT_REVISE_THRESHOLD)
                max_rounds = int(params.get("max_revise_rounds") or DEFAULT_MAX_REVISE_ROUNDS)
                auto_revise_enabled = bool(params.get("auto_revise", True))
                if generated_chapter_id_safe is None:
                    raise RuntimeError("generated_chapter_id_missing")
                if generated_chapter_volume_id_safe is None or generated_chapter_idx_safe is None:
                    raise RuntimeError("generated_chapter_identity_incomplete")
                ch = await db.get(
                    _ChapterModel,
                    generated_chapter_id_safe,
                    populate_existing=True,
                )
                if ch is None:
                    raise RuntimeError("generated_chapter_missing")
                generated_chapter = ch
                chapter_id_for_eval = generated_chapter_id_safe
                volume_id_for_eval = generated_chapter_volume_id_safe
                chapter_idx_for_eval = generated_chapter_idx_safe
                from app.services.evaluation_neighbors import load_evaluation_neighbors
                neighbors = await load_evaluation_neighbors(db, chapter_id_for_eval, project_id)
                prev_context = neighbors.previous_context

                current_text = full_text
                revision_stop_reason = None
                revision_context_error = None
                final_eval = None
                from app.services.chapter_acceptance import ChapterAcceptance
                acceptance = ChapterAcceptance(required=True, threshold=threshold)

                from app.services.cognition_retrieval import load_candidate_context
                from app.services.context_errors import RequiredContextUnavailable

                # C2/F1: whole-book style statistics for evaluator adjudication.
                style_stats_text = ""
                try:
                    from app.services.style_stat import load_style_stats_text
                    style_stats_text = await load_style_stats_text(db, project_id)
                except Exception as _style_err:
                    logger.warning(
                        "Async generation: style stats load failed: %s", _style_err
                    )

                for round_idx in range(1, max_rounds + 2):
                    task.status = "evaluating"
                    task.progress_text = current_text
                    task.progress_kind = 'candidate'
                    task.char_count = len(current_text)
                    await db.commit()

                    try:
                        cognition_ledger_text = await load_candidate_context(project_id,
                            before_chapter_id=chapter_id_for_eval, chapter_outline=generated_chapter_outline,
                            chapter_text=current_text, previous_text=prev_context,
                            user_instruction=generated_chapter_user_instr, sessions=read_session_factory)
                    except RequiredContextUnavailable as context_error:
                        revision_context_error = context_error
                        revision_stop_reason = 'revision_context_unavailable'
                        break
                    evaluator = ChapterEvaluator()
                    # ChapterEvaluator owns one total deadline across Registry,
                    # retries and fallback, shared with manual/SSE callers.
                    import asyncio as _asyncio
                    eval_result = await evaluator.evaluate(
                        chapter_text=current_text,
                        project_id=project_id, chapter_id=chapter_id_for_eval,
                        established_facts=await load_established_facts_block(
                            db, project_id, chapter_id_for_eval),
                        chapter_outline=generated_chapter_outline,
                        previous_summary=prev_context,
                        following_context=neighbors.following_context,
                        style_profile=style_text,
                        user_instruction=generated_chapter_user_instr,
                        cognition_ledger_text=cognition_ledger_text,
                        style_stats_text=style_stats_text,
                        timeout_seconds=params.get("evaluation_timeout_seconds"),
                    )
                    final_eval = eval_result
                    acceptance.record(current_text, eval_result)
                    db.add(ChapterEvaluation(
                        chapter_id=chapter_id_for_eval,
                        plot_coherence=eval_result.plot_coherence,
                        character_consistency=eval_result.character_consistency,
                        style_adherence=eval_result.style_adherence,
                        narrative_pacing=eval_result.narrative_pacing,
                        foreshadow_handling=eval_result.foreshadow_handling,
                        overall=eval_result.overall,
                        issues_json=eval_result.issues,
                        source_json=getattr(eval_result, "chapter_source", None),
                    ))
                    await db.commit()

                    logger.info(
                        "Auto chapter evaluation: chapter_id=%s round=%d overall=%.2f threshold=%.2f issues=%d",
                        chapter_id_for_eval, round_idx, eval_result.overall, threshold, len(eval_result.issues),
                    )

                    if not should_revise(eval_result, threshold=threshold) or not auto_revise_enabled:
                        break
                    if round_idx > max_rounds:
                        logger.warning(
                            "Auto chapter evaluation did not pass after %d regenerate rounds: chapter_id=%s overall=%.2f threshold=%.2f issues=%s",
                            max_rounds, chapter_id_for_eval, eval_result.overall, threshold, eval_result.issues[:5],
                        )
                        break

                    task.status = "regenerating"
                    await db.commit()

                    # Q2 targeted span revision (QMAI rewriteTarget): locate
                    # issues via their evaluator `quote` and rewrite only
                    # paragraph-±1 spans, splicing back into the current text.
                    # On success the spliced text re-enters the scoring loop
                    # directly; otherwise preserve it for review without
                    # automatically widening into full-chapter regeneration.
                    if targeted_revision_enabled():
                        try:
                            cognition_ledger_text = await load_candidate_context(project_id,
                                before_chapter_id=chapter_id_for_eval, chapter_outline=generated_chapter_outline,
                                chapter_text=current_text, previous_text=prev_context,
                                user_instruction=generated_chapter_user_instr, sessions=read_session_factory)
                        except RequiredContextUnavailable as context_error:
                            revision_context_error = context_error
                            revision_stop_reason = 'revision_context_unavailable'
                            break
                        try:
                            span_result = await revise_spans(current_text, eval_result.issues, project_id=project_id, chapter_id=chapter_id_for_eval,
                                context=RevisionContext(user_instruction=generated_chapter_user_instr,
                                    chapter_outline=generated_chapter_outline, previous_context=prev_context,
                                    following_context=neighbors.following_context, style_profile=style_text,
                                    cognition_ledger=cognition_ledger_text))
                        except Exception as targeted_err:
                            span_result = None
                            logger.warning(
                                "Async generation: targeted revision failed; preserving candidate chapter_id=%s round=%d err=%s",
                                chapter_id_for_eval, round_idx, targeted_err,
                            )
                        if span_result is not None and span_result.spans_revised > 0:
                            logger.info(
                                "Async generation: targeted revision applied chapter_id=%s round=%d spans=%d unlocatable=%d",
                                chapter_id_for_eval, round_idx,
                                span_result.spans_revised, len(span_result.unlocatable_issues),
                            )
                            current_text = span_result.text
                            continue

                        revision_stop_reason = "targeted_revision_unapplied"
                        task.params_json = {
                            **(task.params_json or {}),
                            "revision_stop_reason": "targeted_revision_unapplied",
                        }
                        await db.commit()
                        break

                    revise_instr = issues_to_revise_instruction(eval_result, round_idx=round_idx)
                    merged_instruction = merge_revise_into_user_instruction(
                        generated_chapter_user_instr,
                        revise_instr,
                    )
                    regen_chunks: list[str] = []
                    import asyncio as _asyncio
                    last_regen_err: Exception | None = None
                    if _force_direct:
                        logger.warning(
                            "Async generation: force_direct_chapter enabled during auto-revise; bypassing scene planner task_id=%s round=%d",
                            task_id,
                            round_idx,
                        )
                        try:
                            fallback_timeout = float(
                                params.get("fallback_timeout_seconds")
                                or _settings.SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS
                            )
                            llm_timeout_kwargs = _single_shot_llm_timeout_kwargs(fallback_timeout)
                            force_revise_instr = (
                                merged_instruction
                                + "\n\n【强制修订直出要求】本轮 auto-revise 继续跳过 scene_planner/scene_writer。"
                                + "请直接输出修订后的完整小说正文，集中修复本轮评分问题；禁止输出说明、提纲或 JSON。"
                            )
                            await db.commit()  # Publish inputs before the detached context/model call.
                            force_revise_text = await _asyncio.wait_for(
                                ChapterGenerator().generate(
                                    project_id=project_id,
                                    volume_id=volume_id_for_eval,
                                    chapter_idx=chapter_idx_for_eval,
                                    db=None,
                                    chapter_id=chapter_id_for_eval,
                                    user_instruction=force_revise_instr,
                                    **llm_timeout_kwargs,
                                ),
                                timeout=fallback_timeout,
                            )
                            force_revise_text = (force_revise_text or "").strip()
                            if not force_revise_text:
                                raise RuntimeError("force_direct_chapter_revise_empty")
                            regen_chunks.append(force_revise_text)
                        except Exception as regen_force_err:
                            last_regen_err = regen_force_err
                    else:
                        for _attempt in (1, 2):
                            orchestrator = SceneOrchestrator()
                            try:
                                await db.commit()  # Publish inputs before the detached context/model call.
                                async with aclosing(orchestrator.orchestrate_chapter_stream(project_id=project_id, volume_id=volume_id_for_eval, chapter_idx=chapter_idx_for_eval, db=None, chapter_id=chapter_id_for_eval, user_instruction=merged_instruction, target_words=generated_chapter_target_words)) as owned_stream:
                                    async for chunk in owned_stream:
                                        regen_chunks.append(chunk)
                                        if len(regen_chunks) % 5 == 0:
                                            task.progress_text = "".join(regen_chunks)
                                            task.progress_kind = 'candidate'
                                            task.char_count = len(task.progress_text)
                                            await db.commit()
                                last_regen_err = None
                                break
                            except Exception as regen_scene_err:
                                last_regen_err = regen_scene_err
                                # Once prose has arrived, a second attempt cannot
                                # safely append into the same candidate buffer.
                                # Preserve the previous candidate for repair.
                                if any(regen_chunks):
                                    break
                                if isinstance(regen_scene_err, RequiredContextUnavailable):
                                    break
                                if _attempt == 1:
                                    logger.warning(
                                        "Async generation: regen SceneOrchestrator failed (attempt=%d/%d); retrying task_id=%s err=%s",
                                        _attempt,
                                        2,
                                        task_id,
                                        regen_scene_err,
                                    )
                                    await _asyncio.sleep(2.0)
                                    continue
                                break
                    if last_regen_err is not None:
                        # Quality gate: do NOT fall back to single-shot generator
                        # during auto-revise. Scene planning failures correlate
                        # strongly with world-logic violations (time/space/etc.).
                        # Surface a root-cause repair requirement instead of
                        # producing another low-quality sample.
                        logger.warning(
                            "Async generation: regen SceneOrchestrator failed; require root-cause repair task_id=%s err=%s",
                            task_id,
                            last_regen_err,
                        )
                        revision_context_error = last_regen_err if isinstance(last_regen_err, RequiredContextUnavailable) else None
                        revision_stop_reason = 'revision_context_unavailable' if revision_context_error else (
                            "revision_incomplete_response"
                            if isinstance(last_regen_err, ValueError)
                            and str(last_regen_err) == "provider_incomplete_response"
                            else "full_revision_failed"
                        )
                        break
                    regenerated = "".join(regen_chunks).strip()
                    if not regenerated:
                        logger.warning("Auto chapter regeneration returned empty text: chapter_id=%s round=%d", chapter_id_for_eval, round_idx)
                        revision_stop_reason = "full_revision_empty"
                        break
                    current_text = regenerated

                # 先净化(幂等)再进质量门:让"含英文话术的正文"不会拿到质量分。
                # 放在 stop 分支之前,是为了保持"落库前紧跟质量门"的既有结构
                # (tests/test_revision_stop_terminal.py 锁了这段生产位置)。
                current_text = _purify_generated_text(
                    current_text, task, task_id, chapter_id_for_eval)
                if revision_stop_reason is not None:
                    # Retain the exact pre-failure candidate only in this task.
                    # Do not run another rewrite, overwrite chapter content, or
                    # promote this unaccepted candidate into downstream memory.
                    task.params_json = {
                        **(task.params_json or {}),
                        "revision_stop_reason": revision_stop_reason,
                    }
                    task.status = "needs_review"
                    task.error_message = {
                        "targeted_revision_unapplied": "局部修订未能可靠落实，已保留修订前候选，请复核。",
                        "revision_incomplete_response": "模型未完整交付本轮改写，已保留修订前候选，请复核。",
                        "full_revision_failed": "本轮整章改写失败，已保留修订前候选，请复核。",
                        "full_revision_empty": "本轮整章改写未返回正文，已保留修订前候选，请复核。",
                        "revision_context_unavailable": "本轮认知材料暂不可用，已保留当前候选，请复核后重试。",
                    }[revision_stop_reason]
                    if revision_stop_reason == 'revision_context_unavailable':
                        task.error_message = task_generation_error(revision_context_error)
                    task.result_text = current_text
                    task.progress_text = current_text
                    task.progress_kind = 'candidate'
                    task.char_count = len(current_text)
                    await db.commit()
                    return

                full_text = current_text
                if full_text and not bool(params.get("skip_polish")):
                    try:
                        import asyncio as _asyncio_qg

                        quality_gate_timeout = float(
                            params.get("quality_gate_timeout_seconds")
                            or _settings.CHAPTER_QUALITY_GATE_TIMEOUT_SECONDS
                        )
                        quality_result = await _asyncio_qg.wait_for(
                            apply_chapter_quality_gate(
                                text=full_text,
                                db=db,
                                project_id=project_id,
                                chapter_id=chapter_id_for_eval,
                                skip_polish=False,
                                target_word_count=generated_chapter_target_words,
                            ),
                            timeout=quality_gate_timeout,
                        )
                        task.params_json = {
                            **(task.params_json or {}),
                            "quality_gate": quality_result.to_safe_metadata(),
                        }
                        if quality_result.status != "passed":
                            full_text = _stage_needs_review_chapter_text(
                                task,
                                ch,
                                _purify_generated_text(
                                    quality_result.final_text, task, task_id,
                                    chapter_id_for_eval),
                                error_message=(
                                    "quality_gate blocked: "
                                    f"{quality_result.warning_reason or 'blocked'}"
                                ),
                            )
                            db.add(ChapterVersion(
                                chapter_id=chapter_id_for_eval,
                                parent_id=None,
                                branch_name="main",
                                content_text=full_text,
                                content_diff="",
                                word_count=len(full_text),
                                is_active=0,
                                source="ai_generation",
                                metadata_json={
                                    "caller": "tasks.run_async_generation",
                                    "auto_evaluated": True,
                                    "revise_threshold": threshold,
                                    "final_overall": getattr(final_eval, "overall", None),
                                    "issue_count": len(getattr(final_eval, "issues", []) or []),
                                    "passed": False,
                                    "quality_gate": quality_result.to_safe_metadata(),
                                },
                            ))
                            await db.commit()
                            try:
                                from app.services.chapter_finalize import finalize_chapter

                                # 章后收尾走统一实现(唯一执行链)
                                await finalize_chapter(
                                    db=db, chapter=ch, project_id=project_id,
                                    chapter_id=chapter_id_for_eval, volume_id=volume_id_for_eval,
                                    full_text=full_text, caller="tasks.run_async_generation")
                            except Exception as sum_err:
                                logger.warning("Chapter finalize failed after quality-gate review save: %s", sum_err)
                            return
                        if quality_result.rewrite_rounds > 0:
                            logger.info(
                                "Async generation quality gate applied task_id=%s chapter_id=%s status=%s rounds=%d",
                                task_id,
                                chapter_id_for_eval,
                                quality_result.status,
                                quality_result.rewrite_rounds,
                            )
                        if quality_result.final_text != full_text:
                            full_text = quality_result.final_text
                    except Exception as quality_err:
                        logger.warning(
                            "Async generation quality gate failed; blocking chapter save task_id=%s chapter_id=%s err=%s",
                            task_id,
                            chapter_id_for_eval,
                            quality_err,
                        )
                        full_text = _stage_needs_review_chapter_text(
                            task,
                            ch,
                            full_text,
                            error_message=(
                                "quality_gate blocked: "
                                f"{type(quality_err).__name__}"
                            ),
                        )
                        db.add(ChapterVersion(
                            chapter_id=chapter_id_for_eval,
                            parent_id=None,
                            branch_name="main",
                            content_text=full_text,
                            content_diff="",
                            word_count=len(full_text),
                            is_active=0,
                            source="ai_generation",
                            metadata_json={
                                "caller": "tasks.run_async_generation",
                                "auto_evaluated": True,
                                "revise_threshold": threshold,
                                "final_overall": getattr(final_eval, "overall", None),
                                "issue_count": len(getattr(final_eval, "issues", []) or []),
                                "passed": False,
                                "quality_gate": {"error": type(quality_err).__name__},
                            },
                        ))
                        await db.commit()
                        try:
                            from app.services.chapter_finalize import finalize_chapter

                            # 章后收尾走统一实现(唯一执行链)
                            await finalize_chapter(
                                db=db, chapter=ch, project_id=project_id,
                                chapter_id=chapter_id_for_eval, volume_id=volume_id_for_eval,
                                full_text=full_text, caller="tasks.run_async_generation")
                        except Exception as sum_err:
                            logger.warning("Chapter finalize failed after quality-gate error save: %s", sum_err)
                        return

                try:
                    from app.services.fact_contract import (
                        build_fact_contract,
                        validate_text_against_fact_contract,
                    )

                    fact_contract = await build_fact_contract(db, project_id)
                    fact_report = validate_text_against_fact_contract(full_text, fact_contract)
                    task.params_json = {
                        **(task.params_json or {}),
                        "fact_contract_gate": fact_report.to_dict(),
                    }
                    if not fact_report.ok:
                        full_text = _stage_needs_review_chapter_text(
                            task,
                            ch,
                            full_text,
                            error_message="fact_contract_gate blocked: role/entity drift",
                        )
                        db.add(ChapterVersion(
                            chapter_id=chapter_id_for_eval,
                            parent_id=None,
                            branch_name="main",
                            content_text=full_text,
                            content_diff="",
                            word_count=len(full_text),
                            is_active=0,
                            source="ai_generation",
                            metadata_json={
                                "caller": "tasks.run_async_generation",
                                "auto_evaluated": True,
                                "revise_threshold": threshold,
                                "final_overall": getattr(final_eval, "overall", None),
                                "issue_count": len(getattr(final_eval, "issues", []) or []),
                                "passed": False,
                                "quality_gate": (task.params_json or {}).get("quality_gate") or {},
                                "fact_contract_gate": fact_report.to_dict(),
                            },
                        ))
                        await db.commit()
                        try:
                            from app.services.chapter_finalize import finalize_chapter

                            # 章后收尾走统一实现(唯一执行链)
                            await finalize_chapter(
                                db=db, chapter=ch, project_id=project_id,
                                chapter_id=chapter_id_for_eval, volume_id=volume_id_for_eval,
                                full_text=full_text, caller="tasks.run_async_generation")
                        except Exception as sum_err:
                            logger.warning("Chapter finalize failed after fact-contract review save: %s", sum_err)
                        return
                except Exception as fact_err:
                    logger.warning(
                        "Fact contract gate failed; blocking chapter save task_id=%s chapter_id=%s err=%s",
                        task_id,
                        chapter_id_for_eval,
                        fact_err,
                    )
                    full_text = _stage_needs_review_chapter_text(
                        task,
                        ch,
                        full_text,
                        error_message=f"fact_contract_gate blocked: {type(fact_err).__name__}",
                    )
                    db.add(ChapterVersion(
                        chapter_id=chapter_id_for_eval,
                        parent_id=None,
                        branch_name="main",
                        content_text=full_text,
                        content_diff="",
                        word_count=len(full_text),
                        is_active=0,
                        source="ai_generation",
                        metadata_json={
                            "caller": "tasks.run_async_generation",
                            "auto_evaluated": True,
                            "revise_threshold": threshold,
                            "final_overall": getattr(final_eval, "overall", None),
                            "issue_count": len(getattr(final_eval, "issues", []) or []),
                            "passed": False,
                            "quality_gate": (task.params_json or {}).get("quality_gate") or {},
                            "fact_contract_gate": {"error": type(fact_err).__name__},
                        },
                    ))
                    await db.commit()
                    try:
                        from app.services.chapter_finalize import finalize_chapter

                        # 章后收尾走统一实现(唯一执行链)
                        await finalize_chapter(
                            db=db, chapter=ch, project_id=project_id,
                            chapter_id=chapter_id_for_eval, volume_id=volume_id_for_eval,
                            full_text=full_text, caller="tasks.run_async_generation")
                    except Exception as sum_err:
                        logger.warning("Chapter finalize failed after fact-contract error save: %s", sum_err)
                    return

                ch.content_text = full_text
                ch.word_count = len(full_text)
                passed = acceptance.passed(full_text)
                ch.status = "completed" if passed else "needs_review"
                quality_gate_summary = (task.params_json or {}).get("quality_gate")
                fact_contract_summary = (task.params_json or {}).get("fact_contract_gate")
                if passed:
                    await db.execute(
                        _sql_update(ChapterVersion)
                        .where(ChapterVersion.chapter_id == chapter_id_for_eval, ChapterVersion.is_active == 1)
                        .values(is_active=0)
                    )
                db.add(ChapterVersion(
                    chapter_id=chapter_id_for_eval,
                    parent_id=None,
                    branch_name="main",
                    content_text=full_text,
                    content_diff="",
                    word_count=len(full_text),
                    is_active=1 if passed else 0,
                    source="ai_generation",
                    metadata_json={
                        "caller": "tasks.run_async_generation",
                        "auto_evaluated": True,
                        "revise_threshold": threshold,
                        "final_overall": getattr(final_eval, "overall", None),
                        "issue_count": len(getattr(final_eval, "issues", []) or []),
                        "passed": passed,
                        "quality_gate": quality_gate_summary or {},
                        "fact_contract_gate": fact_contract_summary or {},
                    },
                ))
                await db.commit()
                # 2026-11-20 与桌面 SSE 路径对齐:此前只有 api/generate.py 做
                # "本章计划的伏笔是否在正文里留下铺垫"的检查,后台任务生成的书
                # 从来不做 —— 同一条管线因入口不同给出不同告警。仅告警:不阻断、
                # 不改状态、不改正文。
                try:
                    from app.services.foreshadow_setup_check import (
                        check_chapter_foreshadow_setup,
                        setup_event_payload,
                    )

                    _fs_report = await check_chapter_foreshadow_setup(
                        db, project_id=project_id, chapter_id=chapter_id_for_eval)
                    _fs_payload = setup_event_payload(_fs_report)
                    if _fs_payload:
                        task.params_json = {**(task.params_json or {}),
                                            "foreshadow_setup_missing": _fs_payload}
                        logger.warning(
                            "foreshadow_setup_missing task_id=%s chapter_id=%s "
                            "planned=%s missing=%s",
                            task_id, chapter_id_for_eval, _fs_payload.get("planned"),
                            len(_fs_payload.get("missing") or []),
                        )
                except Exception as fs_err:  # noqa: BLE001 - 检查失败不影响交付
                    logger.warning("foreshadow setup check failed task_id=%s err=%s",
                                   task_id, fs_err)
                # 事实/经济一致性(确定性兜底,与评审的自洽性判据互补)。
                # 评审看整章语义,这里只抓能机械判定的:金额与已确立数额不符、
                # 期限未到就处置该笔钱。仅告警并记录,不阻断交付。
                try:
                    from app.services.established_facts import (
                        build_established_facts,
                        check_chapter_against_facts,
                    )

                    _facts = await build_established_facts(db, project_id, chapter_id_for_eval)
                    if _facts:
                        _findings = check_chapter_against_facts(full_text, _facts.lines)
                        if _findings:
                            task.params_json = {**(task.params_json or {}),
                                                "fact_consistency": _findings[:5]}
                            logger.warning(
                                "fact_consistency_warning task_id=%s chapter_id=%s kinds=%s",
                                task_id, chapter_id_for_eval,
                                sorted({f.get("kind") for f in _findings}),
                            )
                except Exception as facts_err:  # noqa: BLE001
                    logger.warning("fact consistency check failed task_id=%s err=%s",
                                   task_id, facts_err)
                # B2' (audit 2026-07-26): mirror the SSE path — enqueue Neo4j
                # entity extraction after every chapter save. Without this,
                # projects generated via the background task queue got no
                # states/locations/relationships at all.
                # 章后收尾走**统一实现**(唯一执行链):摘要 / 实体派发 / 卷桥接 /
                # 伏笔缺铺垫 / 事实一致性 —— 三入口(SSE / 队列 / 探针)共用同一条链,
                # 见 app/services/chapter_finalize.py。
                try:
                    from app.services.chapter_finalize import finalize_chapter

                    await finalize_chapter(
                        db=db, chapter=ch, project_id=project_id,
                        chapter_id=chapter_id_for_eval, volume_id=volume_id_for_eval,
                        full_text=full_text, caller="tasks.run_async_generation")
                except Exception as sum_err:
                    logger.warning("Chapter finalize failed after evaluation: %s", sum_err)
                # Cognition ledger ingestion on every FINAL save (audit
                # 2026-07-26). Q3 v1.9.1 gated this on `passed`, but that was
                # lossy: a needs_review final save still persists full_text as
                # the chapter's canonical content_text (readable, exportable,
                # summarized, entity-extracted, volume-backfilled), and a later
                # manual accept never re-ingests — accepted chapters silently
                # missed the ledger. The SSE reference path (api/generate.py)
                # ingests the settled text regardless of evaluation score and
                # withholds only aborted/blocked drafts; align with it. The
                # quality-gate / fact-contract blocked branches above return
                # before this point, so blocked drafts are still never
                # ingested. Never blocks chapter persistence.
                try:
                    from app.services.character_cognition import extract_and_update
                    await extract_and_update(db, project_id, full_text, chapter_id=chapter_id_for_eval)
                except Exception as cog_err:
                    logger.warning("Cognition ledger update failed after evaluation: %s", cog_err)
                # C2/F1: recompute whole-book style stats in the background
                # (deterministic, non-blocking, celery-optional).
                try:
                    from app.services.style_stat import dispatch_style_recompute
                    dispatch_style_recompute(project_id, caller="knowledge_tasks.chapter")
                except Exception as style_err:
                    logger.warning("Style stats dispatch failed: %s", style_err)

            task.result_text = full_text
            if task.task_type == 'outline_volume' and generated_volume_source is not None:
                from app.services.volume_outline_writes import text_digest
                await require_volume_task_source(db, task.project_id, params)
                task.params_json = {**(task.params_json or {}), 'volume_candidate': {
                    'version': 1, 'target': generated_volume_source, 'text_hash': text_digest(full_text),
                }}

            # A polish is an optional, separately reviewed candidate, never
            # a replacement for the evaluated/saved source chapter.
            task.polished_text = ""
            if params.get("enable_polish"):
                task.status = "polishing"
                task.progress_text = full_text
                task.progress_kind = 'candidate'
                await db.commit()
                from app.services.polish_candidate import create_polish_candidate
                try:
                    polish = await create_polish_candidate(
                        full_text, instruction=generated_chapter_user_instr,
                        llm_call=router.generate, project_id=project_id,
                        chapter_id=generated_chapter_id_safe,
                    )
                    task.polished_text = polish.text
                    task.params_json = {**(task.params_json or {}), "polish_candidate": polish.metadata()}
                except Exception as pe:
                    logger.warning("Polish candidate failed: %s", type(pe).__name__)
                    task.params_json = {
                        **(task.params_json or {}),
                        "polish_candidate": {"status": "failed", "reason": type(pe).__name__},
                    }

            task.progress_text = full_text
            task.progress_kind = 'candidate'
            task.char_count = len(full_text)
            task.status = "completed" if (not generated_chapter or passed) else "needs_review"

            # Auto-save outline to outlines table
            if task.task_type.startswith("outline") and full_text and project_id:
                # Explicit task_type -> outline level mapping. `outline_from_reference`
                # is semantically a book-level outline (built from a reference book),
                # NOT a separate "from_reference" level. The outline_volume branch is
                # intentionally a no-op here: real volume outlines are written by
                # /volumes/{id}/regenerate which sets parent_id and dedupes per
                # volume_idx; auto-saving here would produce orphan rows that no query
                # path can find (volumes.py requires parent_id == book_outline_id).
                _outline_level_map = {
                    "outline_from_reference": "book",
                    "outline_book": "book",
                }
                outline_level = _outline_level_map.get(task.task_type)
                if outline_level == "book":
                    from app.services.chapter_order import lock_chapter_structure, reindex_chapter_structure
                    await lock_chapter_structure(db, project_id)
                    # Upsert: enforce one-book-per-project invariant. Migration
                    # a1001500 adds a partial UNIQUE index on outlines(project_id)
                    # WHERE level='book'; this DELETE+INSERT keeps the index happy
                    # and replaces any prior book outline atomically within this txn.
                    from sqlalchemy import delete as _sql_delete
                    await db.execute(
                        _sql_delete(Outline).where(
                            Outline.project_id == project_id,
                            Outline.level == "book",
                        )
                    )
                    # PR-OL2: extract <volume-plan> JSON block and persist it
                    # alongside raw_text so the wizard can prefill volume count
                    # without re-running the SSE preview.
                    _vol_plan = None
                    # PR-FIX-OL15-CELERY-STRIP: the SSE save path in
                    # backend/app/api/generate.py:945-949 already calls
                    # _OG._strip_volume_plan_tags(full_text) before persisting,
                    # but this celery branch (used by outline_book /
                    # outline_from_reference task_types) was writing the raw LLM
                    # output verbatim, which leaks <volume-plan>...</volume-plan>
                    # control tags into user-visible raw_text. Strip them here
                    # too. Both _extract_volume_plan (instance method) and
                    # _strip_volume_plan_tags (staticmethod) live on the same
                    # _OG import, so we do both inside the same try.
                    full_text_clean = full_text
                    try:
                        from app.services.outline_generator import OutlineGenerator as _OG
                        _vol_plan = _OG()._extract_volume_plan(full_text)
                        full_text_clean = _OG._strip_volume_plan_tags(full_text)
                    except Exception as _vp_err:
                        logger.warning("volume_plan extract/strip failed: %s", _vp_err)
                    if isinstance(book_outline_metadata.get("volume_plan"), list):
                        _vol_plan = book_outline_metadata["volume_plan"]
                    _content = {"raw_text": full_text_clean}
                    if isinstance(book_outline_metadata.get("structured"), dict):
                        _content["structured"] = book_outline_metadata["structured"]
                    if _vol_plan:
                        _content["volume_plan"] = _vol_plan
                    from app.services.outline_consistency_gate import validate_outline_consistency
                    _consistency = validate_outline_consistency(_content, level="book")
                    _content["_consistency_report"] = _consistency.to_dict()
                    db.add(
                        Outline(
                            project_id=project_id,
                            level="book",
                            content_json=_content,
                        )
                    )
                    # PR-OL2: also create empty Volume rows from the plan so
                    # the wizard step 2 has a real skeleton to fill in. Skip
                    # idx that already exists (idempotent on retry).
                    if _vol_plan:
                        from sqlalchemy import select as _sql_select
                        from app.models.project import Volume as _Volume
                        existing = await db.execute(
                            _sql_select(_Volume.volume_idx).where(
                                _Volume.project_id == project_id
                            )
                        )
                        existing_idx = {r[0] for r in existing.all()}
                        from app.services.budget_allocator import allocate_weighted, VOLUME_DEFAULT
                        if project_target_word_count and int(project_target_word_count) > 0:
                            volume_targets = allocate_weighted(
                                int(project_target_word_count),
                                [int(v.get("est_chapters") or 1) for v in _vol_plan],
                            )
                        else:
                            volume_targets = [VOLUME_DEFAULT] * len(_vol_plan)
                        for _v, volume_target in zip(_vol_plan, volume_targets):
                            _idx = int(_v.get("idx") or 0)
                            if _idx <= 0 or _idx in existing_idx:
                                continue
                            db.add(_Volume(
                                project_id=project_id,
                                volume_idx=_idx,
                                title=str(_v.get("title") or f"第{_idx}卷"),
                                summary=str(_v.get("theme") or ""),
                                target_word_count=volume_target,
                            ))
                        logger.info(
                            "PR-OL2: planned %d volumes from <volume-plan> for project=%s",
                            len(_vol_plan), project_id,
                        )
                        await db.flush()
                        await reindex_chapter_structure(db, project_id)
                else:
                    logger.info(
                        "Skipping auto-save for task_type=%s (handled by dedicated endpoint)",
                        task.task_type,
                    )

            await db.commit()
            logger.info("Async generation complete: %s, %d chars", task.task_type, len(full_text))

        except Exception as e:
            # v4.13 reliability hardening: long direct-generation LLM calls can
            # produce valid prose and then leave the original async session bound
            # to a closed/stale connection. If that later raises MissingGreenlet,
            # asyncpg "connection is closed", or another commit-time persistence
            # error, do not mark a non-empty generated draft as failed. First try
            # to salvage the already-produced text through a brand-new session.
            try:
                _salvage_text = (locals().get("full_text") or _joined_collected() or "").strip()
            except Exception:
                _salvage_text = ""
            try:
                _salvage_chapter_id = locals().get("generated_chapter_id_safe")
                if not _salvage_chapter_id:
                    _salvage_chapter = locals().get("generated_chapter")
                    _salvage_chapter_id = getattr(_salvage_chapter, "id", None)
            except Exception:
                _salvage_chapter_id = None
            if _salvage_text:
                try:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    async with session_factory() as db2:
                        task2 = await db2.get(GenerationTask, task_id)
                        if task2:
                            task2.progress_text = _salvage_text
                            task2.progress_kind = 'candidate'
                            task2.result_text = _salvage_text
                            task2.char_count = len(_salvage_text)
                            task2.polished_text = task2.polished_text or ""
                            # Saved fragments are recovery material, not proof that
                            # all outline stages and canonical persistence succeeded.
                            task2.status = "needs_review" if _salvage_chapter_id else "failed"
                            task2.error_message = (
                                None if _salvage_chapter_id else
                                "生成或保存中断，已保留返回内容供检查；任务未完成，请检查后重试。"
                            )
                            from app.services.context_errors import RequiredContextUnavailable
                            if isinstance(e, RequiredContextUnavailable):
                                task2.error_message = task_generation_error(e)
                        ch2 = None
                        if _salvage_chapter_id:
                            ch2 = await db2.get(Chapter, _salvage_chapter_id)
                            if ch2:
                                ch2.content_text = _salvage_text
                                ch2.word_count = len(_salvage_text)
                                ch2.status = "needs_review"
                        await db2.commit()
                        if ch2 is not None:
                            from app.services.chapter_finalize import finalize_chapter

                            # 章后收尾走统一实现(唯一执行链)
                            await finalize_chapter(
                                db=db2, chapter=ch2, project_id=project_id,
                                chapter_id=getattr(ch2, "id", None),
                                volume_id=getattr(ch2, "volume_id", None),
                                full_text=getattr(ch2, "content_text", "") or "",
                                caller="tasks.run_async_generation")
                    logger.warning(
                        "Async generation salvaged produced prose after persistence/session error task_id=%s chars=%d err=%s",
                        task_id,
                        len(_salvage_text),
                        e,
                    )
                    return
                except Exception as salvage_err:
                    logger.error("Async generation salvage commit failed; falling back to failed status: %s", salvage_err)

            # 2026-11-17: 瞬时基础设施故障(断连/超时/OperationalError)不要写成终态
            # failed —— 上层 `_run_guarded_generation` 会把它保留为 pending,交给重投
            # 与恢复扫描重试。此前这个 except 吞掉一切并 return,导致那条重试分支
            # 对"生成中途断连"完全无效(十几分钟的章节被永久判死)。
            if any(isinstance(e, kind) for kind in _transient_errors()):
                logger.warning(
                    "Async generation transient failure (%s); leaving task pending for retry: %s",
                    type(e).__name__, task_id,
                )
                raise

            # v1.12 L5: be defensive about DB connection drops inside long
            # running Celery tasks. If no generated text is available to salvage,
            # persist failure using a fresh session so the UI does not remain
            # stuck in running.
            try:
                task.status = "failed"
                task.error_message = task_generation_error(e)[:500]
                await db.commit()
            except Exception as commit_err:
                logger.warning("Async generation failure commit failed; retry with fresh session: %s", commit_err)
                try:
                    await db.rollback()
                except Exception:
                    pass
                try:
                    async with session_factory() as db2:
                        task2 = await db2.get(GenerationTask, task_id)
                        if task2:
                            task2.status = "failed"
                            task2.error_message = task_generation_error(e)[:500]
                            await db2.commit()
                except Exception as commit_err2:
                    logger.error("Async generation failure commit (fresh session) also failed: %s", commit_err2)
            logger.exception("Async generation failed: %s", task_id)


@celery_app.task(name="tasks.run_pipeline_generation")
def run_pipeline_generation(pipeline_id: str, command_id: str | None = None):
    """Execute a persisted command; legacy tokenless messages cannot write."""
    return _run_async(_run_pipeline_async(pipeline_id, command_id))


async def _run_pipeline_async(pipeline_id: str, command_id: str | None = None):
    if command_id is None:
        return False  # Beat recovers abandoned legacy rows with a fresh token.
    from app.db import session
    from app.services.production.runtime import run_command
    return await run_command(session.engine, session.async_session_factory, pipeline_id, command_id)


@celery_app.task(name="tasks.dispatch_pipeline_commands")
def dispatch_pipeline_commands():
    from app.services.production.dispatch import dispatch_queued_commands
    return _run_async(dispatch_queued_commands())


@celery_app.task(name='tasks.run_generation_batch', acks_late=False)
def run_generation_batch(project_id: str, request_id: str):
    return _run_async(_run_batch_async(project_id, request_id))


async def _run_batch_async(project_id: str, request_id: str):
    from app.db.session import async_session_factory
    from app.services.batch_generator import BatchGenerator
    async with async_session_factory() as db:
        job = await BatchGenerator().run_submitted(project_id, request_id, db=db, recover=True)
        return {'request_id': job.job_id, 'status': job.status.value, 'completed': job.completed_chapters}


@celery_app.task(name='tasks.dispatch_generation_batches')
def dispatch_generation_batches():
    from app.services.batch_dispatch import dispatch_pending_batches
    return _run_async(dispatch_pending_batches())


@celery_app.task(name='tasks.dispatch_generation_tasks')
def dispatch_generation_tasks():
    from app.services.generation_task_dispatch import dispatch_pending_tasks
    return _run_async(dispatch_pending_tasks())
