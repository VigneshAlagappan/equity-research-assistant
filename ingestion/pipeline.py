"""Ingestion pipeline: detect -> parse -> validate -> normalize -> store -> index.

(README: Ingestion Approach by Source — the generic flow every source follows,
regardless of which adapter runs. "Normalize" happens inside the adapter via
normalization/financials.py; this module owns validate -> store -> reconcile.)
"""

from __future__ import annotations

import json
import logging
from storage.db_types import DBConnection
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import ingestion.workers  # noqa: F401 -- registers built-in workers (financial_derivation, ...)
from companies.lifecycle import assert_active
from ingestion.detector import ADAPTER_CLASSES, detect_from_path, detect_macro_source_from_path
from ingestion.event_bus import publish
from ingestion.events import DatasetIngestedEvent
from ingestion.validation import validate_macro_observation, validate_observation
from normalization.companies import normalize_company_id
from sources.base import NormalizedObservation
from sources.macro import MacroDataAdapter, MacroNormalizedObservation
from sources.rbi_bank_infrastructure import parse_bank_infrastructure_file
from sources.rbi_dbie_tables import (
    looks_like_row_oriented_dbie_table,
    parse_rbi_daily_rate_table,
    parse_rbi_dbie_table,
)
from sources.fred import fetch_fred_series_raw, fred_csv_url, parse_fred_csv
from storage import raw_object_repository as ror
from storage.raw_object_store import store_raw_object
from sources.iitm_rainfall import parse_iitm_file
from sources.rbi_indicators import looks_like_rbi_indicator_workbook, parse_rbi_indicator_workbook
from sources.sec_edgar import SECEdgarAdapter, company_facts_url, fetch_company_facts_raw
from sources.yfinance_financials import YFinanceAdapter
from storage.repositories import (
    compute_reconciliation_keys,
    get_existing_macro_periods,
    insert_bank_infrastructure_observations,
    insert_financial_observations,
    insert_macro_observations,
)

logger = logging.getLogger(__name__)


def _publish_financial_ingestion(
    conn: DBConnection, *, company_id: str, source_id: str, statement_type: str,
    valid: list[NormalizedObservation],
) -> int:
    """Compute the touched reconciliation keys, publish a
    company_financials DATASET_INGESTED event, and return the reconciled
    count the Financial Derivation Worker (ingestion/workers/
    financial_derivation.py) reports back -- the worker does the actual
    reconciliation now, not this pipeline (README: Signals Dataset-Centric
    Ingestion -- derived calculations belong downstream of ingestion)."""
    keys = compute_reconciliation_keys(conn, valid)
    event = DatasetIngestedEvent(
        dataset_id=f"{source_id}:{company_id}",
        dataset_type="company_financials",
        source=source_id,
        scope={"company_id": company_id, "statement_type": statement_type},
        storage_reference={"table": "financial_observations", "reconcile_keys": [list(k) for k in keys]},
        ingestion_id=str(uuid.uuid4()),
        metadata={"observation_count": len(valid)},
    )
    outcomes = publish(conn, event)
    for outcome in outcomes:
        if outcome.worker_name == "financial_derivation":
            return outcome.result.data.get("reconciled_count", 0)
    return 0


def _publish_dataset_ingested(
    conn: DBConnection, *, dataset_id: str, dataset_type: str, source: str,
    storage_reference: dict, scope: dict, period: str | None = None, metadata: dict | None = None,
) -> None:
    """Publish a DATASET_INGESTED event for a dataset type with no
    downstream worker yet (macro, bank_infrastructure) -- every registered
    worker still runs and reports "skipped" (none is relevant), which is
    exactly the zero-risk "future dataset, future worker" extensibility
    this framework is for. No return value: unlike
    _publish_financial_ingestion(), nothing here needs a synchronous result
    back yet."""
    event = DatasetIngestedEvent(
        dataset_id=dataset_id, dataset_type=dataset_type, source=source,
        storage_reference=storage_reference, ingestion_id=str(uuid.uuid4()),
        scope=scope, period=period, metadata=metadata or {},
    )
    publish(conn, event)


@dataclass
class IngestionResult:
    company_id: str
    source_id: str
    file_path: str
    parsed_count: int = 0
    inserted_count: int = 0
    skipped_count: int = 0
    reconciled_count: int = 0
    skip_reasons: list[str] = field(default_factory=list)


@dataclass
class MacroIngestionResult:
    series_key: str
    source_id: str
    file_path: str
    parsed_count: int = 0
    inserted_count: int = 0
    skipped_count: int = 0
    skip_reasons: list[str] = field(default_factory=list)


@dataclass
class BankInfrastructureIngestionResult:
    source_id: str
    file_path: str
    parsed_count: int = 0
    inserted_count: int = 0


def ingest_file(
    conn: DBConnection,
    file_path: Path,
    *,
    company_id: str | None = None,
    source_id: str | None = None,
    statement_type: str = "consolidated",
) -> IngestionResult:
    """Run one raw file through the full pipeline.

    company_id/source_id are inferred from the file's path
    (data/raw/<COMPANY>/<source>/<file>) unless given explicitly. company_id
    is always normalized (uppercased) before use — a raw.raw/<company> folder
    isn't necessarily typed in canonical case, but companies.company_id is
    always the normalized form (companies/registry.py), so path-detected and
    explicitly-passed company_ids must both go through the same normalization
    or ingested observations silently key under a different company_id than
    the one they were registered under.
    """
    if company_id is None or source_id is None:
        detected_company, detected_source = detect_from_path(file_path)
        company_id = company_id or detected_company
        source_id = source_id or detected_source
    company_id = normalize_company_id(company_id)

    assert_active(conn, company_id)  # ingestion gate (README: Company Lifecycle)

    adapter_cls = ADAPTER_CLASSES.get(source_id)
    if adapter_cls is None:
        raise ValueError(f"No adapter registered for source_id={source_id!r}")
    adapter = adapter_cls(conn)

    parsed = adapter.parse(file_path, company_id, statement_type=statement_type)

    result = IngestionResult(company_id=company_id, source_id=source_id, file_path=str(file_path))
    result.parsed_count = len(parsed)

    valid: list[NormalizedObservation] = []
    for obs in parsed:
        problems = validate_observation(obs)
        if problems:
            result.skipped_count += 1
            label = f"{obs.metric_key} {obs.fiscal_year}{obs.quarter or ''}"
            reason = f"{label}: {'; '.join(problems)}"
            result.skip_reasons.append(reason)
            logger.warning("Skipping invalid observation: %s", reason)
            continue
        valid.append(obs)

    insert_financial_observations(conn, valid)
    result.inserted_count = len(valid)
    result.reconciled_count = _publish_financial_ingestion(
        conn, company_id=company_id, source_id=source_id, statement_type=statement_type, valid=valid,
    )

    logger.info(
        "Ingested %s (%s): parsed=%d inserted=%d skipped=%d reconciled=%d",
        file_path, source_id, result.parsed_count, result.inserted_count,
        result.skipped_count, result.reconciled_count,
    )
    return result


def ingest_nse_pdf_observations(
    conn: DBConnection,
    company_id: str,
    observations: list[NormalizedObservation],
    *,
    source_file: str,
) -> IngestionResult:
    """Store already-extracted (sources/nse_pdf_extractor.py) PDF-sourced
    observations through the same validate -> store -> reconcile flow every
    other adapter's parse() output goes through — this function starts
    after parsing, not before it, since a PDF's document registration
    (storage/repositories.save_company_document(), done by the caller) has
    to happen first to get the source_document_id every observation here
    already carries.

    "Check what's already in canonical_financials before writing" (the
    production task's own scope note — XBRL keeps trust-rank priority
    regardless of period_type) is enforced HERE, one observation at a
    time, rather than left to reconcile()'s own trust_rank tie-break:
    every PDF observation is stamped source="nse" (same convention as
    XBRL — see sources/nse_pdf_extractor.py's module docstring), so it
    would otherwise tie with a real XBRL "nse" observation for the same
    (metric, period) key and the outcome would depend on retrieved_at
    ordering, not on which one is actually more trustworthy. Skipping
    outright whenever a canonical value already exists for that exact key
    is simpler and matches the task's own stated intent more directly than
    teaching reconcile() a second "nse" sub-tier.

    Caveat worth being explicit about (not novel to this function — it's
    an existing, already-shipped side effect of the 2026-08 NSE XBRL
    directive, see reconcile()'s own docstring): inserting ANY "nse"-
    sourced observation for a period "migrates" that whole (period_type,
    fiscal_year, quarter, statement_type) scope, so a real-XBRL-absent
    (pre-2019) period this backfill adds a PDF-sourced P&L fact to will
    also stop honoring any legacy proprietary/screener value for OTHER
    metrics in that same period that PDF/XBRL didn't report — same
    behavior a genuinely new XBRL filing for that period would already
    cause today, not something this function introduces.
    """
    from storage.repositories import get_canonical_value

    result = IngestionResult(company_id=company_id, source_id="nse", file_path=source_file)
    result.parsed_count = len(observations)

    to_insert: list[NormalizedObservation] = []
    for obs in observations:
        problems = validate_observation(obs)
        if problems:
            result.skipped_count += 1
            result.skip_reasons.append(f"{obs.metric_key} {obs.fiscal_year}{obs.quarter or ''}: {'; '.join(problems)}")
            continue
        existing = get_canonical_value(
            conn, company_id, obs.metric_key, obs.period_type, obs.fiscal_year,
            quarter=obs.quarter, statement_type=obs.statement_type,
        )
        if existing is not None:
            result.skipped_count += 1
            result.skip_reasons.append(
                f"{obs.metric_key} {obs.fiscal_year}{obs.quarter or ''} ({obs.statement_type}): "
                f"already has a canonical value from an earlier source — not overwritten"
            )
            continue
        to_insert.append(obs)

    insert_financial_observations(conn, to_insert)
    result.inserted_count = len(to_insert)
    result.reconciled_count = _publish_financial_ingestion(
        conn, company_id=company_id, source_id="nse", statement_type="consolidated", valid=to_insert,
    )
    logger.info(
        "PDF-ingested %s: parsed=%d inserted=%d skipped=%d reconciled=%d",
        source_file, result.parsed_count, result.inserted_count, result.skipped_count, result.reconciled_count,
    )
    return result


def ingest_yfinance_company(
    conn: DBConnection,
    company_id: str,
    ticker: str,
    *,
    currency: str = "USD",
    statement_type: str = "consolidated",
) -> IngestionResult:
    """Fetch a company's financials live from Yahoo Finance and run them
    through the same validate -> store -> reconcile steps ingest_file() uses
    for an uploaded file. Deliberately a separate function, not a branch of
    ingest_file(): there's no file_path/adapter-detection-by-path step here,
    the ticker is the input, same reasoning ingest_macro_file() is its own
    function rather than a parameter added to ingest_file().
    """
    company_id = normalize_company_id(company_id)
    assert_active(conn, company_id)  # same ingestion gate as ingest_file()

    adapter = YFinanceAdapter(conn)

    # ADR-022: land the raw statement JSON in raw/companies/ -- see
    # YFinanceAdapter.fetch_raw_statements_json()'s own docstring for why
    # this is a second yfinance call rather than sharing one fetch with
    # adapter.fetch() below (a deliberate, disclosed tradeoff for this
    # pilot/non-scheduled path).
    raw_bytes = adapter.fetch_raw_statements_json(ticker)
    raw_result = store_raw_object(
        conn, source="yfinance_financials", entity=company_id, object_type="annual_statements",
        period=None, source_url=None, raw_prefix="companies", content=raw_bytes, extension="json",
    )

    parsed = adapter.fetch(company_id, ticker, currency=currency, statement_type=statement_type)

    result = IngestionResult(company_id=company_id, source_id=adapter.source_id, file_path=f"yfinance:{ticker}")
    result.parsed_count = len(parsed)

    valid: list[NormalizedObservation] = []
    for obs in parsed:
        problems = validate_observation(obs)
        if problems:
            result.skipped_count += 1
            label = f"{obs.metric_key} {obs.fiscal_year}{obs.quarter or ''}"
            reason = f"{label}: {'; '.join(problems)}"
            result.skip_reasons.append(reason)
            logger.warning("Skipping invalid observation: %s", reason)
            continue
        valid.append(obs)

    insert_financial_observations(conn, valid)
    result.inserted_count = len(valid)
    result.reconciled_count = _publish_financial_ingestion(
        conn, company_id=company_id, source_id=adapter.source_id, statement_type=statement_type, valid=valid,
    )

    ror.update_raw_object_state(conn, raw_result.object_id, state="ingested", mark_processed=True)
    ror.insert_lineage(
        conn, object_id=raw_result.object_id, derived_store="financial_observations",
        derived_table="financial_observations", derived_record_id=company_id,
    )

    logger.info(
        "Ingested %s (yfinance): parsed=%d inserted=%d skipped=%d reconciled=%d",
        ticker, result.parsed_count, result.inserted_count, result.skipped_count, result.reconciled_count,
    )
    return result


def ingest_sec_edgar_company(
    conn: DBConnection,
    company_id: str,
    cik: int,
    *,
    currency: str = "USD",
) -> IngestionResult:
    """Fetch a US company's quarterly + annual financials live from SEC
    EDGAR's own XBRL data and run them through the same validate -> store
    -> reconcile steps ingest_file() uses for an uploaded file -- same
    "live source, separate function, not a branch of ingest_file()" shape
    as ingest_yfinance_company() just above. No statement_type parameter
    (unlike that one): US public companies file consolidated financials
    only, there's no separate standalone statement to choose between.
    """
    company_id = normalize_company_id(company_id)
    assert_active(conn, company_id)  # same ingestion gate as ingest_file()

    # ADR-022: land the raw companyfacts JSON in raw/companies/ BEFORE
    # parsing -- dedup-by-hash means a re-fetch that returns byte-identical
    # companyfacts (this job's steady-state case once scripts/batch_fetch_
    # sec_edgar.py's own 24h TTL skip has already filtered out the obvious
    # repeats) creates zero new S3 writes/catalog rows. If insert_financial_
    # observations() below raises (e.g. the known financial_observations/
    # Postgres gap, ADR-021's "Known bug, NOT fixed"), the raw object stays
    # at state='stored', not 'ingested' -- preserved and replayable once
    # that's fixed, never lost just because downstream parsing/insert failed.
    raw_bytes = fetch_company_facts_raw(cik)
    raw_result = store_raw_object(
        conn, source="sec_edgar", entity=company_id, object_type="companyfacts", period=None,
        source_url=company_facts_url(cik), raw_prefix="companies", content=raw_bytes, extension="json",
    )
    facts = json.loads(raw_bytes)

    adapter = SECEdgarAdapter(conn)
    parsed = adapter.fetch(company_id, cik, currency=currency, facts=facts)

    result = IngestionResult(company_id=company_id, source_id=adapter.source_id, file_path=f"sec_edgar:CIK{cik:010d}")
    result.parsed_count = len(parsed)

    valid: list[NormalizedObservation] = []
    for obs in parsed:
        problems = validate_observation(obs)
        if problems:
            result.skipped_count += 1
            label = f"{obs.metric_key} {obs.fiscal_year}{obs.quarter or ''}"
            reason = f"{label}: {'; '.join(problems)}"
            result.skip_reasons.append(reason)
            logger.warning("Skipping invalid observation: %s", reason)
            continue
        valid.append(obs)

    insert_financial_observations(conn, valid)
    result.inserted_count = len(valid)
    result.reconciled_count = _publish_financial_ingestion(
        conn, company_id=company_id, source_id=adapter.source_id, statement_type="consolidated", valid=valid,
    )

    ror.update_raw_object_state(conn, raw_result.object_id, state="ingested", mark_processed=True)
    ror.insert_lineage(
        conn, object_id=raw_result.object_id, derived_store="financial_observations",
        derived_table="financial_observations", derived_record_id=company_id,
    )

    logger.info(
        "Ingested CIK%010d (sec_edgar): parsed=%d inserted=%d skipped=%d reconciled=%d",
        cik, result.parsed_count, result.inserted_count, result.skipped_count, result.reconciled_count,
    )
    return result


def ingest_macro_file(
    conn: DBConnection,
    file_path: Path,
    *,
    source_id: str | None = None,
    series_key: str | None = None,
) -> MacroIngestionResult:
    """Run one raw macro file through detect -> parse -> validate -> store.

    Deliberately a separate function from ingest_file(), not a branch inside
    it: there's no company_id here at all, so no assert_active() lifecycle
    gate and no reconciliation against canonical_financials — a genuinely
    different pipeline, not the same one with a nullable field threaded
    through it (README: Data Layers -> Non-company sources).

    Dispatches on file shape, not just extension: sources/macro.py's
    MacroDataAdapter handles the CSV convention (period,value,unit — one
    file, one series); an .xlsx/.xls file instead goes through
    sources/rbi_indicators.py if it matches the "50 Macroeconomic
    Indicators" workbook's sheet names, or sources/rbi_dbie_tables.py's
    single-table parser otherwise; source_id "iitm" goes through
    sources/iitm_rainfall.py's fixed-width parser. series_key is ignored
    for the XLSX and IITM paths — they derive series_key per row/column
    themselves, unlike the CSV convention's one-series-per-file.
    """
    source_id = source_id or detect_macro_source_from_path(file_path)
    if source_id == "iitm":
        parsed = parse_iitm_file(file_path)
    elif file_path.suffix.lower() in (".xlsx", ".xls"):
        if looks_like_rbi_indicator_workbook(file_path):
            parsed = parse_rbi_indicator_workbook(file_path)
        elif looks_like_row_oriented_dbie_table(file_path):
            parsed = parse_rbi_daily_rate_table(file_path)
        else:
            parsed = parse_rbi_dbie_table(file_path)
    else:
        adapter = MacroDataAdapter(source_id)
        parsed = adapter.parse(file_path, series_key=series_key)

    result = MacroIngestionResult(
        series_key=series_key or file_path.stem, source_id=source_id, file_path=str(file_path)
    )
    result.parsed_count = len(parsed)

    valid: list[MacroNormalizedObservation] = []
    for obs in parsed:
        problems = validate_macro_observation(obs)
        if problems:
            result.skipped_count += 1
            label = f"{obs.series_key} {obs.period}"
            reason = f"{label}: {'; '.join(problems)}"
            result.skip_reasons.append(reason)
            logger.warning("Skipping invalid macro observation: %s", reason)
            continue
        valid.append(obs)

    insert_macro_observations(conn, valid)
    result.inserted_count = len(valid)
    if valid:
        _publish_dataset_ingested(
            conn,
            dataset_id=f"macro:{source_id}:{result.series_key}",
            dataset_type="macro",
            source=source_id,
            storage_reference={"table": "macro_observations"},
            scope={
                "series_keys": sorted({obs.series_key for obs in valid}),
                "regions": sorted({obs.region for obs in valid if obs.region}),
            },
            metadata={"observation_count": len(valid)},
        )

    logger.info(
        "Ingested %s (macro/%s): parsed=%d inserted=%d skipped=%d",
        file_path, source_id, result.parsed_count, result.inserted_count, result.skipped_count,
    )
    return result


def ingest_fred_series(
    conn: DBConnection,
    series_id: str,
    *,
    unit: str,
    series_key: str | None = None,
    region: str | None = None,
) -> MacroIngestionResult:
    """Fetch one FRED series live and run it through the same validate ->
    store steps ingest_macro_file() uses for an uploaded RBI/IMD/... CSV.
    Deliberately a separate function, not a branch of ingest_macro_file():
    there's no file_path/source-detection-by-path step here (the series_id
    is the input), same reasoning ingest_yfinance_company() is its own
    function rather than a branch of ingest_file().

    fetch_fred_series() always returns a series' *entire* history (FRED's
    CSV export has no "since" param), and insert_macro_observations() is
    plain append-only (by design -- see its own docstring) with nothing
    upstream deduplicating a period already on file. Left alone, a repeat
    call for the same series_id (e.g. this app's own scheduled quarterly
    FRED job re-running) would append a duplicate row for every period,
    every run, forever. Periods already stored for this exact
    (series_key, region, source="fred") are filtered out here before
    validation/insert -- scoped to this function alone, not
    insert_macro_observations itself, since that function is shared with
    the RBI/IMD file-upload path, where re-processing the same file is a
    much rarer, usually-deliberate action rather than a job's normal
    steady-state behavior.
    """
    resolved_series_key = series_key or series_id.lower()

    # ADR-022: land the raw CSV in raw/macro/ BEFORE parsing -- dedup-by-
    # hash means a re-fetch of an unchanged series (this job's normal
    # steady-state case, since FRED's export is always the whole history)
    # creates zero new S3 writes/catalog rows; only a genuinely updated
    # series creates a new immutable object. entity is the series key
    # (not a company -- FRED has no company concept), so replay-from-S3
    # can be filtered per series the same way a company-scoped source
    # filters by company_id.
    raw_bytes = fetch_fred_series_raw(series_id)
    raw_result = store_raw_object(
        conn, source="fred", entity=resolved_series_key, object_type="fred_series_csv", period=None,
        source_url=fred_csv_url(series_id), raw_prefix="macro", content=raw_bytes, extension="csv",
    )
    parsed = parse_fred_csv(raw_bytes, series_id, unit=unit, series_key=series_key, region=region)

    result = MacroIngestionResult(series_key=resolved_series_key, source_id="fred", file_path=f"fred:{series_id}")
    result.parsed_count = len(parsed)

    existing_periods = get_existing_macro_periods(conn, resolved_series_key, region, "fred")
    new_obs = [obs for obs in parsed if obs.period not in existing_periods]
    already_have = len(parsed) - len(new_obs)
    if already_have:
        logger.info(
            "ingest_fred_series(%s): %d/%d period(s) already on file, skipping",
            series_id, already_have, len(parsed),
        )

    valid: list[MacroNormalizedObservation] = []
    for obs in new_obs:
        problems = validate_macro_observation(obs)
        if problems:
            result.skipped_count += 1
            label = f"{obs.series_key} {obs.period}"
            reason = f"{label}: {'; '.join(problems)}"
            result.skip_reasons.append(reason)
            logger.warning("Skipping invalid macro observation: %s", reason)
            continue
        valid.append(obs)

    insert_macro_observations(conn, valid)
    result.inserted_count = len(valid)
    if valid:
        _publish_dataset_ingested(
            conn,
            dataset_id=f"macro:fred:{result.series_key}",
            dataset_type="macro",
            source="fred",
            storage_reference={"table": "macro_observations"},
            scope={
                "series_keys": sorted({obs.series_key for obs in valid}),
                "regions": sorted({obs.region for obs in valid if obs.region}),
            },
            metadata={"observation_count": len(valid)},
        )

    ror.update_raw_object_state(conn, raw_result.object_id, state="ingested", mark_processed=True)
    ror.insert_lineage(
        conn, object_id=raw_result.object_id, derived_store="macro_observations",
        derived_table="macro_observations", derived_record_id=resolved_series_key,
    )

    logger.info(
        "Ingested %s (macro/fred): parsed=%d inserted=%d skipped=%d",
        series_id, result.parsed_count, result.inserted_count, result.skipped_count,
    )
    return result


def ingest_bank_infrastructure_file(
    conn: DBConnection, file_path: Path, *, source_id: str | None = None
) -> BankInfrastructureIngestionResult:
    """Run one RBI monthly bank-infrastructure bulletin (ATM/NEFT/RTGS,
    sources/rbi_bank_infrastructure.py) through parse -> store.

    A separate pipeline from ingest_macro_file(), not a branch inside it:
    this data is bank x metric x period, not a flat series x period like
    every macro_observations source, so it has its own table
    (bank_infrastructure_observations) and no shared validation step —
    the parser itself already only emits well-formed, numeric-valued rows.
    """
    source_id = source_id or detect_macro_source_from_path(file_path)
    parsed = parse_bank_infrastructure_file(file_path)

    result = BankInfrastructureIngestionResult(source_id=source_id, file_path=str(file_path))
    result.parsed_count = len(parsed)

    insert_bank_infrastructure_observations(conn, parsed)
    result.inserted_count = len(parsed)
    if parsed:
        _publish_dataset_ingested(
            conn,
            dataset_id=f"bank_infrastructure:{source_id}",
            dataset_type="bank_infrastructure",
            source=source_id,
            storage_reference={"table": "bank_infrastructure_observations"},
            scope={
                "bank_names": sorted({obs.bank_name for obs in parsed}),
                "metrics": sorted({obs.metric for obs in parsed}),
            },
            metadata={"observation_count": len(parsed)},
        )

    logger.info(
        "Ingested %s (bank_infrastructure/%s): parsed=%d inserted=%d",
        file_path, source_id, result.parsed_count, result.inserted_count,
    )
    return result
