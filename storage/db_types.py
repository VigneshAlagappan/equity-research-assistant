"""Backend-agnostic type aliases for the database-access layer.

Every function outside `storage/` that takes a `conn` parameter should type
it as `DBConnection`, not `sqlite3.Connection` — and a row it reads back as
`Row`, not `sqlite3.Row`. sqlite3's own connection/row objects already
satisfy these shapes structurally, so this costs nothing today; it just
means research/context/web/financials/etc. no longer need to `import
sqlite3` just to type-hint a parameter, and a future swap to a different
DB-API 2.0 driver (e.g. psycopg2) only requires touching the modules that
actually construct connections or write backend-specific SQL (`storage/`
itself), not every caller's type hints.

`storage/*.py` (the implementation layer) is exactly where importing
`sqlite3` directly, catching `sqlite3.IntegrityError`, and calling
`sqlite3.connect()` still belongs — those files ARE the swappable part.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class DBConnection(Protocol):
    """Structural stand-in for whatever connection object the active
    backend hands back. Every DB-API 2.0 connection (sqlite3.Connection,
    psycopg2's connection, ...) already implements this."""

    def execute(self, sql: str, parameters: Any = ...) -> Any: ...
    def executemany(self, sql: str, parameters: Any) -> Any: ...
    def executescript(self, sql: str) -> Any: ...
    def commit(self) -> None: ...
    def cursor(self) -> Any: ...
    def close(self) -> None: ...


#: A single fetched row. sqlite3.Row (dict-and-index access, `.keys()`)
#: satisfies this loosely; it's a documentation-level alias, not a runtime
#: contract enforced anywhere.
Row = Mapping[str, Any]
