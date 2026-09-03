"""Voyage AI-backed EmbeddingProvider — the optional, paid, hosted embeddings
path (config.settings.EMBEDDING_PROVIDER="voyage"). Anthropic has no
first-party embeddings API; Voyage AI is Anthropic's commonly-recommended
embeddings partner, so it's the second concrete provider alongside the local
default (retrieval/embedding_provider_local.py).

The ONLY module in this codebase allowed to import voyageai — mirrors
retrieval/embedding_provider_local.py (sentence-transformers) and
retrieval/vector_store_qdrant.py (Qdrant) in keeping a specific third-party
SDK behind exactly one seam. Never selected by default and never exercised
against the real API in this repo's test suite — it needs a real
VOYAGE_API_KEY and spends real money per call, so wiring it up for actual use
is a decision only whoever holds that key can make (see this feature's final
report: "deferred to the user").

The `voyageai` package is imported lazily inside __init__(), not at module
top level — so this module stays importable (and its "not installed"/"no
key" error paths stay testable) even in an environment that has never run
`pip install voyageai`.
"""

from __future__ import annotations

from retrieval.embedding_provider import EmbeddingProviderUnavailable

#: voyage-3-lite's published output width. Voyage returns this dynamically
#: per response too, but VectorStore collection sizing needs it up front.
_DIMENSIONS_BY_MODEL = {
    "voyage-3-lite": 512,
    "voyage-3": 1024,
    "voyage-3-large": 1024,
}
_DEFAULT_DIMENSION = 512

#: Voyage documents a request-size cap; batch client-side rather than let a
#: large backfill's single call fail outright.
_MAX_BATCH_SIZE = 128


class VoyageEmbeddingProvider:
    """EmbeddingProvider backed by Voyage AI's hosted embeddings API —
    satisfies retrieval.embedding_provider.EmbeddingProvider structurally."""

    def __init__(self, model_id: str | None = None, api_key: str | None = None) -> None:
        from config import settings

        self.model_id = model_id or settings.EMBEDDING_MODEL_VOYAGE
        self.dimension = _DIMENSIONS_BY_MODEL.get(self.model_id, _DEFAULT_DIMENSION)
        key = api_key or settings.VOYAGE_API_KEY
        if not key:
            raise EmbeddingProviderUnavailable(
                "VOYAGE_API_KEY is not set — set it in the environment or switch "
                "EMBEDDING_PROVIDER back to 'local' to use the zero-cost on-device provider."
            )
        try:
            import voyageai
        except ImportError as exc:
            raise EmbeddingProviderUnavailable(
                "voyageai is not installed — run `pip install voyageai` or set "
                "EMBEDDING_PROVIDER=local to use the on-device provider instead."
            ) from exc
        self._client = voyageai.Client(api_key=key)

    def embed_text(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), _MAX_BATCH_SIZE):
                batch = texts[start:start + _MAX_BATCH_SIZE]
                result = self._client.embed(batch, model=self.model_id, input_type="document")
                vectors.extend(result.embeddings)
        except Exception as exc:  # noqa: BLE001 - any API/network failure from the Voyage SDK
            raise EmbeddingProviderUnavailable(f"Voyage embeddings request failed: {exc}") from exc
        return vectors
