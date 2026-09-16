"""Scheduled job: classify any macro series not yet represented in the
knowledge graph (research/macro_knowledge_builder.py's Step 2A counterpart
for macro data) into MacroFactor entities + MAY_AFFECT/DRIVES/EXPOSED_TO
relationships against the real sector/industry taxonomy.

No new batch-loop needed here, unlike scripts/batch_fetch_nse.py:
classify_macro_factors() already loops every un-classified series in its
own MAX_SERIES_PER_BATCH-sized batches and already tolerates one bad batch
without aborting the rest -- this script only adds the BatchRun audit-trail
wrapper (ingestion/batch_log.py, job_name="macro_factor_classification")
every other scheduled job in this file's sibling scripts already gets.

Usage: python -m scripts.classify_macro_factors_batch
(a plain `python scripts/classify_macro_factors_batch.py` fails on the
`ingestion`/`storage` imports below -- run as a module so the repo root,
not scripts/, lands on sys.path, same as every other script here.)
"""

from __future__ import annotations

from ingestion.batch_log import BatchRun
from research.macro_knowledge_builder import classify_macro_factors
from storage.database import init_db

JOB_NAME = "macro_factor_classification"


def run_macro_factor_classification_batch(conn=None) -> int:
    """The actual classification call, factored out of main() so the
    Settings > Data Operations > Schedule panel's "Run now" button
    (web/app.py) can trigger the identical job on demand -- same
    one-capability-two-triggers shape every other job in this file's
    sibling scripts uses. Returns the BatchRun's run_id."""
    owns_conn = conn is None
    if conn is None:
        conn = init_db()

    try:
        with BatchRun(conn, JOB_NAME, "macro series -> knowledge graph") as run:
            with run.item("macro_factor_classification") as item:
                result = classify_macro_factors(conn)
                item.detail = (
                    f"series_classified={result.series_classified} "
                    f"factors_created={result.factors_created} "
                    f"relationships_created={result.relationships_created} "
                    f"batches_failed={result.batches_failed}"
                )
                print(f"Done. {item.detail}", flush=True)
        return run.run_id
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    run_macro_factor_classification_batch()


if __name__ == "__main__":
    main()
