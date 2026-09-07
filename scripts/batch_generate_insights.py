"""Monthly job: generate + persist "Key Insights" for every Nifty 50 and US
company on file.

Closes the shallow half of the gap SCHEDULED_JOBS.md section 5 and
web/app.py's `insights_companies` ScheduledJob row flagged: research/
insights.py's generate_key_insights() (one Anthropic LLM call per company)
already existed as a reusable, company-scoped function -- it just had no
batch-loop wrapper or scheduler trigger, only the Overview tab's one-click,
one-company "Generate" button. Macro insights (SCHEDULED_JOBS.md's other
`insights_macro` row) are a separate, deeper gap -- no generation function
exists there yet -- and are not touched by this script.

Company universe is Nifty 50 (India) + every active US company on file,
matching SCHEDULED_JOBS.md section 5's own "Nifty 50 and USA" framing (not
the full Nifty 500 -- same reasoning web/app.py's `_run_financials_india`
gives for scoping its own Nifty 50 job: a monthly LLM-cost job shouldn't
silently expand to several hundred companies without a separate decision).

Paced noticeably slower than the price/XBRL batch jobs in this directory:
those wait out an external site's soft rate limits, this one spends real
Anthropic tokens and dollars per call (see SCHEDULED_JOBS.md section 5's
closing note -- 62 companies x a monthly LLM call each is real recurring
spend, confirm that's wanted before scheduling this job to run
unattended). A company with no ingested financials yet
(NoDataToSummarizeError) is skipped, not a failure -- "nothing to
summarize" is a normal, expected state for a newly-added company, same
"absence isn't an error" rule the rest of this app follows.

Usage: python -m scripts.batch_generate_insights
(a plain `python scripts/batch_generate_insights.py` fails on the
`research`/`storage` imports below -- run as a module so the repo root, not
scripts/, lands on sys.path, same as every other script here.)
"""

from __future__ import annotations

import time

from config.settings import ANTHROPIC_API_KEY_SET
from ingestion.batch_log import BatchRun
from research.insights import NoDataToSummarizeError, generate_key_insights
from storage.company_repository import select_active_companies_by_country, select_company_ids_by_index
from storage.database import init_db
from storage.repositories import save_company_insights

REQUEST_DELAY_SECONDS = 3.0
STATEMENT_TYPE = "consolidated"
JOB_NAME = "key_insights_batch"


def _target_company_ids(conn) -> list[str]:
    nifty50 = [r["company_id"] for r in select_company_ids_by_index(conn, "Nifty 50")]
    usa = [r["company_id"] for r in select_active_companies_by_country(conn, "US")]
    # A company somehow tagged both Nifty 50 and country='US' shouldn't get
    # two LLM calls in the same run -- dict.fromkeys() dedupes while keeping
    # India-then-USA order, plain and cheap for a list this small.
    return list(dict.fromkeys(nifty50 + usa))


def run_key_insights_batch(conn=None) -> int:
    """The actual per-company generate+persist loop, factored out of main()
    so the Settings > Data Operations > Schedule panel's "Run now" button
    (web/app.py) can trigger the identical job on demand -- same
    one-capability-two-triggers shape every other job in this directory's
    sibling scripts uses.

    Returns the BatchRun's run_id.

    Raises RuntimeError up front (before opening a BatchRun or touching any
    company) if no Anthropic API key is configured -- generate_key_insights()
    itself doesn't raise on that, it *catches* AllProvidersUnavailableError
    internally and returns a "temporarily unavailable" string instead (see
    research/insights.py), which company_generate_insights() (the Overview
    tab's own single-company button route) then persists as if it were a
    real insight. Fine for one accidental click; silently writing that
    placeholder over 62 companies' saved insights in one unattended run is
    not, so this job checks the precondition itself rather than reproducing
    that same rough edge at batch scale."""
    if not ANTHROPIC_API_KEY_SET:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on the server — the assistant can't run.")

    owns_conn = conn is None
    if conn is None:
        conn = init_db()

    try:
        companies = _target_company_ids(conn)
        total = len(companies)
        print(f"{total} companies (Nifty 50 + USA) on file", flush=True)

        generated = skipped = errors = 0
        with BatchRun(conn, JOB_NAME, scope_label=f"Nifty 50 + USA ({total})") as run:
            for i, company_id in enumerate(companies, 1):
                with run.item(company_id) as item:
                    try:
                        insight_text = generate_key_insights(conn, company_id, statement_type=STATEMENT_TYPE)
                    except NoDataToSummarizeError:
                        skipped += 1
                        print(f"[{i}/{total}] {company_id:24s} skipped (no data yet)", flush=True)
                        item.detail = "skipped: no data ingested yet"
                        time.sleep(REQUEST_DELAY_SECONDS)
                        continue
                    except Exception as exc:
                        errors += 1
                        print(f"[{i}/{total}] {company_id:24s} ERROR {exc}", flush=True)
                        time.sleep(REQUEST_DELAY_SECONDS)
                        raise

                    save_company_insights(conn, company_id, insight_text, STATEMENT_TYPE)
                    generated += 1
                    print(f"[{i}/{total}] {company_id:24s} generated", flush=True)
                    item.detail = "generated"

                    time.sleep(REQUEST_DELAY_SECONDS)

        print(f"\nDone. generated={generated} skipped={skipped} errors={errors} total={total}", flush=True)
        return run.run_id
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    run_key_insights_batch()


if __name__ == "__main__":
    main()
