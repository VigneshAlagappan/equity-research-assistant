"""One-time repair for a Qdrant collection created before
retrieval/vector_store_qdrant.py's _ensure_collection() started creating
company_id/document_id payload indexes at collection-creation time.

Without these indexes, every search()/count() call filtering on company_id
and every delete_document() call filtering on document_id fails outright
with "400 Bad Request: Index required but not found for <field>" on this
Qdrant Cloud tier (confirmed against production 2026-09-13/14 -- the
company_id gap is what was silently eating ~70s of every Ask AI request
before falling through to a broken Neo4j fallback and finally a gunicorn
WORKER TIMEOUT; the document_id gap separately made chunk_and_index_
document()'s delete-before-reinsert step fail, so new documents' chunks
never reached Qdrant even once chunking/extraction itself succeeded). New
collections no longer need this script; it exists only because the
collection already had ~97k points and neither index by the time the bugs
were found.

Idempotent -- create_payload_index on a field that's already indexed is a
no-op from Qdrant's side (it returns/updates the same index), so running
this more than once is harmless.

Goes through retrieval.vector_store_qdrant.QdrantVectorStore's own
ensure_payload_indexes() rather than importing qdrant_client directly --
that module is the ONLY one allowed to import the SDK (its own module
docstring, enforced by tests/test_vector_store_architecture.py).

Usage: python -m scripts.ensure_qdrant_company_index
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from retrieval.vector_store_qdrant import QdrantVectorStore


def main() -> None:
    store = QdrantVectorStore()
    payload_schema = store.ensure_payload_indexes()
    if not payload_schema:
        print("Collection does not exist yet -- nothing to repair.")
        return
    print(f"Done. payload_schema={payload_schema}")


if __name__ == "__main__":
    main()
