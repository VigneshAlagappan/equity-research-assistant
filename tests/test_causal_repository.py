import sqlite3

import pytest

from storage import causal_repository as repo


@pytest.fixture
def conn(db_conn: sqlite3.Connection):
    db_conn.execute(
        "INSERT INTO investigations (investigation_id, question, company_ids, statement_type, generated_at) "
        "VALUES ('inv1', 'q', '[]', 'consolidated', '2026-01-01')"
    )
    db_conn.execute(
        "INSERT INTO investigation_hypotheses (hypothesis_id, investigation_id, statement, category, "
        "generation_order, created_at) VALUES ('h1', 'inv1', 's', 'financial', 1, '2026-01-01')"
    )
    db_conn.commit()
    return db_conn


def _graph():
    nodes = [
        {"node_id": "inv1:h1:n0", "hypothesis_id": "h1", "position": 0, "label": "A", "normalized_label": "a"},
        {"node_id": "inv1:h1:n1", "hypothesis_id": "h1", "position": 1, "label": "B", "normalized_label": "b"},
    ]
    edges = [{
        "edge_id": "inv1:h1:e0", "hypothesis_id": "h1", "position": 0, "source_node_id": "inv1:h1:n0",
        "target_node_id": "inv1:h1:n1", "edge_key": "a->b", "supporting_count": 2, "presented": True,
        "hypothesis_verdict": "SUPPORTED",
    }]
    return nodes, edges


def test_graph_roundtrip_and_replace_is_idempotent(conn):
    nodes, edges = _graph()
    repo.replace_investigation_graph(conn, "inv1", nodes, edges)
    repo.replace_investigation_graph(conn, "inv1", nodes, edges)
    assert len(repo.list_graph_nodes(conn, "inv1")) == 2
    (edge,) = repo.list_graph_edges(conn, "inv1")
    assert edge["edge_key"] == "a->b" and edge["presented"] == 1 and edge["supporting_count"] == 2


def test_metrics_roundtrip_and_replace(conn):
    repo.save_investigation_metrics(conn, "inv1", {"edges_explored": 3, "models_used": ["m1", "m2"]})
    repo.save_investigation_metrics(conn, "inv1", {"edges_explored": 4, "evidence_coverage": 0.5})
    row = repo.get_investigation_metrics(conn, "inv1")
    assert row["edges_explored"] == 4 and row["evidence_coverage"] == 0.5 and row["investigation_version"] == 1


def test_feedback_newer_vote_supersedes_same_user_same_target_only(conn):
    base = {"investigation_id": "inv1", "hypothesis_id": "h1", "edge_id": "inv1:h1:e0", "user_id": 1}
    first = repo.insert_feedback(conn, {**base, "feedback_type": "NOT_RELEVANT"})
    other_user = repo.insert_feedback(conn, {**base, "user_id": 2, "feedback_type": "CORRECT"})
    other_target = repo.insert_feedback(conn, {**base, "edge_id": None, "feedback_type": "MISSING_DRIVER"})
    second = repo.insert_feedback(conn, {**base, "feedback_type": "CORRECT"})
    active = {r["feedback_id"] for r in repo.list_feedback(conn, "inv1")}
    assert active == {other_user, other_target, second} and first not in active
    assert {r["feedback_id"] for r in repo.list_feedback(conn, "inv1", user_id=1)} == {other_target, second}
    assert len(repo.list_feedback(conn, "inv1", include_superseded=True)) == 4


def test_eval_run_and_case_results(conn):
    repo.insert_eval_run(conn, "run1", {"benchmark_version": "v1", "cases_total": 2, "golden_recall": 0.5})
    repo.insert_eval_case_result(conn, "run1", {
        "case_id": "c1", "status": "ok", "expected_essential": 4, "matched_essential": 2,
        "unmatched_presented": ["x->y"],
    })
    (run,) = repo.list_eval_runs(conn)
    assert run["golden_recall"] == 0.5
    (result,) = repo.list_eval_case_results(conn, "run1")
    assert result["matched_essential"] == 2 and result["unmatched_presented_json"] == '["x->y"]'
