"""research_cases as the single Cases container: origin, soft delete/hide, and
canonical company tags (case_companies) that never duplicate a case."""
from __future__ import annotations

from pathlib import Path

from storage.database import init_db
from storage.repositories import (
    add_case_company,
    create_research_case,
    delete_research_case,
    get_research_case,
    hide_research_case,
    list_case_company_ids,
    list_cases_for_company,
    remove_case_company,
    soft_delete_research_case,
    unhide_research_case,
)


def _conn(tmp_path: Path):
    conn = init_db(db_path=tmp_path / "signals_data.db")
    for company_id in ("HDFCBANK", "ICICIBANK", "AXISBANK"):
        conn.execute(
            "INSERT INTO companies (company_id, legal_name, display_name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', 'now', 'now')",
            (company_id, company_id, company_id),
        )
    conn.commit()
    return conn


def _case(conn, case_id: str, company_ids: list[str], **kwargs):
    return create_research_case(
        conn, case_id, kind="ask", question="q?", company_ids=company_ids,
        statement_type="consolidated", owner_id=None, **kwargs,
    )


def test_case_defaults_to_investigation_origin_and_accepts_conversation(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    assert _case(conn, "a", [])["origin"] == "investigation"
    assert _case(conn, "b", [], origin="conversation")["origin"] == "conversation"


def test_creating_a_case_auto_tags_registered_companies_in_order_and_skips_unknown(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _case(conn, "a", ["ICICIBANK", "NOPE", "HDFCBANK"])
    assert list_case_company_ids(conn, "a") == ["ICICIBANK", "HDFCBANK"]


def test_one_multi_company_case_appears_under_every_company_without_duplication(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _case(conn, "cmp", ["HDFCBANK", "ICICIBANK", "AXISBANK"])
    for company_id in ("HDFCBANK", "ICICIBANK", "AXISBANK"):
        assert [r["case_id"] for r in list_cases_for_company(conn, company_id)] == ["cmp"]
    assert conn.execute("SELECT COUNT(*) FROM research_cases").fetchone()[0] == 1


def test_manual_tag_add_remove(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _case(conn, "a", ["HDFCBANK"])
    assert add_case_company(conn, "a", "AXISBANK") is True
    assert list_case_company_ids(conn, "a") == ["HDFCBANK", "AXISBANK"]
    assert conn.execute("SELECT source FROM case_companies WHERE company_id='AXISBANK'").fetchone()[0] == "manual"
    assert add_case_company(conn, "a", "AXISBANK") is True  # idempotent
    assert add_case_company(conn, "a", "UNKNOWN") is False
    assert add_case_company(conn, "missing-case", "HDFCBANK") is False
    assert remove_case_company(conn, "a", "HDFCBANK") is True
    assert remove_case_company(conn, "a", "HDFCBANK") is False
    assert list_case_company_ids(conn, "a") == ["AXISBANK"]


def test_hide_unhide_and_soft_delete_drop_a_case_from_company_view_but_keep_the_row(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _case(conn, "a", ["HDFCBANK"])
    hide_research_case(conn, "a")
    assert list_cases_for_company(conn, "HDFCBANK") == []
    unhide_research_case(conn, "a")
    assert len(list_cases_for_company(conn, "HDFCBANK")) == 1
    assert soft_delete_research_case(conn, "a") is True
    assert list_cases_for_company(conn, "HDFCBANK") == []
    assert get_research_case(conn, "a") is not None
    assert hide_research_case(conn, "a") is False  # deleted cases can't be re-hidden


def test_hard_delete_also_removes_its_tags(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _case(conn, "a", ["HDFCBANK"])
    assert delete_research_case(conn, "a") is True
    assert conn.execute("SELECT COUNT(*) FROM case_companies").fetchone()[0] == 0
