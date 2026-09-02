"""Company lifecycle: active <-> archived (README: Company Lifecycle / Archiving).

Archiving only flips metadata on the companies row — it never touches
observations, documents, or canonical data, so nothing is reconstructed on
restore. Ingestion checks status == active before running; that gate lives
here as assert_active(), even though today (manual ingestion, freshly
registered companies) it's effectively a no-op.
"""

from __future__ import annotations

from normalization.companies import normalize_company_id
from storage import company_repository as repo
from storage.database import utcnow_iso
from storage.db_types import DBConnection

ARCHIVE_REASONS = {"delisted", "acquired", "merged", "renamed", "duplicate", "manual"}


class CompanyNotFoundError(ValueError):
    pass


class CompanyNotActiveError(ValueError):
    """Raised by the ingestion gate when a company is unknown or archived."""


class InvalidArchiveReasonError(ValueError):
    pass


def archive_company(conn: DBConnection, company_id: str, reason: str) -> None:
    if reason not in ARCHIVE_REASONS:
        raise InvalidArchiveReasonError(
            f"archive_reason must be one of {sorted(ARCHIVE_REASONS)}, got {reason!r}"
        )
    company_id = normalize_company_id(company_id)
    rowcount = repo.update_company_lifecycle_status(
        conn, company_id, status="archived", archived_at=utcnow_iso(), archive_reason=reason, now=utcnow_iso(),
    )
    if rowcount == 0:
        raise CompanyNotFoundError(f"No company registered with company_id={company_id!r}")


def restore_company(conn: DBConnection, company_id: str) -> None:
    """Flip an archived company back to active. Observations/documents were never touched."""
    company_id = normalize_company_id(company_id)
    rowcount = repo.update_company_lifecycle_status(
        conn, company_id, status="active", archived_at=None, archive_reason=None, now=utcnow_iso(),
    )
    if rowcount == 0:
        raise CompanyNotFoundError(f"No company registered with company_id={company_id!r}")


def assert_active(conn: DBConnection, company_id: str) -> None:
    """Ingestion gate: raise unless the company exists and is active."""
    company_id = normalize_company_id(company_id)
    row = repo.select_company_status(conn, company_id)
    if row is None:
        raise CompanyNotActiveError(f"No company registered with company_id={company_id!r}")
    if row["status"] != "active":
        raise CompanyNotActiveError(
            f"Company {company_id!r} is {row['status']!r}, not active — ingestion refused"
        )
