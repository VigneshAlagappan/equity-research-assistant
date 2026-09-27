"""config/settings.py's _warn_if_document_store_misconfigured -- a real
drift found live (2026-09-17): DATABASE_BACKEND=postgres pointed at the
real NEON credential with no explicit DOCUMENT_STORE_BACKEND=s3 silently
falls back to local-disk documents while every DB write still lands in the
real shared database -- 7 of 8 broken generated_reports.s3_key rows traced
to exactly this. Calls the function directly rather than reloading
config.settings itself -- that module is imported by nearly everything
else in this app, and other modules cache references to its constants that
importlib.reload() would silently leave stale."""

from __future__ import annotations

import logging

from config.settings import _warn_if_document_store_misconfigured


def test_warns_when_postgres_points_at_neon_without_s3(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="config.settings"):
        _warn_if_document_store_misconfigured("postgres", None, "local")

    assert any("DOCUMENT_STORE_BACKEND" in r.message for r in caplog.records)


def test_silent_when_document_store_is_s3(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="config.settings"):
        _warn_if_document_store_misconfigured("postgres", None, "s3")

    assert caplog.records == []


def test_silent_when_pointed_at_local_dev_postgres(caplog) -> None:
    # A local Postgres + local documents dev workflow is legitimate --
    # LOCAL_DEV_DATABASE_URL being set is what distinguishes it from real
    # cloud-DB-plus-local-disk drift.
    with caplog.at_level(logging.WARNING, logger="config.settings"):
        _warn_if_document_store_misconfigured(
            "postgres", "postgresql://signals_test:signals_test@localhost:5433/signals_dev", "local",
        )

    assert caplog.records == []


def test_silent_on_default_sqlite_backend(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="config.settings"):
        _warn_if_document_store_misconfigured("sqlite", None, "local")

    assert caplog.records == []
