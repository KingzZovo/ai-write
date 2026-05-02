"""LangGraph-based orchestration (v1.0 chunk 8).

This package holds graph definitions that can progressively replace the
hand-rolled state machine in :mod:`app.services.generation_runner`. Each
graph module exposes a ``build()`` factory and a ``to_dot()`` helper.
"""
from __future__ import annotations

from .generation_graph import build_generation_graph, to_dot

__all__ = ["build_generation_graph", "to_dot"]
