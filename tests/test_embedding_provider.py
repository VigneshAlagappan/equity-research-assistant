"""retrieval/embedding_provider.py — the EmbeddingProvider abstraction and
its default() factory (section 2, section 14: "EmbeddingProvider is
independent of VectorStore"). Uses the real local (sentence-transformers)
provider — no mocking needed, no API key, no network beyond the one-time
model download already cached on this machine — plus a config-switch test
for the factory itself."""

from __future__ import annotations

import pytest

from retrieval.embedding_provider import EmbeddingProviderUnavailable, default_embedding_provider
from retrieval.embedding_provider_local import LocalEmbeddingProvider
from retrieval.embedding_provider_voyage import VoyageEmbeddingProvider


def test_local_provider_produces_correctly_sized_vectors() -> None:
    provider = LocalEmbeddingProvider()
    vector = provider.embed_text("Revenue grew twelve percent this quarter")
    assert len(vector) == provider.dimension
    assert all(isinstance(v, float) for v in vector)


def test_local_provider_embed_batch_preserves_order_and_count() -> None:
    provider = LocalEmbeddingProvider()
    texts = ["net interest margin expanded", "loan growth accelerated", "deposits declined"]
    vectors = provider.embed_batch(texts)
    assert len(vectors) == len(texts)
    # embed_text on the same input reproduces (up to batching's floating-point
    # rounding differences) embed_batch's corresponding vector.
    solo = provider.embed_text(texts[1])
    assert all(a == pytest.approx(b, abs=1e-4) for a, b in zip(vectors[1], solo))


def test_local_provider_embed_batch_empty_list_returns_empty() -> None:
    assert LocalEmbeddingProvider().embed_batch([]) == []


def test_local_provider_is_independent_of_any_vector_store() -> None:
    """EmbeddingProvider never IMPORTS VectorStore/a vector-database SDK —
    the module doesn't depend on a vector database existing (section 2).
    Checked by import statement, not bare substring, since the module's own
    prose docstring legitimately mentions retrieval/vector_store_qdrant.py by
    name when explaining the parallel abstraction it mirrors."""
    import ast

    import retrieval.embedding_provider_local as module

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(m == "qdrant_client" or m.startswith("qdrant_client.") for m in imported_modules)
    assert not any(m.startswith("retrieval.vector_store") for m in imported_modules)


def test_default_embedding_provider_selects_local_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.settings.EMBEDDING_PROVIDER", "local")
    provider = default_embedding_provider()
    assert isinstance(provider, LocalEmbeddingProvider)


def test_default_embedding_provider_selects_voyage_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.settings.EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr("config.settings.VOYAGE_API_KEY", "")  # no key configured in this test env
    with pytest.raises(EmbeddingProviderUnavailable):
        default_embedding_provider()


def test_default_embedding_provider_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.settings.EMBEDDING_PROVIDER", "not-a-real-provider")
    with pytest.raises(ValueError):
        default_embedding_provider()


def test_voyage_provider_without_key_raises_embedding_provider_unavailable() -> None:
    with pytest.raises(EmbeddingProviderUnavailable):
        VoyageEmbeddingProvider(api_key="")


def test_voyage_provider_with_key_but_no_package_raises_typed_error() -> None:
    """This repo's test suite never installs voyageai (section 2's cost/CI
    guardrail — no paid provider dependency required to run tests), so a key
    present with the package absent must fail with the typed,
    pluggability-preserving error — never a bare ImportError leaking past
    this seam."""
    with pytest.raises(EmbeddingProviderUnavailable):
        VoyageEmbeddingProvider(api_key="fake-key-for-test")
