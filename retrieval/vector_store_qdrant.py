"""Qdrant-backed VectorStore — the concrete implementation selected by
config.settings.VECTOR_STORE_BACKEND="qdrant" (the default). This is the
ONLY module in this codebase allowed to import qdrant_client
(retrieval/vector_store.py's module docstring, STRICT RULE) — every payload
key, filter shape, and collection-naming decision Qdrant-specific lives here
and nowhere else.

Local setup (not managed by this app — start it yourself, same as Neo4j/
Ollama, README §20):
    docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant
Then set QDRANT_URL (defaults to http://localhost:6333) if it's running
somewhere other than localhost.

The collection is created lazily, on first upsert, sized to whatever
embedding dimension that first batch of vectors carries — there is no
migration step to run by hand. Every method translates qdrant_client's own
exceptions (connection errors, timeouts) into VectorStoreUnavailable, so
retrieval/hybrid_search.py never needs to know Qdrant is the backend in
order to degrade gracefully (section 10) when it isn't reachable.
"""

from __future__ import annotations

from retrieval.vector_store import VectorMatch, VectorRecord, VectorStoreUnavailable


class QdrantVectorStore:
    """VectorStore backed by a Qdrant collection — satisfies
    retrieval.vector_store.VectorStore structurally."""

    def __init__(self, *, url: str | None = None, collection: str | None = None, timeout: float | None = None) -> None:
        from config import settings

        self._url = url or settings.QDRANT_URL
        self._collection = collection or settings.QDRANT_COLLECTION
        self._timeout = timeout if timeout is not None else settings.QDRANT_TIMEOUT_SECONDS
        self._client = None  # lazy — constructing a QdrantClient doesn't itself prove connectivity

    def _get_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=self._url, timeout=self._timeout)
        return self._client

    def health_check(self) -> bool:
        try:
            self._get_client().get_collections()
            return True
        except Exception:  # noqa: BLE001 - any connectivity/auth failure means "not healthy", never a raise
            return False

    def _collection_exists(self, client) -> bool:
        return any(c.name == self._collection for c in client.get_collections().collections)

    def _ensure_collection(self, client, dimension: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        if not self._collection_exists(client):
            client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
            )

    def upsert(self, records: list[VectorRecord]) -> None:
        if not records:
            return
        try:
            from qdrant_client.models import PointStruct

            client = self._get_client()
            self._ensure_collection(client, len(records[0].embedding))
            points = [
                PointStruct(
                    id=record.chunk_id,
                    vector=record.embedding,
                    payload={
                        "document_id": record.document_id,
                        "company_id": record.company_id,
                        "page_number": record.page_number,
                        "document_type": record.document_type,
                        "fiscal_year": record.fiscal_year,
                        "quarter": record.quarter,
                        "source": record.source,
                        "published_at": record.published_at,
                    },
                )
                for record in records
            ]
            client.upsert(collection_name=self._collection, points=points)
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - qdrant_client raises its own connection/response types
            raise VectorStoreUnavailable(f"Qdrant upsert failed: {exc}") from exc

    def delete_document(self, document_id: int) -> None:
        try:
            from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

            client = self._get_client()
            if not self._collection_exists(client):
                return  # nothing has ever been indexed — deleting is a no-op, not an error
            client.delete(
                collection_name=self._collection,
                points_selector=FilterSelector(
                    filter=Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))])
                ),
            )
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(f"Qdrant delete_document failed: {exc}") from exc

    def search(
        self, query_embedding: list[float], *, company_id: str | None = None, limit: int = 10
    ) -> list[VectorMatch]:
        try:
            client = self._get_client()
            if not self._collection_exists(client):
                return []
            query_filter = None
            if company_id is not None:
                from qdrant_client.models import FieldCondition, Filter, MatchValue

                query_filter = Filter(must=[FieldCondition(key="company_id", match=MatchValue(value=company_id))])
            response = client.query_points(
                collection_name=self._collection,
                query=query_embedding,
                query_filter=query_filter,
                limit=limit,
                with_payload=False,
            )
            return [VectorMatch(chunk_id=int(point.id), score=float(point.score)) for point in response.points]
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(f"Qdrant search failed: {exc}") from exc
