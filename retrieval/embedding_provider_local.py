"""Local, zero-API-cost EmbeddingProvider — sentence-transformers running
entirely on-device. The default (config.settings.EMBEDDING_PROVIDER="local")
precisely so the test suite and this repo's CI never need a paid embeddings
key (retrieval/embedding_provider.py's module docstring).

The ONLY module in this codebase allowed to import sentence_transformers —
mirrors retrieval/embedding_provider_voyage.py (the other concrete provider)
and retrieval/vector_store_qdrant.py (the concrete VectorStore) in keeping a
specific third-party SDK behind exactly one seam.

Model: "sentence-transformers/all-MiniLM-L6-v2" by default
(config.settings.EMBEDDING_MODEL_LOCAL) — a small (384-dim), fast,
general-purpose sentence embedding model, good enough to demonstrate real
paraphrase/synonym retrieval (README validation exercise) without the size or
latency of a larger model. The underlying torch model is loaded once per
process (module-level cache keyed by model_id) since loading it is the
expensive part, not encoding a handful of chunks.
"""

from __future__ import annotations

from retrieval.embedding_provider import EmbeddingProviderUnavailable

#: model_id -> loaded SentenceTransformer instance. Process-wide cache: the
#: model is 80-100MB of weights, expensive to load and safe to share across
#: every LocalEmbeddingProvider instance in this process (the model itself is
#: stateless/side-effect-free per call).
_MODEL_CACHE: dict[str, object] = {}

#: sentence-transformers/all-MiniLM-L6-v2's known output width. Only used as
#: a fallback if introspecting the loaded model's own dimension fails.
_DEFAULT_DIMENSION = 384


def _load_model(model_id: str):
    if model_id in _MODEL_CACHE:
        return _MODEL_CACHE[model_id]
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - exercised only when the optional dep is missing
        raise EmbeddingProviderUnavailable(
            "sentence-transformers is not installed — run `pip install sentence-transformers` "
            "or set EMBEDDING_PROVIDER=voyage to use a hosted provider instead."
        ) from exc
    try:
        model = SentenceTransformer(model_id)
    except Exception as exc:  # noqa: BLE001 - e.g. no network to fetch the model on first use
        raise EmbeddingProviderUnavailable(f"Could not load local embedding model {model_id!r}: {exc}") from exc
    _MODEL_CACHE[model_id] = model
    return model


class LocalEmbeddingProvider:
    """EmbeddingProvider backed by a local sentence-transformers model —
    satisfies retrieval.embedding_provider.EmbeddingProvider structurally."""

    def __init__(self, model_id: str | None = None) -> None:
        from config import settings

        self.model_id = model_id or settings.EMBEDDING_MODEL_LOCAL
        self._model = _load_model(self.model_id)
        self.dimension = self._introspect_dimension()

    def _introspect_dimension(self) -> int:
        # get_embedding_dimension() is the current sentence-transformers API;
        # get_sentence_embedding_dimension() is its deprecated predecessor,
        # kept as a fallback for older installed versions. Either failing
        # falls back to the known default rather than failing construction.
        for method_name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
            method = getattr(self._model, method_name, None)
            if method is not None:
                try:
                    return int(method())
                except Exception:  # noqa: BLE001
                    continue
        return _DEFAULT_DIMENSION

    def embed_text(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors = self._model.encode(list(texts), convert_to_numpy=True, show_progress_bar=False)
        except Exception as exc:  # noqa: BLE001 - torch/runtime failure mid-encode
            raise EmbeddingProviderUnavailable(f"Local embedding encode failed: {exc}") from exc
        return [vector.tolist() for vector in vectors]
