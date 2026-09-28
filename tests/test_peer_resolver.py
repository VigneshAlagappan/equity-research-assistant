"""research/peer_resolver.py — Level 4 comparison-group grounding.
Fully deterministic: no LLM, no network, no monkeypatching required beyond
config.settings.MAX_COMPARISON_DATASETS for the cap tests.
"""

from __future__ import annotations

import sqlite3

import pytest

from companies.registry import register_company, seed_companies
from research.peer_resolver import resolve_comparison_group


@pytest.fixture
def conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)  # HDFCBANK + ICICIBANK, both basic_industry="Private Sector Bank"
    return db_conn


def test_no_comparison_language_is_a_no_op(conn) -> None:
    resolution = resolve_comparison_group(conn, ["HDFCBANK"], "What was net profit in FY2024?")

    assert resolution.peer_company_ids == []
    assert resolution.planner_used is False
    assert resolution.neo4j_used is False


def test_already_multi_company_question_is_left_alone(conn) -> None:
    """The caller's own company_ids already IS the comparison group when it
    names more than one company -- nothing for this module to ground."""
    resolution = resolve_comparison_group(conn, ["HDFCBANK", "ICICIBANK"], "Compare their industry standing")

    assert resolution.peer_company_ids == []
    assert resolution.planner_used is False


def test_industry_keyword_resolves_sector_peer(conn) -> None:
    resolution = resolve_comparison_group(conn, ["HDFCBANK"], "Compare HDFC Bank's growth with the industry")

    assert resolution.peer_company_ids == ["ICICIBANK"]
    assert resolution.grounding_field == "basic_industry"
    assert resolution.grounding_value == "Private Sector Bank"
    assert resolution.planner_used is True
    assert resolution.notes == []


def test_peers_keyword_also_triggers_resolution(conn) -> None:
    resolution = resolve_comparison_group(conn, ["HDFCBANK"], "How does HDFC Bank stack up against its peers?")

    assert resolution.peer_company_ids == ["ICICIBANK"]


def test_caps_peer_count_unless_broader_scope_requested(conn, monkeypatch) -> None:
    monkeypatch.setattr("research.peer_resolver.MAX_COMPARISON_DATASETS", 0)

    resolution = resolve_comparison_group(conn, ["HDFCBANK"], "Compare HDFC Bank's growth with its peers")

    assert resolution.peer_company_ids == []
    assert resolution.notes  # limitation stated, per policy


def test_broader_scope_wording_lifts_the_cap(conn, monkeypatch) -> None:
    monkeypatch.setattr("research.peer_resolver.MAX_COMPARISON_DATASETS", 0)

    resolution = resolve_comparison_group(conn, ["HDFCBANK"], "Compare HDFC Bank's growth with the entire industry")

    assert resolution.peer_company_ids == ["ICICIBANK"]
    assert resolution.notes == []


def test_no_sector_on_file_states_the_limitation_rather_than_inventing_a_peer(conn) -> None:
    register_company(conn, "NOSECTOR", "No Sector Ltd", "No Sector")  # no sector/industry fields set

    resolution = resolve_comparison_group(conn, ["NOSECTOR"], "Compare it with its peers")

    assert resolution.peer_company_ids == []
    assert resolution.notes
    assert resolution.planner_used is True


def test_unregistered_company_states_the_limitation(conn) -> None:
    resolution = resolve_comparison_group(conn, ["NOTREAL"], "Compare it with its peers")

    assert resolution.peer_company_ids == []
    assert resolution.notes
