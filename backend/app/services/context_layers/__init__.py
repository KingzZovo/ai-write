"""Context Pack Layer Builders — extracted from context_pack.py for maintainability."""

from app.services.context_layers.proximity import build_proximity_layer
from app.services.context_layers.facts import build_facts_layer
from app.services.context_layers.rag import build_rag_layer

__all__ = ["build_proximity_layer", "build_facts_layer", "build_rag_layer"]
