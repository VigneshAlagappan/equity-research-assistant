"""storage/investigation_repository.py — the one-investigation-to-many-
companies association behind "Company -> Investigations".

The behaviour under test is specifically the spec's cross-company rule: a
comparison investigation must appear under EVERY company it covers, from a
single shared record, never duplicated per company.
"""

from __future__ import annotations

import sqlite3

from companies.registry import seed_companies
from storage.investigation_repository import (
    backfill_investigation_companies,
    count_investigation_hypotheses,
    select_company_ids_for_investigation,
    select_investigations_for_company,
)
from storage.repositories import save_investigation, save_investigation_hypothesis


def _save(conn: sqlite3.Connection, investigation_id: str, company_ids: list[str], **kwargs) -> None:
    save_investigation(
        conn, investigation_id=investigation_id, question=f"Why {investigation_id}?",
        company_ids=company_ids, statement_type="consolidated", strongest_explanation=None,
        unanswered_questions=[], additional_evidence_needed=[], **kwargs,
    )


def test_a_cross_company_investigation_is_one_record_listed_under_each_company(
    db_conn: sqlite3.Connection,
) -> None:
    seed_companies(db_conn)
    _save(db_conn, "inv_pair", ["HDFCBANK", "ICICIBANK"])

    for company_id in ("HDFCBANK", "ICICIBANK"):
        rows = select_investigations_for_company(db_conn, company_id)
        assert [r["investigation_id"] for r in rows] == ["inv_pair"]

    # ...and exactly one underlying investigation row, not one per company.
    assert db_conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 1


def test_a_single_company_investigation_gets_exactly_one_association(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    _save(db_conn, "inv_solo", ["HDFCBANK"])

    assert select_company_ids_for_investigation(db_conn, "inv_solo") == ["HDFCBANK"]
    assert select_investigations_for_company(db_conn, "ICICIBANK") == []


def test_association_order_follows_the_question_as_asked(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    _save(db_conn, "inv_order", ["ICICIBANK", "HDFCBANK"])
    assert select_company_ids_for_investigation(db_conn, "inv_order") == ["ICICIBANK", "HDFCBANK"]


def test_a_repeated_company_id_does_not_break_the_save(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    _save(db_conn, "inv_dupe", ["HDFCBANK", "HDFCBANK"])
    assert select_company_ids_for_investigation(db_conn, "inv_dupe") == ["HDFCBANK"]


def test_investigations_are_listed_newest_first(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    _save(db_conn, "inv_old", ["HDFCBANK"])
    db_conn.execute("UPDATE investigations SET generated_at = '2020-01-01T00:00:00Z' WHERE investigation_id = 'inv_old'")
    _save(db_conn, "inv_new", ["HDFCBANK"])

    rows = select_investigations_for_company(db_conn, "HDFCBANK")
    assert [r["investigation_id"] for r in rows] == ["inv_new", "inv_old"]


def test_as_of_round_trips_onto_the_investigation_row(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    _save(db_conn, "inv_hist", ["HDFCBANK"], as_of="2013-03-31")
    row = db_conn.execute(
        "SELECT as_of FROM investigations WHERE investigation_id = 'inv_hist'"
    ).fetchone()
    assert row["as_of"] == "2013-03-31"


def test_backfill_is_idempotent_and_recovers_pre_join_table_investigations(
    db_conn: sqlite3.Connection,
) -> None:
    """An investigation saved before the join table existed still has its JSON
    company_ids — the backfill must recover it (and running twice must not
    duplicate)."""
    seed_companies(db_conn)
    _save(db_conn, "inv_legacy", ["HDFCBANK", "ICICIBANK"])
    db_conn.execute("DELETE FROM investigation_companies WHERE investigation_id = 'inv_legacy'")

    assert select_investigations_for_company(db_conn, "HDFCBANK") == []
    assert backfill_investigation_companies(db_conn) == 1
    assert select_company_ids_for_investigation(db_conn, "inv_legacy") == ["HDFCBANK", "ICICIBANK"]
    assert backfill_investigation_companies(db_conn) == 0


def test_backfill_skips_a_company_id_with_no_companies_row(db_conn: sqlite3.Connection) -> None:
    """A since-deleted company must not abort the whole backfill — the
    investigation keeps its other associations."""
    seed_companies(db_conn)
    _save(db_conn, "inv_ghost", ["HDFCBANK"])
    db_conn.execute("DELETE FROM investigation_companies WHERE investigation_id = 'inv_ghost'")
    db_conn.execute(
        "UPDATE investigations SET company_ids = '[\"HDFCBANK\", \"DELISTEDCO\"]' "
        "WHERE investigation_id = 'inv_ghost'"
    )
    backfill_investigation_companies(db_conn)
    assert select_company_ids_for_investigation(db_conn, "inv_ghost") == ["HDFCBANK"]


def test_hypothesis_count_is_scoped_to_its_own_investigation(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    _save(db_conn, "inv_a", ["HDFCBANK"])
    _save(db_conn, "inv_b", ["HDFCBANK"])
    for n in (1, 2, 3):
        save_investigation_hypothesis(
            db_conn, hypothesis_id=f"inv_a-h{n}", investigation_id="inv_a", statement="s",
            mechanism=None, category="financial", rationale=None, unknowns=[], generation_order=n,
        )
    assert count_investigation_hypotheses(db_conn, "inv_a") == 3
    assert count_investigation_hypotheses(db_conn, "inv_b") == 0
