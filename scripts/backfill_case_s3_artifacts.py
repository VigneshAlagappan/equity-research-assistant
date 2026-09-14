"""One-time backfill: for every investigation/generated_report that
predates ADR-021's persistence split (s3_key IS NULL), reconstruct its
full content from the still-intact normalized tables (investigation_
hypotheses/investigation_hypothesis_evidence, research_thread_evidence/
research_thread_followups -- none of that was touched by the split, only
new writes changed shape), upload the same JSON artifact shape research/
investigation.py::_persist() / web/app.py::_persist_generated_report_s3()
build for new rows, generate an LLM abstract, and record s3_key/abstract/
version/strongest_verdict on the row.

Idempotent/resumable: only ever selects rows with s3_key IS NULL, so a
re-run after a partial failure just picks up where it left off.

Usage: python -m scripts.backfill_case_s3_artifacts
"""

from __future__ import annotations

import json
import time

# Must run before any other import in this file touches storage.
# repositories/company_repository/etc. -- see storage/backend_bootstrap.py's
# own docstring, and scripts/run_job.py's identical top-of-file comment for
# why importing scheduling.jobs (or anything it transitively imports)
# first, then calling open_db() later, is too late.
import storage.backend_bootstrap

storage.backend_bootstrap.install()

from research.abstracts import generate_abstract
from scheduling.jobs import open_db
from storage.document_store import default_document_store
from storage.repositories import (
    get_strongest_verdict_by_investigation,
    list_investigation_hypotheses,
    list_investigation_hypothesis_evidence,
    list_investigations,
    list_report_evidence,
    list_report_followups,
    update_generated_report_s3_metadata,
    update_investigation_s3_metadata,
)


def backfill_investigations(conn) -> tuple[int, int]:
    all_investigations = [dict(row) for row in list_investigations(conn)]
    pending = [inv for inv in all_investigations if not inv.get("s3_key")]
    if not pending:
        return 0, 0
    ids = [inv["investigation_id"] for inv in pending]
    verdict_by_id = get_strongest_verdict_by_investigation(conn, ids)

    ok = 0
    for inv in pending:
        investigation_id = inv["investigation_id"]
        hypotheses_json = []
        for h in list_investigation_hypotheses(conn, investigation_id):
            h = dict(h)
            evidence = [dict(e) for e in list_investigation_hypothesis_evidence(conn, h["hypothesis_id"])]
            hypotheses_json.append({
                "hypothesis_id": h["hypothesis_id"], "statement": h["statement"], "mechanism": h["mechanism"],
                "chain_steps": json.loads(h["chain_steps"] or "[]") if "chain_steps" in h else [],
                "category": h["category"], "rationale": h["rationale"],
                "unknowns": json.loads(h["unknowns"] or "[]"), "generation_order": h["generation_order"],
                "verdict": h["verdict"], "confidence_basis": h["confidence_basis"],
                "confidence_score": h["confidence_score"], "synthesis_rank": h["synthesis_rank"],
                "supporting_evidence": [e for e in evidence if e["stance"] == "supporting"],
                "contradicting_evidence": [e for e in evidence if e["stance"] == "contradicting"],
                "missing_evidence": [e for e in evidence if e["stance"] == "missing"],
            })
        artifact = {
            "investigation_id": investigation_id, "question": inv["question"],
            "company_ids": json.loads(inv["company_ids"] or "[]"), "statement_type": inv["statement_type"],
            "strongest_explanation": inv["strongest_explanation"],
            "unanswered_questions": json.loads(inv["unanswered_questions"] or "[]"),
            "additional_evidence_needed": json.loads(inv["additional_evidence_needed"] or "[]"),
            "as_of": inv["as_of"], "hypotheses": hypotheses_json,
        }
        s3_key = f"investigations/{investigation_id}/v1.json"
        default_document_store().store(s3_key, json.dumps(artifact, indent=2).encode("utf-8"))
        abstract = generate_abstract(conn, inv["strongest_explanation"])
        update_investigation_s3_metadata(
            conn, investigation_id, s3_key=s3_key, abstract=abstract, version=1,
            strongest_verdict=verdict_by_id.get(investigation_id),
        )
        ok += 1
        print(f"  investigation {investigation_id}: backfilled", flush=True)
    return ok, len(pending)


def backfill_generated_reports(conn) -> tuple[int, int]:
    from storage.repositories import list_generated_reports

    all_reports = list_generated_reports(conn)
    pending = [r for r in all_reports if not r.get("s3_key")]
    if not pending:
        return 0, 0

    ok = 0
    for report in pending:
        thread_id = report["thread_id"]
        evidence = list_report_evidence(conn, thread_id)
        followups = list_report_followups(conn, thread_id)
        artifact = {
            "thread_id": thread_id, "question": report["question"], "company_ids": report["company_ids"],
            "statement_type": report["statement_type"], "report_markdown": report["report_markdown"],
            "evidence": evidence, "followups": followups,
        }
        s3_key = f"threads/{thread_id}/v1.json"
        default_document_store().store(s3_key, json.dumps(artifact, indent=2).encode("utf-8"))
        abstract = generate_abstract(conn, report["report_markdown"])
        update_generated_report_s3_metadata(conn, thread_id, s3_key=s3_key, abstract=abstract, version=1)
        ok += 1
        print(f"  thread {thread_id}: backfilled", flush=True)
    return ok, len(pending)


def main() -> None:
    conn = open_db()
    start = time.time()

    inv_ok, inv_total = backfill_investigations(conn)
    print(f"Investigations: {inv_ok}/{inv_total} backfilled", flush=True)

    report_ok, report_total = backfill_generated_reports(conn)
    print(f"Generated reports: {report_ok}/{report_total} backfilled", flush=True)

    print(f"\nDone in {time.time() - start:.1f}s", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
