"""Structured causal feedback routes (docs/L5_MVP_TASK_PLAN.md, M8): taxonomy
validation, target ownership, supersede behaviour, login requirement, and the
controls appearing on the investigation page. Feedback writes the ledger only."""

from __future__ import annotations

import sqlite3

import pytest

from config.causal_feedback import FEEDBACK_TYPES, FeedbackValidationError, validate_feedback
from research.investigation_graph import build_graph
from storage.causal_repository import list_feedback, replace_investigation_graph
from storage.repositories import create_user
from tests.test_investigate_view_report import _seed_full_investigation, client  # noqa: F401  (fixture)


def test_validate_feedback_levels_and_limits():
    assert validate_feedback("edge", "WRONG_TIMING", "  hmm ") == "hmm"
    assert validate_feedback("edge", "OVERSTATED", "") is None
    for level, ftype in (("investigation", "WRONG_TIMING"), ("edge", "MISSING_DRIVER"), ("path", "OVERSTATED"), ("nope", "CORRECT"), ("edge", "BOGUS")):
        with pytest.raises(FeedbackValidationError):
            validate_feedback(level, ftype, None)
    with pytest.raises(FeedbackValidationError):
        validate_feedback("edge", "CORRECT", "x" * 2001)
    assert len(FEEDBACK_TYPES) == 9


@pytest.fixture
def seeded(client, tmp_path):  # noqa: F811
    db_path = tmp_path / "web_test.db"
    inv = _seed_full_investigation(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    hyps = [type("H", (), {"hypothesis_id": "hyp-report-1", "chain_steps": ["Merger closes", "Funding cost rises", "NIM compresses"]})()]
    g = build_graph(inv, hyps, {})
    replace_investigation_graph(conn, inv, g.nodes, g.edges)
    uid = create_user(conn, "a@example.com", "x")
    conn.close()
    return inv, uid, db_path


def _login(client, uid):
    with client.session_transaction() as sess:
        sess["user_id"] = uid


def test_requires_login(client, seeded):
    inv, _, _ = seeded
    r = client.post(f"/investigate/{inv}/feedback", json={"target": "investigation", "feedback_type": "CORRECT"})
    assert r.status_code in (302, 401)


def test_submit_validation_and_context_and_supersede(client, seeded):
    inv, uid, db_path = seeded
    _login(client, uid)
    url = f"/investigate/{inv}/feedback"

    assert client.post(url, json={"target": "edge", "target_id": "nope", "feedback_type": "CORRECT"}).status_code == 400
    assert client.post(url, json={"target": "hypothesis", "target_id": "nope", "feedback_type": "CORRECT"}).status_code == 400
    assert client.post(url, json={"target": "edge", "target_id": f"{inv}:hyp-report-1:e0", "feedback_type": "MISSING_DRIVER"}).status_code == 400

    edge = f"{inv}:hyp-report-1:e0"
    assert client.post(url, json={"target": "edge", "target_id": edge, "feedback_type": "NOT_RELEVANT", "comment": "not for banks"}).status_code == 200
    assert client.post(url, json={"target": "edge", "target_id": edge, "feedback_type": "CORRECT"}).status_code == 200
    assert client.post(url, json={"target": "investigation", "feedback_type": "MISSING_DRIVER", "comment": "deposit mix"}).status_code == 200

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    active = list_feedback(conn, inv)
    assert sorted(r["feedback_type"] for r in active) == ["CORRECT", "MISSING_DRIVER"]  # NOT_RELEVANT superseded
    edge_row = next(r for r in active if r["edge_id"] == edge)
    assert edge_row["company_id"] == "HDFCBANK" and edge_row["question_type"] == "investigation"
    assert edge_row["hypothesis_id"] == "hyp-report-1" and edge_row["user_class"] == "ordinary"
    assert edge_row["period"] == "2026-01-01"
    assert len(list_feedback(conn, inv, include_superseded=True)) == 3
    conn.close()

    mine = client.get(url).get_json()["feedback"]
    assert {m["feedback_type"] for m in mine} == {"CORRECT", "MISSING_DRIVER"}


def test_page_shows_feedback_controls_only_for_signed_in_viewer(client, seeded):
    inv, uid, _ = seeded
    anonymous = client.get(f"/investigate/{inv}", follow_redirects=True)
    assert b"causal-feedback" not in anonymous.data
    _login(client, uid)
    body = client.get(f"/investigate/{inv}", follow_redirects=True).data.decode()
    assert "Give feedback on this explanation" in body and "Merger closes" in body and "Funding cost rises" in body
