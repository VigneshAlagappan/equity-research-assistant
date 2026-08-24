"""Company lifecycle: active <-> archived (README: Company Lifecycle / Archiving).

Archiving only flips metadata on the companies row — it never touches
observations, documents, or canonical data, so nothing is reconstructed on
restore. Ingestion checks status == active before running; that gate lives
here as assert_active(), even though today (manual ingestion, freshly
registered companies) it's effectively a no-op.
"""

from __future__ import annotations

import sqlite3

from normalization.companies import normalize_company_id
from storage.database import utcnow_iso

ARCHIVE_REASONS = {"delisted", "acquired", "merged", "renamed", "duplicate", "manual"}


class CompanyNotFoundError(ValueError):
    pass


class CompanyNotActiveError(ValueError):
    """Raised by the ingestion gate when a company is unknown or archived."""


class InvalidArchiveReasonError(ValueError):
    pass


def archive_company(conn: sqlite3.Connection, company_id: str, reason: str) -> None:
    if reason not in ARCHIVE_REASONS:
        raise InvalidArchiveReasonError(
            f"archive_reason must be one of {sorted(ARCHIVE_REASONS)}, got {reason!r}"
        )
    company_id = normalize_company_id(company_id)
    cursor = conn.execute(
        """
        UPDATE companies SET status = 'archived', archived_at = ?, archive_reason = ?, updated_at = ?
        WHERE company_id = ?
        """,
        (utcnow_iso(), reason, utcnow_iso(), company_id),
    )
    if cursor.rowcount == 0:
        raise CompanyNotFoundError(f"No company registered with company_id={company_id!r}")
    conn.commit()


def restore_company(conn: sqlite3.Connection, company_id: str) -> None:
    """Flip an archived company back to active. Observations/documents were never touched."""
    company_id = normalize_company_id(company_id)
    cursor = conn.execute(
        """
        UPDATE companies SET status = 'active', archived_at = NULL, archive_reason = NULL, updated_at = ?
        WHERE company_id = ?
        """,
        (utcnow_iso(), company_id),
    )
    if cursor.rowcount == 0:
        raise CompanyNotFoundError(f"No company registered with company_id={company_id!r}")
    conn.commit()


def assert_active(conn: sqlite3.Connection, company_id: str) -> None:
    """Ingestion gate: raise unless the company exists and is active."""
    company_id = normalize_company_id(company_id)
    row = conn.execute(
        "SELECT status FROM companies WHERE company_id = ?", (company_id,)
    ).fetchone()
    if row is None:
        raise CompanyNotActiveError(f"No company registered with company_id={company_id!r}")
    if row["status"] != "active":
        raise CompanyNotActiveError(
            f"Company {company_id!r} is {row['status']!r}, not active — ingestion refused"
        )
