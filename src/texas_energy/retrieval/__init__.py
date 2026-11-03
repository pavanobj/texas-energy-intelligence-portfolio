"""Private, local retrieval utilities for trusted energy knowledge."""

from .local import (
    KnowledgeIndex,
    RetrievalAnswer,
    RetrievalError,
    RetrievalHit,
    TransformerEmbeddingBackend,
    build_default_index,
    search_energy_knowledge,
)

__all__ = [
    "KnowledgeIndex",
    "RetrievalAnswer",
    "RetrievalError",
    "RetrievalHit",
    "TransformerEmbeddingBackend",
    "build_default_index",
    "search_energy_knowledge",
]
