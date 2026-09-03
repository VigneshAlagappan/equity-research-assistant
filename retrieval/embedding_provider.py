"""EmbeddingProvider — the abstraction that turns text into vectors for
semantic search, kept strictly independent of the VectorStore that later
indexes those vectors (config/settings.py's EMBEDDING_PROVIDER doc comment,
architecture.md "Hybrid Document Retrieval").

Dependency direction (STRICT — see retrieval/vector_store.py's own
docstring for the matching rule on the storage side):

    Hybrid Retriever -> EmbeddingProvider (this module's Protocol)
                              -> retrieval/embedding_provider_local.py  (sentence-transformers)
                              -> retrieval/embedding_provider_voyage.py (Voyage AI API)

    Hybrid Retriever -> VectorStore (retrieval/vector_store.py's Protocol)
                              -> retrieval/vector_store_qdrant.py (Qdrant)

No research/investigation/planner/route/ingestion code ever imports
sentence_transformers or voyageai directly — only the two concrete provider
modules above do. Everything else calls default_embedding_provider() (or
receives an EmbeddingProvider via dependency injection, same "capability,
not a direct import" seam research/capabilities.py already established for
every other Planner dependency).

Two concrete providers, one config switch (EMBEDDING_PROVIDER):
  "local"  sentence-transformers, on-device, zero API cost, no key required
           — the default, so the test suite and CI never need a paid key.
  "voyage" Voyage AI's hosted embeddings API (Anthropic has no first-party
           embeddings endpoint; Voyage is Anthropic's commonly-recommended
           embeddings partner) — better retrieval quality, needs
           VOYAGE_API_KEY and costs real money per call, so it is never the
           default and this repo never calls it in a test.
"""

from __future__ import annotations

from typing import Protocol


class EmbeddingProviderUnavailable(Exception):
    """Raised by an EmbeddingProvider when it cannot serve this request right
    now — missing dependency, missing/invalid API key, rate-limited, network
    outage. Callers (retrieval/hybrid_search.py, retrieval/semantic_search.py,
    the embedding indexer worker) catch this ONE type regardless of which
    concrete provider is configured and degrade to FTS5/BM25-only (section 10)
    rather than let a provider-specific exception leak past this seam."""


class EmbeddingProvider(Protocol):
    """Every concrete provider satisfies this shape structurally — no base
    class needed, same minimal pattern llm/providers/base.py's
    Provider(Protocol) already uses for swappable LLM providers."""

    #: Stable identifier for what produced a vector (persisted alongside each
    #: chunk's embedding_model column and each vector's payload, so a future
    #: change of model/provider is auditable and re-embeddable rather than
    #: silently mixing incompatible vector spaces in one collection).
    model_id: str
    #: Vector width this provider produces — VectorStore implementations use
    #: this to size a new collection (retrieval/vector_store_qdrant.py).
    dimension: int

    def embed_text(self, text: str) -> list[float]:
        """Embed one piece of text (typically a search query). Raises
        EmbeddingProviderUnavailable rather than returning a zero/garbage
        vector when it cannot serve the request."""
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed many texts (typically a document's chunks) in one call —
        batched for throughput, not just a Python-level loop over
        embed_text(), though a provider MAY implement it that way. Returns
        vectors in the same order as `texts`; [] in, [] out."""
        ...


def default_embedding_provider() -> EmbeddingProvider:
    """The only place that imports a concrete provider module directly —
    everywhere else routes through the EmbeddingProvider seam (dependency
    injection, or this factory). Reads config.settings.EMBEDDING_PROVIDER at
    call time (not import time), same "read live" reasoning
    storage/database.py's get_connection docstring gives for settings.DB_PATH."""
    from config import settings

    provider = settings.EMBEDDING_PROVIDER
    if provider == "local":
        from retrieval.embedding_provider_local import LocalEmbeddingProvider

        return LocalEmbeddingProvider()
    if provider == "voyage":
        from retrieval.embedding_provider_voyage import VoyageEmbeddingProvider

        return VoyageEmbeddingProvider()
    raise ValueError(f"Unknown EMBEDDING_PROVIDER={provider!r} (expected 'local' or 'voyage')")
