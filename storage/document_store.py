"""DocumentStore — the backend-independent abstraction over "wherever a
document's raw bytes physically live" (narrative PDFs/audio under
config.settings.DOCUMENTS_DIR today; the AWS architecture review's
recommendation to make that swappable for S3 without touching every call
site). Mirrors retrieval/vector_store.py's Protocol + factory pattern —
read that module's docstring for the house style this follows:

  * A Protocol, not a base class (same minimal shape llm/providers/base.py's
    Provider(Protocol) and retrieval/vector_store.py's VectorStore use).
  * One factory (default_document_store()) is the only place that imports a
    concrete backend module directly — everywhere else (research/documents.py,
    web/app.py, ingestion/coordinator.py, sources/investor_relations.py)
    depends on this module's Protocol or receives a DocumentStore via
    dependency injection.
  * config.settings.DOCUMENT_STORE_BACKEND selects the backend, same
    optional-infra shape as GRAPH_BACKEND/VECTOR_STORE_BACKEND: "local"
    (default) needs no extra setup; "s3" opts into the real S3 bucket.

`key` is deliberately the SAME repo-relative string this codebase already
stores in documents.raw_file_path (e.g. "data/documents/AAPL/2026...__x.pdf")
— not a re-namespaced identifier. That means:
  * LocalDocumentStore is a pure refactor of the on-disk behaviour that
    existed before this module (config.settings.from_repo_relative(key) is
    exactly the path today's code already reads/writes) — zero behaviour
    change when DOCUMENT_STORE_BACKEND=local (the default).
  * S3DocumentStore uses the identical string as the S3 object key, so a
    document written under one backend is trivially identifiable under the
    other during a future migration (out of scope here — this module only
    proves the abstraction, it doesn't migrate data/documents/** to S3).

Only DOCUMENTS_DIR-rooted content (narrative PDFs/audio) routes through this
abstraction. RAW_DIR (financial XBRL source files) is deliberately left on
direct Path access elsewhere (ingestion/pipeline.py, sources/nse_xbrl.py) —
lower priority per the architecture review, already reduced to
financial_observations and never re-read at query time.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Protocol


class DocumentStoreError(Exception):
    """Raised by a DocumentStore method when the requested key can't be
    served — missing object, backend unreachable, permission failure. Every
    concrete backend translates its own SDK/OS-specific exception into this
    ONE type, same role VectorStoreUnavailable plays for
    retrieval/vector_store.py's VectorStore backends."""


class DocumentStore(Protocol):
    """Every concrete backend satisfies this shape structurally — no base
    class needed, same minimal pattern VectorStore(Protocol) already uses."""

    def store(self, key: str, content: bytes) -> str:
        """Write `content` under `key`, creating any intermediate structure
        the backend needs (local: parent directories; S3: nothing, keys are
        flat). Returns the storage_object_key actually used — always `key`
        itself for both backends today, but callers should persist the
        returned value rather than assuming it, in case a future backend
        ever needs to rewrite it (e.g. a content-addressed store)."""
        ...

    def retrieve(self, key: str) -> bytes:
        """Full bytes for `key`. Raises DocumentStoreError if it doesn't
        exist or can't be read — callers that treat a missing document as
        "no evidence" rather than an error should check exists() first."""
        ...

    def exists(self, key: str) -> bool:
        """Never raises — an unreachable backend or a missing key are both
        just False, same "absence isn't an error" convention this codebase
        already follows for FTS5/vector search misses."""
        ...

    def delete(self, key: str) -> None:
        """Removes `key` if present. A no-op (not an error) if it's already
        gone — same idempotent-delete convention as
        storage/repositories.py's delete_note_attachment() callers unlinking
        a file that may already be missing."""
        ...

    def metadata(self, key: str) -> dict:
        """{'size': int, 'content_type': str | None} for `key`. Raises
        DocumentStoreError if the key doesn't exist."""
        ...

    def presigned_url(self, key: str, expires_in: int = 3600) -> str | None:
        """A time-limited URL the browser can fetch `key` from directly,
        or None if this backend can't do that (local: always None — there's
        no separate serving layer, callers fall back to Flask's send_file).
        S3DocumentStore returns a real presigned GET URL, valid for
        `expires_in` seconds."""
        ...

    def stream(self, key: str):
        """A file-like/iterator over `key`'s bytes, for large files a caller
        doesn't want to fully buffer in memory. Raises DocumentStoreError if
        the key doesn't exist."""
        ...


class LocalDocumentStore:
    """Wraps today's exact on-disk behaviour — `key` is the same
    repo-relative string already stored in documents.raw_file_path, resolved
    via config.settings.from_repo_relative exactly like every pre-existing
    direct-disk-access call site did. Selected by DOCUMENT_STORE_BACKEND=local
    (the default), so existing local databases/deployments see zero change."""

    def _path(self, key: str) -> Path:
        from config.settings import from_repo_relative

        return from_repo_relative(key)

    def store(self, key: str, content: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return key

    def retrieve(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            raise DocumentStoreError(f"cannot read {key!r}: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def metadata(self, key: str) -> dict:
        path = self._path(key)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise DocumentStoreError(f"cannot stat {key!r}: {exc}") from exc
        content_type, _ = mimetypes.guess_type(path.name)
        return {"size": size, "content_type": content_type}

    def presigned_url(self, key: str, expires_in: int = 3600) -> str | None:
        return None

    def stream(self, key: str):
        path = self._path(key)
        try:
            return path.open("rb")
        except OSError as exc:
            raise DocumentStoreError(f"cannot open {key!r}: {exc}") from exc


class S3DocumentStore:
    """Real boto3-backed implementation against the S3 bucket named by
    config.settings.S3_BUCKET_NAME. `key` is used verbatim as the S3 object
    key — the same repo-relative string LocalDocumentStore resolves to a
    local path, so a document's identity doesn't change shape when
    DOCUMENT_STORE_BACKEND flips from "local" to "s3".

    boto3 resolves credentials from the environment
    (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or any other credential source
    in its normal chain) — this class never handles secrets directly, same
    "capability, not a credential" separation the rest of this codebase's
    optional-infra backends (Neo4j, Qdrant) already follow."""

    def __init__(self, bucket: str | None = None, region_name: str | None = None):
        import boto3
        from config import settings

        self._bucket = bucket or settings.S3_BUCKET_NAME
        self._client = boto3.client("s3", region_name=region_name or settings.S3_REGION_NAME)

    def store(self, key: str, content: bytes) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        content_type, _ = mimetypes.guess_type(key)
        extra_args = {"ContentType": content_type} if content_type else {}
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=content, **extra_args)
        except (BotoCoreError, ClientError) as exc:
            raise DocumentStoreError(f"cannot write {key!r} to s3://{self._bucket}: {exc}") from exc
        return key

    def retrieve(self, key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise DocumentStoreError(f"cannot read {key!r} from s3://{self._bucket}: {exc}") from exc

    def exists(self, key: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except (BotoCoreError, ClientError):
            return False

    def delete(self, key: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise DocumentStoreError(f"cannot delete {key!r} from s3://{self._bucket}: {exc}") from exc

    def metadata(self, key: str) -> dict:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise DocumentStoreError(f"cannot stat {key!r} in s3://{self._bucket}: {exc}") from exc
        return {"size": response.get("ContentLength"), "content_type": response.get("ContentType")}

    def presigned_url(self, key: str, expires_in: int = 3600) -> str | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return self._client.generate_presigned_url(
                "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError) as exc:
            raise DocumentStoreError(f"cannot presign {key!r} in s3://{self._bucket}: {exc}") from exc

    def stream(self, key: str):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"]
        except (BotoCoreError, ClientError) as exc:
            raise DocumentStoreError(f"cannot stream {key!r} from s3://{self._bucket}: {exc}") from exc


def default_document_store() -> DocumentStore:
    """The only place that imports a concrete backend module directly —
    everywhere else routes through the DocumentStore seam (dependency
    injection, or this factory). Reads settings at call time, not import
    time, so tests can monkeypatch the backend choice — same convention
    retrieval/vector_store.py's default_vector_store() follows."""
    from config import settings

    backend = settings.DOCUMENT_STORE_BACKEND
    if backend == "local":
        return LocalDocumentStore()
    if backend == "s3":
        return S3DocumentStore()
    raise ValueError(f"Unknown DOCUMENT_STORE_BACKEND={backend!r} (expected 'local' or 's3')")
