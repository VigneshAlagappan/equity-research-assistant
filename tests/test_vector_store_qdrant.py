"""retrieval/vector_store_qdrant.py — the concrete VectorStore implementation.
Exercised against a fake in-memory stand-in for qdrant_client.QdrantClient
(not a real Qdrant server — this repo's test suite must never depend on
Docker/a running service, same reasoning tests/test_graph_neo4j.py avoids a
real Neo4j server), which is enough to prove the upsert/delete/search/
health_check translation logic is correct and that connectivity failures
become VectorStoreUnavailable rather than a qdrant-specific exception
leaking out."""

from __future__ import annotations

import pytest

from retrieval.vector_store import VectorRecord, VectorStoreUnavailable
from retrieval.vector_store_qdrant import QdrantVectorStore


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class _FakeCollectionDesc:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCollectionsResponse:
    def __init__(self, names: list[str]) -> None:
        self.collections = [_FakeCollectionDesc(n) for n in names]


class _FakeScoredPoint:
    def __init__(self, id_: int, score: float) -> None:
        self.id = id_
        self.score = score


class _FakeQueryResponse:
    def __init__(self, points: list[_FakeScoredPoint]) -> None:
        self.points = points


class FakeQdrantClient:
    """Stands in for qdrant_client.QdrantClient — in-memory, exposing only
    the methods QdrantVectorStore actually calls."""

    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self.url = url
        self.unreachable = False
        self._collections: dict[str, dict[int, tuple[list[float], dict]]] = {}

    def get_collections(self):
        if self.unreachable:
            raise ConnectionError("fake: qdrant unreachable")
        return _FakeCollectionsResponse(list(self._collections))

    def create_collection(self, collection_name, vectors_config):
        self._collections.setdefault(collection_name, {})
        return True

    def upsert(self, collection_name, points):
        if self.unreachable:
            raise ConnectionError("fake: qdrant unreachable")
        collection = self._collections.setdefault(collection_name, {})
        for point in points:
            collection[point.id] = (point.vector, point.payload)

    def delete(self, collection_name, points_selector):
        if self.unreachable:
            raise ConnectionError("fake: qdrant unreachable")
        collection = self._collections.get(collection_name, {})
        condition = points_selector.filter.must[0]
        to_delete = [pid for pid, (_, payload) in collection.items() if payload.get(condition.key) == condition.match.value]
        for pid in to_delete:
            del collection[pid]

    def query_points(self, collection_name, query, query_filter=None, limit=10, with_payload=True):
        if self.unreachable:
            raise ConnectionError("fake: qdrant unreachable")
        collection = self._collections.get(collection_name, {})
        items = list(collection.items())
        if query_filter is not None:
            condition = query_filter.must[0]
            items = [(pid, v) for pid, v in items if v[1].get(condition.key) == condition.match.value]
        scored = [_FakeScoredPoint(pid, _cosine(query, vector)) for pid, (vector, _payload) in items]
        scored.sort(key=lambda p: p.score, reverse=True)
        return _FakeQueryResponse(scored[:limit])


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeQdrantClient:
    client = FakeQdrantClient()
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda *args, **kwargs: client)
    return client


def test_health_check_true_when_reachable(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="test_coll")
    assert store.health_check() is True


def test_health_check_false_when_unreachable(fake_client: FakeQdrantClient) -> None:
    fake_client.unreachable = True
    store = QdrantVectorStore(collection="test_coll")
    assert store.health_check() is False


def test_upsert_then_search_returns_the_match(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="test_coll")
    store.upsert([VectorRecord(chunk_id=1, document_id=10, company_id="ACME", embedding=[1.0, 0.0, 0.0])])

    results = store.search([1.0, 0.0, 0.0], limit=5)

    assert len(results) == 1
    assert results[0].chunk_id == 1


def test_search_scoped_to_company(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="test_coll")
    store.upsert([
        VectorRecord(chunk_id=1, document_id=10, company_id="ACME", embedding=[1.0, 0.0]),
        VectorRecord(chunk_id=2, document_id=11, company_id="OTHER", embedding=[1.0, 0.0]),
    ])

    results = store.search([1.0, 0.0], company_id="ACME", limit=5)

    assert [r.chunk_id for r in results] == [1]


def test_delete_document_removes_only_its_own_vectors(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="test_coll")
    store.upsert([
        VectorRecord(chunk_id=1, document_id=10, company_id="ACME", embedding=[1.0, 0.0]),
        VectorRecord(chunk_id=2, document_id=10, company_id="ACME", embedding=[0.0, 1.0]),
        VectorRecord(chunk_id=3, document_id=99, company_id="ACME", embedding=[1.0, 1.0]),
    ])

    store.delete_document(10)

    results = store.search([1.0, 1.0], limit=10)
    assert {r.chunk_id for r in results} == {3}


def test_search_returns_empty_for_never_created_collection(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="never_used")
    assert store.search([1.0, 0.0], limit=5) == []


def test_delete_document_is_a_noop_for_never_created_collection(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="never_used")
    store.delete_document(1)  # must not raise


def test_upsert_empty_list_is_a_noop(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="test_coll")
    store.upsert([])  # must not raise, must not construct a client/collection
    assert store.search([1.0, 0.0], limit=5) == []


def test_search_raises_vector_store_unavailable_when_unreachable(fake_client: FakeQdrantClient) -> None:
    store = QdrantVectorStore(collection="test_coll")
    store.upsert([VectorRecord(chunk_id=1, document_id=10, company_id="ACME", embedding=[1.0, 0.0])])
    fake_client.unreachable = True

    with pytest.raises(VectorStoreUnavailable):
        store.search([1.0, 0.0], limit=5)


def test_upsert_raises_vector_store_unavailable_when_unreachable(fake_client: FakeQdrantClient) -> None:
    fake_client.unreachable = True
    store = QdrantVectorStore(collection="test_coll")

    with pytest.raises(VectorStoreUnavailable):
        store.upsert([VectorRecord(chunk_id=1, document_id=10, company_id="ACME", embedding=[1.0, 0.0])])
