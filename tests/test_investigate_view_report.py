"""End-to-end check that /investigate/<id> renders through the new Signal
Report Design System (reports/schema + reports/templates/deep_dive) without
breaking backward compatibility for investigations stored via the legacy
table-based path (no S3 artifact — see web/app.py's investigate_view
docstring comment on investigation_row["s3_key"]).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from companies.registry import seed_companies
from normalization.financials import ensure_metric_vocabulary
from storage.database import init_db
from storage.repositories import (
    save_investigation,
    save_investigation_hypothesis,
    save_investigation_hypothesis_evidence,
)


def _build_app(db_path: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    monkeypatch.setattr("config.settings.DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr("config.settings.RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr("config.settings.PRICE_DB_PATH", tmp_path / "price_history.db")
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    from web.app import create_app

    app = create_app()
    app.testing = True
    return app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "web_test.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        yield test_client


def _seed_full_investigation(db_path: Path) -> str:
    """A complete legacy-table investigation with two hypotheses -- one
    fully scored/ranked, one with confidence_score=None (never scored)."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    save_investigation(
        conn,
        investigation_id="inv-report-1",
        question="Why did HDFC Bank's NIM compress in FY24?",
        company_ids=["HDFCBANK"],
        statement_type="consolidated",
        strongest_explanation="Funding-cost pressure from the HDFC Ltd merger outweighed asset repricing.",
        unanswered_questions=["How much reverses in FY25?"],
        additional_evidence_needed=["Segment-wise deposit cost breakdown"],
        as_of="2026-01-01",
    )
    save_investigation_hypothesis(
        conn,
        hypothesis_id="hyp-report-1",
        investigation_id="inv-report-1",
        statement="Merger-driven funding cost pressure compressed NIM.",
        mechanism="Higher-cost HDFC Ltd borrowings replaced low-cost CASA funding mix.",
        category="Financial",
        rationale="Initial rationale.",
        unknowns=["Exact merged funding mix"],
        generation_order=1,
        chain_steps=["Merger closes", "Wholesale borrowings absorbed", "Funding cost rises", "NIM compresses"],
        verdict="SUPPORTED",
        confidence_basis="Cost of funds rose 45bps while yield on assets rose only 20bps.",
        confidence_score=78,
        synthesis_rank=1,
    )
    save_investigation_hypothesis_evidence(
        conn,
        "hyp-report-1",
        [
            {"stance": "supporting", "kind": "FACT", "label": "Cost of funds", "value": "+45bps YoY", "citation": "FY24 AR p.12"},
            {"stance": "contradicting", "kind": "MANAGEMENT_OPINION", "label": "Management guided margin stability", "value": None, "citation": "Q3FY24 concall"},
            {"stance": "missing", "kind": "INFERENCE", "label": "Segment-level funding cost", "value": None, "citation": None},
        ],
    )
    # Second hypothesis: never scored (confidence_score left as its default
    # None) -- must render "Unscored", not crash the >=60/>=30 comparisons.
    save_investigation_hypothesis(
        conn,
        hypothesis_id="hyp-report-2",
        investigation_id="inv-report-1",
        statement="Loan mix shift toward lower-yield secured retail also contributed.",
        mechanism=None,
        category="Operational",
        rationale=None,
        unknowns=[],
        generation_order=2,
        chain_steps=[],
        verdict="INSUFFICIENT_EVIDENCE",
        confidence_basis=None,
        confidence_score=None,
        synthesis_rank=None,
    )
    conn.close()
    return "inv-report-1"


def test_investigate_view_renders_full_investigation_through_new_report_system(client, tmp_path, monkeypatch):
    db_path = tmp_path / "web_test.db"
    investigation_id = _seed_full_investigation(db_path)

    response = client.get(f"/investigate/{investigation_id}")
    assert response.status_code == 200
    body = response.data.decode()

    # Signal Report Design System markers -- confirms the new template/theme
    # actually rendered, not the old investigation.html.
    assert "signal-report" in body
    assert "Deep Dive Research Report" in body

    # Content preserved verbatim from the research pipeline's output.
    assert "Funding-cost pressure from the HDFC Ltd merger" in body
    assert "Merger-driven funding cost pressure compressed NIM." in body
    assert "Cost of funds" in body
    assert "Management guided margin stability" in body
    assert "Segment-level funding cost" in body
    assert "How much reverses in FY25?" in body
    assert "Segment-wise deposit cost breakdown" in body

    # Causal chain preserved.
    assert "Merger closes" in body
    assert "NIM compresses" in body

    # Score band + evidence-matrix status words are present.
    assert "Leading" in body  # hyp-report-1's 78% score
    assert "Unscored" in body  # hyp-report-2's None score -- no crash, graceful label
    assert "Have" in body
    assert "Contradicts" in body
    assert "Missing" in body


def test_investigate_view_older_investigation_missing_fields_does_not_crash(client, tmp_path):
    """A pre-migration investigation: no chain_steps, no confidence_score,
    no synthesis_rank, no strongest_explanation, no follow-up/gap lists.
    Must render 200, not 500."""
    import sqlite3

    db_path = tmp_path / "web_test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    save_investigation(
        conn,
        investigation_id="inv-old-1",
        question="An older investigation with minimal fields.",
        company_ids=["HDFCBANK"],
        statement_type="consolidated",
        strongest_explanation=None,
        unanswered_questions=[],
        additional_evidence_needed=[],
    )
    save_investigation_hypothesis(
        conn,
        hypothesis_id="hyp-old-1",
        investigation_id="inv-old-1",
        statement="A hypothesis with no score, chain, or rank.",
        mechanism=None,
        category="Strategic",
        rationale=None,
        unknowns=[],
        generation_order=1,
    )
    conn.close()

    response = client.get("/investigate/inv-old-1")
    assert response.status_code == 200
    body = response.data.decode()
    assert "signal-report" in body
    assert "A hypothesis with no score, chain, or rank." in body
    assert "Unscored" in body
