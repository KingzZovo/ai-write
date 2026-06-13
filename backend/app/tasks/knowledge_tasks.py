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
    import os as _os

    configured = float(_os.getenv("SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS", "840"))
    retry_attempts = int(_os.getenv("SINGLE_SHOT_LLM_RETRY_ATTEMPTS", "1"))
    stream_raw = str(_os.getenv("SINGLE_SHOT_LLM_STREAM", "0")).strip().lower()
    use_stream = stream_raw in {"1", "true", "yes", "on"}
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
        assumed_endpoints = max(1, int(_os.getenv("SINGLE_SHOT_LLM_BUDGET_ENDPOINTS", "2")))
    else:
        assumed_endpoints = max(1, int(_os.getenv("SINGLE_SHOT_LLM_BUDGET_ENDPOINTS", "1")))
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


@celery_app.task(name="tasks.vectorize_book")
def vectorize_book_task(book_id: str):
    """Vectorize all chunks of an existing book into Qdrant."""
    _run_async(_vectorize_book_async(book_id))


async def _vectorize_book_async(book_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import TextChunk, ReferenceBook
    from app.services.model_router import get_model_router_async
    from app.services.qdrant_store import QdrantStore
    from qdrant_client import AsyncQdrantClient
    from app.config import settings

    router = await get_model_router_async()
    qc = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    store = QdrantStore(qc)
    await store.ensure_collections()

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            logger.error("Book %s not found", book_id)
            return

        result = await db.execute(
            select(TextChunk)
            .where(TextChunk.book_id == book_id)
            .order_by(TextChunk.sequence_id)
        )
        chunks = list(result.scalars().all())
        logger.info("Vectorizing %d chunks for %s", len(chunks), book.title)

        vectorized = 0
        for chunk in chunks:
            try:
                embedding = await router.embed(chunk.content[:500])
                if embedding and any(v != 0 for v in embedding[:10]):
                    await store.store_style_features(
                        book_id=book_id,
                        chunk_id=str(chunk.id),
                        sequence_id=chunk.sequence_id,
                        features_dict={
                            "chapter_title": chunk.chapter_title or "",
                            "char_count": chunk.char_count,
                        },
                        embedding=embedding,
                    )
                    vectorized += 1
            except Exception as e:
                logger.debug("Chunk vectorization failed: %s", e)

            # Log progress every 100 chunks
            if vectorized > 0 and vectorized % 100 == 0:
                logger.info("  %d/%d vectorized...", vectorized, len(chunks))

        logger.info("Vectorization complete: %d/%d chunks for %s", vectorized, len(chunks), book.title)

    await qc.close()


@celery_app.task(
    name="tasks.run_async_generation",
    # v1.12 L4: This task can run for a long time and previously got stuck in
    # Redis "unacked" state (acked-late) when a worker subprocess stalled.
    # Ack early so a stalled worker won't permanently wedge the message.
    acks_late=False,
)
def run_async_generation(task_id: str):
    """Run outline/chapter generation in background with progress tracking."""
    _run_async(_run_async_generation_impl(task_id))


async def _run_async_generation_impl(task_id: str):
    from app.models.generation_task import GenerationTask
    from app.models.project import Outline
    from app.services.model_router import get_model_router_async
    from app.services.outline_generator import OutlineGenerator

    router = await get_model_router_async()
    session_factory = _make_session()

    async with session_factory() as db:
        task = await db.get(GenerationTask, task_id)
        if not task:
            return

        task.status = "running"
        await db.commit()

        params = task.params_json or {}
        user_input = params.get("user_input", "")
        project_id = str(task.project_id)
        project_settings = {}
        try:
            from app.models.project import Project as _Project
            project = await db.get(_Project, project_id)
            if project and isinstance(project.settings_json, dict):
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
                from app.services.style_compiler import compile_style
                profile = await db.get(StyleProfile, style_id)
                if profile:
                    style_text = compile_style(profile)
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

            # Generate based on task_type
            generator = OutlineGenerator()
            collected = []

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
                task.char_count = len(ot)
                await db.commit()

            elif task.task_type == "outline_book":
                async for chunk in await generator.generate_book_outline(
                    user_input=enhanced, stream=True
                ):
                    _append_chunk(chunk)
                    # Update progress every 20 chunks
                    if len(collected) % 5 == 0:
                        task.progress_text = _joined_collected()
                        task.char_count = len(task.progress_text)
                        await db.commit()

            elif task.task_type == "outline_volume":
                # Get book outline for context
                from sqlalchemy import select
                from app.services.outline_readiness import has_meaningful_outline_content
                result = await db.execute(
                    select(Outline)
                    .where(Outline.project_id == project_id, Outline.level == "book")
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

                async for chunk in await generator.generate_volume_outline(
                    book_outline=book_data,
                    volume_idx=params.get("volume_idx", 1),
                    user_notes=enhanced, stream=True
                ):
                    _append_chunk(chunk)
                    if len(collected) % 5 == 0:
                        task.progress_text = _joined_collected()
                        task.char_count = len(task.progress_text)
                        await db.commit()

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
                import os as _os
                # Scene-mode is preferred for quality, but operators must be able to
                # bypass the unstable JSON scene_planner when it repeatedly causes
                # running+0 chars. When force_direct_chapter/skip_scene_planner is set,
                # go straight to the single-shot prose generator and still run the
                # downstream evaluation/revise loop.
                _force_direct = bool(
                    params.get("force_direct_chapter")
                    or params.get("skip_scene_planner")
                    or _os.getenv("FORCE_DIRECT_CHAPTER", "0") == "1"
                )
                last_scene_err: Exception | None = None
                # v1.12 L2: Hard timeout guard to prevent Celery tasks from
                # getting stuck in an unacked state when upstream LLM calls hang.
                # This timeout covers the whole scene-mode pipeline for a chapter.
                _scene_timeout_requested = float(params.get("scene_timeout_seconds") or 900)
                # The API may request a very long scene timeout, but a chapter task must not
                # sit at running+0 chars while planner/writer is stuck. Clamp by default and
                # fall back to single-shot prose if scene-mode cannot emit.
                _scene_timeout_cap = float(_os.getenv("SCENE_MODE_TIMEOUT_HARD_CAP_SECONDS", "600"))
                _scene_timeout = min(_scene_timeout_requested, _scene_timeout_cap)

                if _force_direct:
                    logger.warning(
                        "Async generation: force_direct_chapter enabled; bypassing scene planner task_id=%s",
                        task_id,
                    )
                    try:
                        task.progress_text = "force_direct_chapter: 正在跳过 scene_planner，直接生成完整正文。"
                        task.char_count = 0
                        task.error_message = None
                        await db.commit()
                    except Exception as hb_err:
                        logger.warning("force_direct_chapter heartbeat failed task_id=%s err=%s", task_id, hb_err)
                    try:
                        fallback_timeout = float(
                            params.get("fallback_timeout_seconds")
                            or _os.getenv("SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS", "420")
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
                        fallback_text = await _asyncio.wait_for(
                            ChapterGenerator().generate(
                                project_id=project_id,
                                volume_id=generated_chapter_volume_id_safe,
                                chapter_idx=generated_chapter_idx_safe,
                                db=db,
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
                            task.error_message = ("force_direct_chapter blocked: " + (str(force_err) or type(force_err).__name__))[:1500]
                            await db.commit()
                        return

                if not _force_direct:
                    for _attempt in (1, 2):
                        orchestrator = SceneOrchestrator()

                        async def _consume_scene_stream() -> None:
                            async for chunk in orchestrator.orchestrate_chapter_stream(
                                project_id=project_id,
                                volume_id=generated_chapter_volume_id_safe,
                                chapter_idx=generated_chapter_idx_safe,
                                db=db,
                                chapter_id=generated_chapter_id_safe,
                                user_instruction=user_instr,
                                target_words=generated_chapter_target_words,
                            ):
                                _append_chunk(chunk)
                                if len(collected) % 5 == 0:
                                    task.progress_text = _joined_collected()
                                    task.char_count = len(task.progress_text)
                                    await db.commit()

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
                            if _attempt == 1:
                                await _asyncio.sleep(2.0)
                                continue
                            break
                        except Exception as scene_err:
                            last_scene_err = scene_err
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
                            task.char_count = 0
                            task.error_message = "scene_mode fallback running: " + _root[:900]
                            await db.commit()
                    except Exception as hb_err:
                        logger.warning("single-shot fallback heartbeat failed task_id=%s err=%s", task_id, hb_err)
                    try:
                        fallback_timeout = float(
                            params.get("fallback_timeout_seconds")
                            or _os.getenv("SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS", "420")
                        )
                        llm_timeout_kwargs = _single_shot_llm_timeout_kwargs(fallback_timeout)
                        fallback_instr = (
                            user_instr
                            + "\n\n【兜底生成要求】scene_planner/scene_writer 未能稳定输出。"
                            + "请直接按本章大纲、上下文和连续性约束生成完整正文；"
                            + "必须有开端、推进、结果与章末钩子；禁止输出说明、提纲或 JSON。"
                        )
                        fallback_text = await _asyncio.wait_for(
                            ChapterGenerator().generate(
                                project_id=project_id,
                                volume_id=generated_chapter_volume_id_safe,
                                chapter_idx=generated_chapter_idx_safe,
                                db=db,
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
                                + (str(fallback_err) or type(fallback_err).__name__)[:900]
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
                    or _os.getenv("SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS", "420")
                )
                llm_timeout_kwargs = _single_shot_llm_timeout_kwargs(fallback_timeout)
                fallback_text = await _asyncio.wait_for(
                    ChapterGenerator().generate(
                        project_id=project_id,
                        volume_id=generated_chapter_volume_id_safe,
                        chapter_idx=generated_chapter_idx_safe,
                        db=db,
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
                task.char_count = len(full_text)
                await db.commit()

            # Post-process: strip Markdown + AI fluff
            import re as _re
            full_text = _re.sub(r'\*\*([^*]+)\*\*', r'\1', full_text)  # **bold**
            full_text = _re.sub(r'\*([^*]+)\*', r'\1', full_text)  # *italic*
            full_text = _re.sub(r'^#{1,6}\s*', '', full_text, flags=_re.MULTILINE)  # # headers
            full_text = _re.sub(r'^---+\s*$', '', full_text, flags=_re.MULTILINE)  # --- hr
            full_text = _re.sub(r'^>\s*', '', full_text, flags=_re.MULTILINE)  # > blockquote
            full_text = _re.sub(r'`([^`]+)`', r'\1', full_text)  # `code`
            # Strip AI conversational fluff
            fluff_patterns = [
                # Opening fluff
                r'^(好的|当然|下面|以下|接下来|没问题)[，,。！].*?\n',
                r'^我(会|将|来|给你|不会|不能|可以|不直接|不照搬).*?\n',
                # Conditional suggestions (delete entire paragraph)
                r'^如果(你|需要|想|希望|愿意|以后|后续).*?\n',
                r'^(你也可以|你可以|可以考虑|建议你|需要的话).*?\n',
                # Closing fluff
                r'^希望(这|对你|能|以上|你|整).*?\n',
                r'^(以上|这就是|这是一份|这套|整体).*?(大纲|方案|规划|框架).*?\n',
                r'^让我.*?\n',
                # Meta-commentary about the writing process
                r'^(整体按|整体气质|整体风格|整体来看).*?\n',
                r'^(注意|提示|说明|备注)[：:].*?\n',
                # "I won't copy X but will Y" disclaimers
                r'^我不(会|能|直接|照搬).*?\n',
                r'^(不直接|不去|不照搬).*?(某|某部|具体|特定).*?\n',
            ]
            for p in fluff_patterns:
                full_text = _re.sub(p, '', full_text, flags=_re.MULTILINE)
            full_text = _re.sub(r'\n{3,}', '\n\n', full_text)  # excess newlines

            # Anti-AI humanization: break statistical patterns that detectors flag
            import random as _rand
            lines = full_text.split('\n')
            humanized = []
            for line in lines:
                line = line.strip()
                if not line:
                    humanized.append('')
                    continue
                # Break overly uniform sentence lengths by occasionally merging/splitting
                # Remove trailing symmetry patterns
                line = _re.sub(r'[，,]\s*(而|且|并|同时)$', '。', line)
                # Vary punctuation: occasionally use。instead of ，for long sentences
                if len(line) > 60 and '，' in line and _rand.random() < 0.3:
                    parts = line.split('，', 1)
                    if len(parts[0]) > 15:
                        line = parts[0] + '。' + parts[1]
                # Add occasional short interjections to break rhythm
                humanized.append(line)

            full_text = '\n'.join(humanized).strip()

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
                volume = await db.get(Volume, volume_id_for_eval)
                prev_context = ""
                if volume is not None:
                    prev_result = await db.execute(
                        _sql_select(type(ch)).where(
                            type(ch).volume_id == volume_id_for_eval,
                            type(ch).chapter_idx == chapter_idx_for_eval - 1,
                        )
                    )
                    prev_ch = prev_result.scalar_one_or_none()
                    if prev_ch:
                        prev_context = "\n\n".join(
                            part for part in [
                                "【跨章节评估硬要求】必须结合前章检查剧情衔接，重点识别失忆、幻觉、莫名新增人物/道具/地点、规则临时变更、伏笔突兀回收、空间跳跃。",
                                f"【上一章摘要】\n{prev_ch.summary}" if prev_ch.summary else "",
                                f"【上一章正文节选】\n{(prev_ch.content_text or '')[-3500:]}" if prev_ch.content_text else "",
                            ] if part
                        )

                current_text = full_text
                final_eval = None

                # Q3 v1.9.1: serialized cognition ledger for the evaluator's
                # cognition_violation check. Best-effort; "" on any failure.
                cognition_ledger_text = ""
                try:
                    from app.services import character_cognition as _cognition
                    cognition_ledger_text = _cognition.serialize_for_prompt(
                        await _cognition.load_ledger(db, project_id)
                    )
                except Exception as _cog_err:
                    logger.warning(
                        "Async generation: cognition ledger load failed: %s", _cog_err
                    )

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
                    task.char_count = len(current_text)
                    await db.commit()

                    evaluator = ChapterEvaluator()
                    # Evaluation relies on LLM calls and can occasionally hang.
                    # Put a hard timeout around it so async chapter generation
                    # always makes forward progress and saves output.
                    import asyncio as _asyncio
                    _eval_timeout = float(params.get("evaluation_timeout_seconds") or 90)
                    try:
                        eval_result = await _asyncio.wait_for(
                            evaluator.evaluate(
                                chapter_text=current_text,
                                chapter_outline=generated_chapter_outline,
                                previous_summary=prev_context,
                                style_profile=style_text,
                                cognition_ledger_text=cognition_ledger_text,
                                style_stats_text=style_stats_text,
                            ),
                            timeout=_eval_timeout,
                        )
                    except _asyncio.TimeoutError:
                        logger.warning(
                            "Auto chapter evaluation timed out (%.1fs): chapter_id=%s round=%d; saving without eval",
                            _eval_timeout,
                            chapter_id_for_eval,
                            round_idx,
                        )
                        final_eval = None
                        break
                    final_eval = eval_result
                    db.add(ChapterEvaluation(
                        chapter_id=chapter_id_for_eval,
                        plot_coherence=eval_result.plot_coherence,
                        character_consistency=eval_result.character_consistency,
                        style_adherence=eval_result.style_adherence,
                        narrative_pacing=eval_result.narrative_pacing,
                        foreshadow_handling=eval_result.foreshadow_handling,
                        overall=eval_result.overall,
                        issues_json=eval_result.issues,
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
                    # directly; otherwise fall through to the existing
                    # full-chapter regeneration below.
                    if targeted_revision_enabled():
                        try:
                            span_result = await revise_spans(current_text, eval_result.issues)
                        except Exception as targeted_err:
                            span_result = None
                            logger.warning(
                                "Async generation: targeted revision failed; falling back to full rewrite chapter_id=%s round=%d err=%s",
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
                                or _os.getenv("SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS", "420")
                            )
                            llm_timeout_kwargs = _single_shot_llm_timeout_kwargs(fallback_timeout)
                            force_revise_instr = (
                                merged_instruction
                                + "\n\n【强制修订直出要求】本轮 auto-revise 继续跳过 scene_planner/scene_writer。"
                                + "请直接输出修订后的完整小说正文，集中修复本轮评分问题；禁止输出说明、提纲或 JSON。"
                            )
                            force_revise_text = await _asyncio.wait_for(
                                ChapterGenerator().generate(
                                    project_id=project_id,
                                    volume_id=volume_id_for_eval,
                                    chapter_idx=chapter_idx_for_eval,
                                    db=db,
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
                                async for chunk in orchestrator.orchestrate_chapter_stream(
                                    project_id=project_id,
                                    volume_id=volume_id_for_eval,
                                    chapter_idx=chapter_idx_for_eval,
                                    db=db,
                                    chapter_id=chapter_id_for_eval,
                                    user_instruction=merged_instruction,
                                    target_words=generated_chapter_target_words,
                                ):
                                    regen_chunks.append(chunk)
                                    if len(regen_chunks) % 5 == 0:
                                        task.progress_text = "".join(regen_chunks)
                                        task.char_count = len(task.progress_text)
                                        await db.commit()
                                last_regen_err = None
                                break
                            except Exception as regen_scene_err:
                                last_regen_err = regen_scene_err
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
                        # NOTE: generation_tasks.status is VARCHAR(20) in DB.
                        task.status = "needs_repair"
                        task.error_message = (
                            "auto_revise blocked: scene_orchestrator_failed during regenerate. "
                            "Fix scene_planner/contract fields or upstream outline/context, then retry."
                        )
                        await db.commit()
                        break
                    regenerated = "".join(regen_chunks).strip()
                    if not regenerated:
                        logger.warning("Auto chapter regeneration returned empty text: chapter_id=%s round=%d", chapter_id_for_eval, round_idx)
                        break
                    current_text = regenerated

                full_text = current_text
                if full_text and not bool(params.get("skip_polish")):
                    try:
                        import asyncio as _asyncio_qg

                        quality_gate_timeout = float(
                            params.get("quality_gate_timeout_seconds")
                            or _os.getenv("CHAPTER_QUALITY_GATE_TIMEOUT_SECONDS", "420")
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
                                quality_result.final_text,
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
                                await summarize_and_save_chapter(chapter_id=chapter_id_for_eval, db=db, overwrite=True)
                            except Exception as sum_err:
                                logger.warning("Auto chapter summarize failed after quality-gate review save: %s", sum_err)
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
                            await summarize_and_save_chapter(chapter_id=chapter_id_for_eval, db=db, overwrite=True)
                        except Exception as sum_err:
                            logger.warning("Auto chapter summarize failed after quality-gate error save: %s", sum_err)
                        return

                ch.content_text = full_text
                ch.word_count = len(full_text)
                passed = bool(final_eval and getattr(final_eval, "overall", 0) >= threshold)
                ch.status = "completed" if passed else "needs_review"
                quality_gate_summary = (task.params_json or {}).get("quality_gate")
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
                    },
                ))
                await db.commit()
                try:
                    await summarize_and_save_chapter(chapter_id=chapter_id_for_eval, db=db, overwrite=True)
                except Exception as sum_err:
                    logger.warning("Auto chapter summarize failed after evaluation: %s", sum_err)
                # Q3 v1.9.1 (review fix): character cognition ledger ingestion
                # only when the final evaluation passed. needs_review saves
                # (passed=False, ch.status='needs_review') await manual review
                # or a rewrite and must not pollute the ledger — previously
                # this final-save path ingested them anyway, contradicting the
                # quality-gate-blocked branch above which already skips
                # ingestion. Never blocks chapter persistence.
                if passed:
                    try:
                        from app.services.character_cognition import extract_and_update
                        await extract_and_update(db, project_id, full_text)
                    except Exception as cog_err:
                        logger.warning("Cognition ledger update failed after evaluation: %s", cog_err)
                    # C2/F1: recompute whole-book style stats in the background
                    # (deterministic, non-blocking, celery-optional).
                    try:
                        from app.services.style_stat import dispatch_style_recompute
                        dispatch_style_recompute(project_id, caller="knowledge_tasks.chapter")
                    except Exception as style_err:
                        logger.warning("Style stats dispatch failed: %s", style_err)
                else:
                    logger.debug(
                        "Skipping cognition ledger ingestion for needs_review chapter %s (overall below threshold)",
                        chapter_id_for_eval,
                    )

            task.result_text = full_text

            # Second pass: LLM anti-AI polishing (optional, only when enabled)
            if params.get("enable_polish"):
                task.status = "polishing"
                task.progress_text = full_text
                await db.commit()

                try:
                    polish_chunks = []
                    chunk_size = 2000
                    for i in range(0, len(full_text), chunk_size):
                        chunk_text = full_text[i:i + chunk_size]
                        polish_result = await router.generate(
                            task_type="polishing",
                            messages=[
                                {"role": "system", "content": (
                                    "你是文本润色编辑。改写以下文本让它更像人写的。\n"
                                    "规则：保持所有内容不变，只改表达方式。\n"
                                    "加口语化表达，打破均匀节奏，去对称句式。\n"
                                    "直接输出，不加说明，不用Markdown。"
                                )},
                                {"role": "user", "content": chunk_text},
                            ],
                        )
                        polish_chunks.append(polish_result.text if polish_result.text else chunk_text)
                    polished = "".join(polish_chunks)
                    polished = _re.sub(r'\*\*([^*]+)\*\*', r'\1', polished)
                    polished = _re.sub(r'\*([^*]+)\*', r'\1', polished)
                    polished = _re.sub(r'^#{1,6}\s*', '', polished, flags=_re.MULTILINE)
                    polished = _re.sub(r'\n{3,}', '\n\n', polished)
                    task.polished_text = polished.strip()
                except Exception as pe:
                    logger.warning("Polishing failed, using raw text: %s", pe)
                    task.polished_text = full_text
            else:
                task.polished_text = ""  # No polishing requested

            task.progress_text = full_text
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
                    _content = {"raw_text": full_text_clean}
                    if _vol_plan:
                        _content["volume_plan"] = _vol_plan
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
                        for _v in _vol_plan:
                            _idx = int(_v.get("idx") or 0)
                            if _idx <= 0 or _idx in existing_idx:
                                continue
                            db.add(_Volume(
                                project_id=project_id,
                                volume_idx=_idx,
                                title=str(_v.get("title") or f"第{_idx}卷"),
                                summary=str(_v.get("theme") or ""),
                            ))
                        logger.info(
                            "PR-OL2: planned %d volumes from <volume-plan> for project=%s",
                            len(_vol_plan), project_id,
                        )
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
                    session_factory2 = _make_session()
                    async with session_factory2() as db2:
                        task2 = await db2.get(GenerationTask, task_id)
                        if task2:
                            task2.progress_text = _salvage_text
                            task2.result_text = _salvage_text
                            task2.char_count = len(_salvage_text)
                            task2.polished_text = task2.polished_text or ""
                            task2.status = "needs_review" if _salvage_chapter_id else "completed"
                            task2.error_message = None
                        if _salvage_chapter_id:
                            ch2 = await db2.get(Chapter, _salvage_chapter_id)
                            if ch2:
                                ch2.content_text = _salvage_text
                                ch2.word_count = len(_salvage_text)
                                ch2.status = "needs_review"
                        await db2.commit()
                    logger.warning(
                        "Async generation salvaged produced prose after persistence/session error task_id=%s chars=%d err=%s",
                        task_id,
                        len(_salvage_text),
                        e,
                    )
                    return
                except Exception as salvage_err:
                    logger.error("Async generation salvage commit failed; falling back to failed status: %s", salvage_err)

            # v1.12 L5: be defensive about DB connection drops inside long
            # running Celery tasks. If no generated text is available to salvage,
            # persist failure using a fresh session so the UI does not remain
            # stuck in running.
            try:
                task.status = "failed"
                task.error_message = str(e)[:500]
                await db.commit()
            except Exception as commit_err:
                logger.warning("Async generation failure commit failed; retry with fresh session: %s", commit_err)
                try:
                    await db.rollback()
                except Exception:
                    pass
                try:
                    session_factory2 = _make_session()
                    async with session_factory2() as db2:
                        task2 = await db2.get(GenerationTask, task_id)
                        if task2:
                            task2.status = "failed"
                            task2.error_message = str(e)[:500]
                            await db2.commit()
                except Exception as commit_err2:
                    logger.error("Async generation failure commit (fresh session) also failed: %s", commit_err2)
            logger.exception("Async generation failed: %s", task_id)


@celery_app.task(name="tasks.run_pipeline_generation")
def run_pipeline_generation(pipeline_id: str):
    """Execute pipeline generation: iterate chapters, generate, review."""
    _run_async(_run_pipeline_async(pipeline_id))


async def _run_pipeline_async(pipeline_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.pipeline import PipelineRun, PipelineChapterStatus
    from app.models.project import Chapter
    from app.services.model_router import get_model_router_async
    from datetime import datetime, timezone
    import asyncio as aio

    async with async_session_factory() as db:
        pipeline = await db.get(PipelineRun, pipeline_id)
        if not pipeline or pipeline.state not in ("generating", "planning"):
            return

        if pipeline.state == "planning":
            pipeline.state = "generating"
            pipeline.started_at = datetime.now(timezone.utc)
            await db.commit()

        router = await get_model_router_async()

        # Get pending chapters
        result = await db.execute(
            select(PipelineChapterStatus)
            .where(
                PipelineChapterStatus.pipeline_id == pipeline.id,
                PipelineChapterStatus.state == "pending",
            )
            .order_by(PipelineChapterStatus.chapter_idx)
        )
        pending = list(result.scalars().all())

        for cs in pending:
            if pipeline.state == "paused":
                break

            chapter = await db.get(Chapter, str(cs.chapter_id))
            if not chapter:
                cs.state = "failed"
                cs.error_message = "章节不存在"
                await db.commit()
                continue

            cs.state = "generating"
            cs.started_at = datetime.now(timezone.utc)
            pipeline.current_chapter_idx = cs.chapter_idx
            await db.commit()

            try:
                gen_result = await router.generate(
                    task_type="generation",
                    messages=[
                        {"role": "system", "content": "你是一位专业的小说内容生成引擎。根据章节标题和大纲生成正文。每章至少3000字。"},
                        {"role": "user", "content": f"章节标题：{chapter.title}\n大纲：{chapter.outline_json or '无'}"},
                    ],
                    max_tokens=8192,
                )

                chapter.content_text = gen_result.text
                chapter.word_count = len(gen_result.text)
                chapter.status = "completed"
                cs.state = "completed"
                cs.word_count = len(gen_result.text)
                cs.completed_at = datetime.now(timezone.utc)
                pipeline.completed_chapters = (pipeline.completed_chapters or 0) + 1

            except Exception as e:
                cs.state = "failed"
                cs.error_message = str(e)[:200]
                logger.warning("Pipeline chapter %d failed: %s", cs.chapter_idx, e)

            await db.commit()
            # B2' (v1.5.0): kick entity-extraction task post-commit when the
            # chapter actually got a new body. Failures never block the
            # pipeline: they retry asynchronously on the celery queue.
            if cs.state == "completed":
                try:
                    from app.services.entity_dispatch import dispatch_for_chapter
                    await dispatch_for_chapter(
                        chapter, db,
                        caller="knowledge_tasks.run_pipeline_chapters",
                        project_id_hint=str(pipeline.project_id),
                    )
                except Exception as dispatch_err:
                    logger.warning(
                        "Entity dispatch after pipeline chapter %d failed: %s",
                        cs.chapter_idx, dispatch_err,
                    )
            await aio.sleep(1)  # Rate limit

        # Advance pipeline state
        from app.services.pipeline_service import advance_pipeline
        await advance_pipeline(db, pipeline_id)
        await db.commit()

        logger.info("Pipeline %s state: %s (%d/%d chapters)",
                     pipeline_id, pipeline.state, pipeline.completed_chapters, pipeline.total_chapters)


@celery_app.task(name="tasks.process_uploaded_book")
def process_uploaded_book(book_id: str, file_path: str, filename: str, user_title: str = "", user_author: str = ""):
    """Process an uploaded book file: parse → chunk → auto-score."""
    _run_async(_process_uploaded_book_async(book_id, file_path, filename, user_title, user_author))


async def _process_uploaded_book_async(book_id: str, file_path: str, filename: str, user_title: str, user_author: str):
    import os
    from app.db.session import async_session_factory
    from app.models.project import ReferenceBook, TextChunk
    from app.services.text_pipeline import process_text_file

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            logger.error("Book %s not found", book_id)
            return

        try:
            # Stage 1: cleaning
            book.status = "cleaning"
            await db.commit()

            with open(file_path, "rb") as f:
                content = f.read()

            parse_result, blocks = process_text_file(content, filename)

            if parse_result.title and not user_title:
                book.title = parse_result.title
            if parse_result.author and not user_author:
                book.author = parse_result.author
            book.total_chapters = len(parse_result.chapters)
            book.total_words = parse_result.total_chars

            # Save text chunks
            for block in blocks:
                chunk = TextChunk(
                    book_id=book.id,
                    chapter_idx=block.chapter_idx,
                    block_idx=block.block_idx,
                    chapter_title=block.chapter_title,
                    content=block.content,
                    char_count=block.char_count,
                    sequence_id=block.sequence_id,
                )
                db.add(chunk)
            await db.commit()

            # Stage 2: vectorize chunks into Qdrant
            book.status = "extracting"
            await db.commit()

            try:
                from app.services.qdrant_store import QdrantStore
                from app.services.model_router import get_model_router_async
                from qdrant_client import AsyncQdrantClient as _QC
                from app.config import settings as _s

                # Pre-load model router (with DB config + decrypted keys)
                router = await get_model_router_async()

                qdrant_client = _QC(host=_s.QDRANT_HOST, port=_s.QDRANT_PORT)
                store = QdrantStore(qdrant_client)
                await store.ensure_collections()
                vectorized = 0
                for block in blocks:
                    try:
                        embedding = await router.embed(block.content[:500])
                        if embedding and any(v != 0 for v in embedding[:10]):
                            await store.store_style_features(
                                book_id=str(book.id),
                                chunk_id=f"{block.chapter_idx}_{block.block_idx}",
                                sequence_id=block.sequence_id,
                                features_dict={"chapter_title": block.chapter_title, "char_count": block.char_count},
                                embedding=embedding,
                            )
                            vectorized += 1
                    except Exception:
                        pass
                await qdrant_client.close()
                logger.info("Vectorized %d/%d chunks for %s", vectorized, len(blocks), book.title)
            except Exception as e:
                logger.warning("Vectorization skipped for %s: %s", book.title, e)

            # Stage 3: auto quality scoring (if model configured)
            try:
                from app.services.model_router import get_model_router_async as _gmra
                await _gmra()  # Ensure router loaded before QualityScorer uses sync version
                from app.services.quality_scorer import QualityScorer
                scorer = QualityScorer()

                sample_blocks = blocks[:5] if len(blocks) <= 5 else [blocks[i] for i in range(0, len(blocks), max(1, len(blocks) // 5))][:5]
                samples = [b.content for b in sample_blocks]

                score, is_suitable = await scorer.score_and_filter(samples)
                metadata = book.metadata_json or {}
                metadata["quality_score"] = score.to_dict()
                book.metadata_json = metadata
                if not is_suitable:
                    book.status = "low_quality"
                else:
                    book.status = "ready"
            except Exception as e:
                logger.warning("Auto-scoring skipped for %s: %s", book.title, e)
                book.status = "ready"

            await db.commit()
            # PR-BOOK-PROFILE-BIND: ensure every reference_book has a bound StyleProfile
            try:
                from app.services.style_profile_resolver import get_or_create_book_profile
                await get_or_create_book_profile(db, str(book.id))
            except Exception as e:
                logger.warning("auto profile binding failed for %s: %s", book.title, e)
            logger.info("Book processed: %s — %d chapters, %d chars", book.title, book.total_chapters, book.total_words)

        except Exception as e:
            book.status = "error"
            book.error_message = str(e)[:500]
            await db.commit()
            logger.exception("Failed to process book %s", book_id)
        finally:
            # Clean up temp file
            try:
                os.unlink(file_path)
            except OSError:
                pass


@celery_app.task(name="tasks.batch_test_sources")
def batch_test_sources_task(source_ids: list[str]):
    """Batch test book sources for connectivity in background."""
    _run_async(_batch_test_async(source_ids))


async def _batch_test_async(source_ids: list[str]):
    import httpx
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import BookSource

    client = httpx.AsyncClient(
        timeout=6,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/107.0 Mobile Safari/537.36"},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )

    ok_count = 0
    fail_count = 0

    async with async_session_factory() as db:
        result = await db.execute(
            select(BookSource).where(BookSource.id.in_(source_ids))
        )
        sources = list(result.scalars().all())

        async def test_one(source: BookSource):
            sj = source.source_json or {}
            base = sj.get("bookSourceUrl", "")
            if not base:
                return False
            try:
                resp = await client.get(base)
                reachable = resp.status_code < 500
                source.last_test_ok = 1 if reachable else 0
                source.last_test_at = datetime.now(timezone.utc)
                if reachable:
                    source.success_count = (source.success_count or 0) + 1
                    source.consecutive_fails = 0
                    source.score = min(10.0, (source.score or 5.0) + 0.3)
                else:
                    source.fail_count = (source.fail_count or 0) + 1
                    source.consecutive_fails = (source.consecutive_fails or 0) + 1
                    source.score = max(0.0, (source.score or 5.0) - 0.5)
                return reachable
            except Exception:
                source.last_test_ok = 0
                source.last_test_at = datetime.now(timezone.utc)
                source.fail_count = (source.fail_count or 0) + 1
                source.consecutive_fails = (source.consecutive_fails or 0) + 1
                source.score = max(0.0, (source.score or 5.0) - 1.0)
                return False

        batch_size = 30
        for i in range(0, len(sources), batch_size):
            batch = sources[i:i + batch_size]
            results = await asyncio.gather(*[test_one(s) for s in batch], return_exceptions=True)
            for r in results:
                if r is True:
                    ok_count += 1
                else:
                    fail_count += 1
            await db.commit()
            logger.info("Batch test progress: %d/%d (ok=%d, fail=%d)", i + len(batch), len(sources), ok_count, fail_count)

    await client.aclose()
    logger.info("Batch test complete: %d total, %d ok, %d failed", len(sources), ok_count, fail_count)


@celery_app.task(bind=True, name="tasks.crawl_book", max_retries=3)
def crawl_book(self, task_id: str):
    """
    Crawl a book using the book source engine.

    Steps:
    1. Load CrawlTask and BookSource from DB
    2. Parse book source rules
    3. Get table of contents
    4. Fetch each chapter with rate limiting
    5. Save raw content to ReferenceBook chapters
    6. Trigger text cleaning pipeline
    """
    _run_async(_crawl_book_async(self, task_id))


async def _crawl_book_async(task, task_id: str):
    from app.db.session import async_session_factory
    from app.models.project import CrawlTask, BookSource, ReferenceBook, TextChunk
    from app.services.book_source_engine import BookSourceEngine
    from app.services.text_pipeline import _strip_noise, slice_chapters_to_blocks, ChapterData

    engine = BookSourceEngine()

    async with async_session_factory() as db:
        crawl_task = await db.get(CrawlTask, task_id)
        if not crawl_task:
            logger.error("CrawlTask %s not found", task_id)
            return

        crawl_task.status = "running"
        await db.commit()

        book = await db.get(ReferenceBook, str(crawl_task.book_id))
        source = await db.get(BookSource, str(crawl_task.source_id)) if crawl_task.source_id else None

        if not source:
            crawl_task.status = "error"
            crawl_task.error_message = "Book source not found"
            await db.commit()
            return

        try:
            config = engine.parse_source(source.source_json)

            # Get TOC
            chapters = await engine.get_toc(config, crawl_task.book_url)
            crawl_task.total_chapters = len(chapters)
            await db.commit()

            if not chapters:
                crawl_task.status = "error"
                crawl_task.error_message = "No chapters found"
                await db.commit()
                return

            # Fetch each chapter
            all_chapter_data = []
            for idx, ch in enumerate(chapters):
                try:
                    content = await engine.get_content(config, ch.url)
                    if content:
                        content = _strip_noise(content)
                        all_chapter_data.append(ChapterData(
                            chapter_idx=idx + 1,
                            title=ch.title,
                            content=content,
                        ))

                    crawl_task.completed_chapters = idx + 1
                    if idx % 10 == 0:
                        await db.commit()

                    # Rate limiting
                    import asyncio as aio
                    await aio.sleep(1.0)

                except Exception as e:
                    logger.warning("Failed to fetch chapter %d: %s", idx, e)
                    continue

            # Slice into blocks
            blocks = slice_chapters_to_blocks(all_chapter_data)

            # Save blocks
            for block in blocks:
                chunk = TextChunk(
                    book_id=book.id,
                    chapter_idx=block.chapter_idx,
                    block_idx=block.block_idx,
                    chapter_title=block.chapter_title,
                    content=block.content,
                    char_count=block.char_count,
                    sequence_id=block.sequence_id,
                )
                db.add(chunk)

            book.total_chapters = len(all_chapter_data)
            book.total_words = sum(ch.char_count for ch in all_chapter_data)
            book.status = "ready"
            crawl_task.status = "completed"
            await db.commit()

            logger.info(
                "Crawl complete: %s — %d chapters, %d blocks",
                book.title, len(all_chapter_data), len(blocks),
            )

        except Exception as e:
            logger.exception("Crawl failed for task %s", task_id)
            crawl_task.status = "error"
            crawl_task.error_message = str(e)
            book.status = "error"
            book.error_message = str(e)
            await db.commit()

    await engine.close()


@celery_app.task(name="tasks.extract_features")
def extract_features(book_id: str):
    """Extract plot and style features from all chunks of a book."""
    _run_async(_extract_features_async(book_id))


async def _extract_features_async(book_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import TextChunk, ReferenceBook
    from app.services.feature_extractor import PlotExtractor, StyleExtractor

    plot_extractor = PlotExtractor()
    style_extractor = StyleExtractor()

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            return

        book.status = "extracting"
        await db.commit()

        result = await db.execute(
            select(TextChunk)
            .where(TextChunk.book_id == book_id)
            .order_by(TextChunk.sequence_id)
        )
        chunks = result.scalars().all()

        # Initialize Qdrant for vectorization
        qdrant_store = None
        try:
            from qdrant_client import AsyncQdrantClient
            from app.config import settings as _cfg
            from app.services.qdrant_store import QdrantStore
            from app.services.feature_extractor import generate_embedding

            _qc = AsyncQdrantClient(host=_cfg.QDRANT_HOST, port=_cfg.QDRANT_PORT)
            qdrant_store = QdrantStore(_qc)
            await qdrant_store.ensure_collections()
        except Exception as e:
            logger.warning("Qdrant not available for vectorization: %s", e)

        for chunk in chunks:
            try:
                # Style extraction (fast, no LLM)
                if not chunk.style_extracted:
                    style_features = style_extractor.extract(chunk.content)
                    chunk.style_features_json = style_features if isinstance(style_features, dict) else {"raw": str(style_features)}
                    chunk.style_extracted = 1

                # Plot extraction (LLM, slower)
                if not chunk.plot_extracted:
                    plot_features = await plot_extractor.extract(chunk.content)
                    chunk.plot_features_json = plot_features if isinstance(plot_features, dict) else {"raw": str(plot_features)}
                    chunk.plot_extracted = 1

                # Vectorize to Qdrant
                if qdrant_store:
                    try:
                        embedding = await generate_embedding(chunk.content[:500])
                        if embedding and any(v != 0 for v in embedding[:10]):
                            summary = str(chunk.plot_features_json.get("summary", "")) if chunk.plot_features_json else ""
                            await qdrant_store.store_plot_features(
                                book_id=book_id, chunk_id=str(chunk.id),
                                sequence_id=chunk.sequence_id, summary_text=summary,
                                embedding=embedding,
                            )
                            await qdrant_store.store_style_features(
                                book_id=book_id, chunk_id=str(chunk.id),
                                sequence_id=chunk.sequence_id,
                                features_dict=chunk.style_features_json or {},
                                embedding=embedding,
                            )
                    except Exception as ve:
                        logger.debug("Vectorization failed for chunk %s: %s", chunk.id, ve)

                await db.commit()
            except Exception as e:
                logger.warning("Feature extraction failed for chunk %s: %s", chunk.id, e)

        if qdrant_store:
            await _qc.close()

        book.status = "ready"
        await db.commit()
        logger.info("Feature extraction + vectorization complete for book %s", book.title)


@celery_app.task(name="tasks.run_quality_score")
def run_quality_score(book_id: str):
    """Run quality scoring on a reference book."""
    _run_async(_run_quality_score_async(book_id))


async def _run_quality_score_async(book_id: str):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import TextChunk, ReferenceBook
    from app.services.quality_scorer import QualityScorer

    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            return

        result = await db.execute(
            select(TextChunk)
            .where(TextChunk.book_id == book_id)
            .order_by(TextChunk.sequence_id)
        )
        chunks = result.scalars().all()
        if not chunks:
            return

        # Sample 5 blocks evenly
        n = len(chunks)
        step = max(1, n // 5)
        samples = [chunks[i].content for i in range(0, n, step)][:5]

        scorer = QualityScorer()
        score, is_suitable = await scorer.score_and_filter(samples)

        metadata = book.metadata_json or {}
        metadata["quality_score"] = score.to_dict()
        book.metadata_json = metadata

        if not is_suitable:
            book.status = "low_quality"

        await db.commit()
        logger.info("Quality score for %s: %.1f (%s)", book.title, score.overall, score.verdict)
