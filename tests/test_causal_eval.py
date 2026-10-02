import sqlite3
from types import SimpleNamespace

import pytest

from research.causal_eval import GoldenCase, aggregate, edge_matches, load_cases, score_case
from research.investigation_graph import build_graph
from scripts.run_causal_eval import run_causal_eval, score_investigation
from storage.causal_repository import (
    list_eval_case_results, list_eval_runs, replace_investigation_graph, save_investigation_metrics,
)


def _expected(src, tgt, imp="essential", i="e"):
    return {"edge_id": i, "source_aliases": src, "target_aliases": tgt, "importance": imp}


def test_alias_matching_is_case_insensitive_and_word_bounded():
    x = _expected(["nim", "net interest margin"], ["profit"])
    assert edge_matches("Net Interest Margin compresses", "Profit impacted", x)
    assert edge_matches("NIM down", "profitability falls", x) is False  # "profit" is not a whole word in "profitability"
    assert not edge_matches("Minimum balance", "profit", x)  # "nim" inside "minimum" must not match
    assert not edge_matches("NIM down", "revenue", x)


def test_golden_files_load_and_are_drafts():
    cases = load_cases("v1")
    assert len(cases) == 5 and {"l5_maruti_margin_movement", "l5_tatasteel_cross_sector_margin"} <= {c.case_id for c in cases}
    assert all(not c.reviewed for c in cases)  # authored drafts until a named reviewer signs them
    assert all(c.expected_edges and all(e["source_aliases"] and e["target_aliases"] for e in c.expected_edges) for c in cases)


def test_score_case_recall_precision_and_unmatched():
    case = GoldenCase("c", "v1", "q", ["X"], None, [
        _expected(["funding cost"], ["margin"], i="a"),
        _expected(["credit cost"], ["profit"], i="b"),
        _expected(["fees"], ["income"], "supporting", i="c"),
    ])
    edges = [
        {"source_label": "Funding cost rises", "target_label": "Margin compresses", "presented": True, "edge_key": "k1"},
        {"source_label": "Weather", "target_label": "Mood", "presented": True, "edge_key": "k2"},
        {"source_label": "Credit cost up", "target_label": "Profit down", "presented": False, "edge_key": "k3"},  # explored only
    ]
    r = score_case(case, edges)
    assert (r["expected_essential"], r["matched_essential"], r["matched_essential_explored"]) == (2, 1, 2)
    assert r["missed_essential_ids"] == ["b"]
    assert (r["presented_edges"], r["matched_presented"]) == (2, 1) and r["unmatched_presented"] == ["k2"]


def test_aggregate_ignores_failed_and_handles_empty():
    ok = {"status": "ok", "expected_essential": 4, "matched_essential": 3, "presented_edges": 5, "matched_presented": 2,
          "evidence_coverage": 0.5, "unsupported_edge_rate": 0.5}
    agg = aggregate([ok, {"status": "failed"}])
    assert agg["cases_total"] == 2 and agg["cases_completed"] == 1
    assert agg["golden_recall"] == 0.75 and agg["golden_precision_lower_bound"] == 0.4
    none = aggregate([{"status": "failed"}])
    assert none["golden_recall"] is None and none["golden_precision_lower_bound"] is None


@pytest.fixture
def conn(db_conn: sqlite3.Connection):
    db_conn.execute("INSERT INTO investigations (investigation_id, question, company_ids, statement_type, generated_at) VALUES ('inv1','q','[]','consolidated','2026-01-01')")
    db_conn.execute("INSERT INTO investigation_hypotheses (hypothesis_id, investigation_id, statement, category, generation_order, created_at) VALUES ('h1','inv1','s','financial',1,'2026-01-01')")
    db_conn.commit()
    return db_conn


def _persist_graph(conn):
    hyp = SimpleNamespace(hypothesis_id="h1", chain_steps=["Steel price up", "Material cost up", "Margin down"])
    ev = SimpleNamespace(verdict="SUPPORTED", supporting_evidence=[SimpleNamespace(chain_step=0)], contradicting_evidence=[])
    g = build_graph("inv1", [hyp], {"h1": ev})
    replace_investigation_graph(conn, "inv1", g.nodes, g.edges)
    save_investigation_metrics(conn, "inv1", {"evidence_coverage": 0.5, "unsupported_edge_rate": 0.5, "estimated_cost_usd": 1.25, "runtime_ms": 1000})


def test_score_existing_investigation_against_maruti_case(conn):
    _persist_graph(conn)
    case = next(c for c in load_cases("v1") if c.case_id == "l5_maruti_margin_movement")
    r = score_investigation(conn, case, "inv1")
    assert "material_margin" in r["matched_essential_ids"]  # "material cost" -> "margin"
    assert r["evidence_coverage"] == 0.5 and r["estimated_cost_usd"] == 1.25 and r["presented_edges"] == 2


def test_run_stores_run_and_case_rows_and_survives_a_failed_case(conn, monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    _persist_graph(conn)
    calls = {"n": 0}

    def fake_investigate(c, question, company_ids, as_of=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("model unavailable")
        return SimpleNamespace(investigation_id="inv1")

    run_id = run_causal_eval(
        conn, ["l5_maruti_margin_movement", "l5_tatasteel_cross_sector_margin"], "v1", investigate=fake_investigate,
    )
    (run,) = list_eval_runs(conn)
    assert run["eval_run_id"] == run_id and run["cases_total"] == 2 and run["cases_completed"] == 1
    assert run["engine_version"] and run["benchmark_version"] == "v1"
    rows = {r["case_id"]: r for r in list_eval_case_results(conn, run_id)}
    assert rows["l5_maruti_margin_movement"]["status"] == "ok"
    assert rows["l5_tatasteel_cross_sector_margin"]["status"] == "failed"
    assert "model unavailable" in rows["l5_tatasteel_cross_sector_margin"]["error_detail"]
