"""
Chapter Generation Service (v0.5)

Uses ContextPackBuilder (L1 proximity + L2 facts + L3 RAG) and routes
through PromptRegistry so every call is logged in llm_call_logs.
"""

from __future__ import annotations

import hashlib
import logging
from typing import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.context_pack import ContextPackBuilder
from app.services.narrative_contract import NARRATIVE_CONTRACT_VERSION
from app.services.narrative_quality_gates import preflight_scene_blueprint_prompt
from app.services.outline_readiness import build_outline_readiness_report
from app.services.prompt_registry import run_text_prompt, stream_text_prompt

logger = logging.getLogger(__name__)


class ChapterGenerator:
    """Orchestrates chapter generation via ContextPack + PromptRegistry."""

    async def generate(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession,
        chapter_id: str | UUID | None = None,
        user_instruction: str = "",
        request_timeout: float | None = None,
        retry_attempts: int | None = None,
        stream: bool | None = None,
    ) -> str:
        """Non-streaming: build pack, call run_text_prompt, return full text.

        Long single-shot chapter generation can legitimately take longer than
        the global chat timeout. Callers may pass per-call routing kwargs so
        the timeout budget used by the outer task guard also reaches the LLM
        request/stream collection layer.
        """
        await self._assert_outline_chain_ready(
            db=db,
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
            chapter_id=chapter_id,
        )
        pack = await ContextPackBuilder(db=db).build(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
        )
        messages = self._with_preflight_blueprint(
            pack.to_messages(user_instruction),
            chapter_idx=chapter_idx,
        )
        rag_hits = self._collect_rag_hits(pack)

        llm_kwargs: dict[str, object] = {}
        if request_timeout is not None:
            llm_kwargs["request_timeout"] = request_timeout
        if retry_attempts is not None:
            llm_kwargs["retry_attempts"] = retry_attempts
        if stream is not None:
            llm_kwargs["stream"] = stream

        result = await run_text_prompt(
            task_type="generation",
            user_content="",
            db=db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            rag_hits=rag_hits,
            messages=messages,
            **llm_kwargs,
        )
        return result.text

    async def generate_stream(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession,
        chapter_id: str | UUID | None = None,
        user_instruction: str = "",
    ) -> AsyncIterator[str]:
        """SSE streaming: build pack, stream through PromptRegistry."""
        await self._assert_outline_chain_ready(
            db=db,
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
            chapter_id=chapter_id,
        )
        pack = await ContextPackBuilder(db=db).build(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
        )
        messages = self._with_preflight_blueprint(
            pack.to_messages(user_instruction),
            chapter_idx=chapter_idx,
        )
        rag_hits = self._collect_rag_hits(pack)

        async for chunk in stream_text_prompt(
            task_type="generation",
            user_content="",
            db=db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            rag_hits=rag_hits,
            messages=messages,
        ):
            yield chunk

    @staticmethod
    def _with_preflight_blueprint(messages: list[dict], *, chapter_idx: int) -> list[dict]:
        """Inject direct-generation-first micro-continuity budget outline contract before drafting.

        The blueprint is a generation scaffold: it asks the model to internally
        plan outline execution-unit movement/resource/information/expression/result budgets and
        anchor-audit every unit before prose, but not to output the scaffold. This keeps the main flow direct-generation-first without
        relying on a post-output quality gate.
        """
        blueprint = preflight_scene_blueprint_prompt(chapter_idx=chapter_idx)
        prompt_hash = hashlib.sha256(blueprint.encode("utf-8")).hexdigest()[:16]
        markers = (
            "direct_generation_first_v4.13",
            "chinese_prose_mechanics",
            "cross_project_prose_quality_contract",
            "outline_execution_units",
            "chapter_outline_unit_ledger",
            "outline_beat_execution_ledger",
            "foreshadow_control_ledger",
            "character_state_ledger",
            "pacing_budget_ledger",
            "evidence_permission_ledger",
            "mechanism_boundary_ledger",
            "inference_uncertainty_ledger",
            "time_window_budget",
            "spatial_feasibility_ledger",
            "channel_occlusion_ledger",
            "coincidence_friction_ledger",
            "dialogue_density_ledger",
            "communication_damping",
            "plain_register_no_wit",
            "focal_measure_only",
            "motive_exposition_zero",
            "floating_dialogue_exchange",
            "prop_fiddling_guard",
            "explicit_pause_marker_zero",
            "subtext_occlusion",
            "mundane_scene_plausibility",
            "plain_modern_register",
            "plain_contemporary_chinese",
            "age_plausibility",
            "abstract_reasoning_zero",
            "limited_pov_only",
            "semantic_density_budget",
            "resource_continuity",
            "action_causality",
            "motivation_bridge",
            "anchor_audit_before_prose",
            "micro_continuity_budget",
            "unit_movement_budget",
            "unit_resource_budget",
            "unit_information_ladder",
            "unit_expression_role",
            "unit_result_delta_cap",
            "no_budget_no_upgrade",
            "runtime_prompt_snapshot",
        )
        missing = [marker for marker in markers if marker not in blueprint]
        logger.warning(
            "preflight_contract_snapshot contract_version=%s chapter_idx=%s prompt_hash=%s markers_missing=%s",
            NARRATIVE_CONTRACT_VERSION,
            chapter_idx,
            prompt_hash,
            ",".join(missing) if missing else "none",
        )
        return [{"role": "system", "content": blueprint}, *messages]

    @staticmethod
    async def _assert_outline_chain_ready(
        *,
        db: AsyncSession,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        chapter_id: str | UUID | None = None,
    ) -> None:
        readiness = await build_outline_readiness_report(
            db,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            volume_id=str(volume_id),
            chapter_idx=chapter_idx,
        )
        if not readiness.ready:
            missing = ",".join(readiness.missing_layers)
            raise RuntimeError(
                f"outline_chain_incomplete: {readiness.block_message()} ({missing})"
            )

    @staticmethod
    def _collect_rag_hits(pack) -> list[dict]:
        """Flatten ContextPack RAG layers into a serializable list for logging."""
        hits: list[dict] = []
        for s in pack.rag_snippets:
            hits.append({"collection": "chapter_summaries", "payload": {"summary": s}})
        for name, lines in pack.dialogue_samples.items():
            hits.append({
                "collection": "dialogue_samples",
                "payload": {"character": name, "lines": lines},
            })
        for s in pack.style_samples:
            hits.append({"collection": "style_samples", "payload": {"text": s}})
        return hits
