"""Generation graph (v1.0 chunk 8).

Models the existing `plan -> draft -> critic -> rewrite -> finalize -> compact`
state machine as a LangGraph ``StateGraph``. ``recall`` is a pre-draft
context-gathering step (RAG + ctxpack).

The critic edge shares the runner's exact-draft review contract. Missing
reviews and exhausted revision budgets stop before finalize/compaction.

The nodes themselves delegate to the same coroutines that
``app.services.generation_runner`` uses, so both runners stay consistent
until we fully cut over.

This module is imported lazily; it is a no-op unless the
``LANGGRAPH_RUNNER_ENABLED`` flag is on.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, TypedDict

from app.services.review_contract import critic_next_action

logger = logging.getLogger(__name__)


class GenerationState(TypedDict, total=False):
    run_id: str
    project_id: str
    chapter_id: str | None
    planning: dict
    draft_text: str
    critic_report: dict
    rewrite_count: int
    max_rewrite_count: int
    final_text: str
    bvsr: dict
    status: str
    stop_reason: str
    editorial_status: str


def route_critic(state: GenerationState) -> str:
    action = critic_next_action(
        state.get('critic_report'), state.get('draft_text', ''),
        int(state.get('rewrite_count', 0)), int(state.get('max_rewrite_count', 3)),
    )
    return action if action in {'rewrite', 'finalize'} else 'needs_review'


def is_langgraph_enabled() -> bool:
    return os.getenv("LANGGRAPH_RUNNER_ENABLED", "false").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_generation_graph(
    *,
    phase_planning: Callable,
    phase_drafting: Callable,
    phase_critic: Callable,
    phase_rewrite: Callable,
    phase_finalize: Callable | None = None,
    phase_compact: Callable | None = None,
    checkpointer: Any = None,
) -> Any:
    """Build and compile the generation StateGraph.

    Callers inject the phase coroutines so tests can stub them.  Each
    coroutine signature is ``async def phase(state) -> state_patch``.
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(GenerationState)

    # --- nodes ---
    async def plan(state: GenerationState) -> dict:
        data = await phase_planning(state)
        return {"planning": data or {}}

    async def recall(state: GenerationState) -> dict:
        # Context recall happens as part of planning today; keep recall as a
        # pass-through so future RAG lookups can slot in without changing the
        # graph topology.
        return {}

    async def draft(state: GenerationState) -> dict:
        text = await phase_drafting(state)
        return {"draft_text": text or ""}

    async def critic(state: GenerationState) -> dict:
        report = await phase_critic(state)
        return {"critic_report": report or {}}

    async def rewrite(state: GenerationState) -> dict:
        text = await phase_rewrite(state)
        return {
            "draft_text": text or state.get("draft_text", ""),
            "rewrite_count": int(state.get("rewrite_count", 0)) + 1,
        }

    async def finalize(state: GenerationState) -> dict:
        if phase_finalize is not None:
            await phase_finalize(state)
        return {"final_text": state.get("draft_text", ""), 'status': 'done', 'editorial_status': 'needs_author_review'}

    async def needs_review(state: GenerationState) -> dict:
        reason = critic_next_action(
            state.get('critic_report'), state.get('draft_text', ''),
            int(state.get('rewrite_count', 0)), int(state.get('max_rewrite_count', 3)),
        )
        return {'final_text': state.get('draft_text', ''), 'status': 'needs_review',
                'stop_reason': reason, 'editorial_status': 'needs_author_review'}

    async def compact(state: GenerationState) -> dict:
        if phase_compact is not None:
            await phase_compact(state)
        return {}

    # --- topology ---
    builder.add_node("plan", plan)
    builder.add_node("recall", recall)
    builder.add_node("draft", draft)
    builder.add_node("critic", critic)
    builder.add_node("rewrite", rewrite)
    builder.add_node("finalize", finalize)
    builder.add_node("compact", compact)
    builder.add_node('needs_review', needs_review)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "recall")
    builder.add_edge("recall", "draft")
    builder.add_edge("draft", "critic")

    builder.add_conditional_edges('critic', route_critic, {k: k for k in ('rewrite', 'finalize', 'needs_review')})
    builder.add_edge("rewrite", "critic")
    builder.add_edge("finalize", "compact")
    builder.add_edge("compact", END)
    builder.add_edge('needs_review', END)

    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


# ---------------------------------------------------------------------------
# DOT serialization (for frontend react-flow)
# ---------------------------------------------------------------------------

_DOT = '''digraph generation_graph {
  rankdir=LR;
  node [shape=box, style="rounded,filled", fillcolor="#f4f6fb", fontname="Inter"];
  START [shape=circle, label="", fillcolor="#333333", width=0.3];
  END   [shape=doublecircle, label="", fillcolor="#cccccc", width=0.3];
  plan     [label="plan"];
  recall   [label="recall"];
  draft    [label="draft"];
  critic   [label="critic"];
  rewrite  [label="rewrite"];
  finalize [label="finalize"];
  compact  [label="compact"];
  needs_review [label="needs_review"];
  START -> plan;
  plan -> recall;
  recall -> draft;
  draft -> critic;
  critic -> rewrite [label="complete review + hard issues + budget"];
  critic -> finalize [label="complete review + no hard/soft issues"];
  critic -> needs_review [label="incomplete review / remaining issues"];
  rewrite -> critic;
  finalize -> compact;
  compact -> END;
  needs_review -> END;
}
'''


def to_dot() -> str:
    """Return a DOT graph description for the generation graph."""
    return _DOT
