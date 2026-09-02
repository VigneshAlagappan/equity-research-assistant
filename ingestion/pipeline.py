"""Ingestion pipeline: detect -> parse -> validate -> normalize -> store -> index.

(README: Ingestion Approach by Source — the generic flow every source follows,
regardless of which adapter runs. "Normalize" happens inside the adapter via
normalization/financials.py; this module owns validate -> store -> reconcile.)
"""

from __future__ import annotations

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
from sources.fred import fetch_fred_series
from sources.iitm_rainfall import parse_iitm_file
from sources.rbi_indicators import looks_like_rbi_indicator_workbook, parse_rbi_indicator_workbook
from sources.yfinance_financials import YFinanceAdapter
from storage.repositories import (
    compute_reconciliation_keys,
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

    logger.info(
        "Ingested %s (yfinance): parsed=%d inserted=%d skipped=%d reconciled=%d",
        ticker, result.parsed_count, result.inserted_count, result.skipped_count, result.reconciled_count,
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
    """
    parsed = fetch_fred_series(series_id, unit=unit, series_key=series_key, region=region)

    result = MacroIngestionResult(
        series_key=series_key or series_id.lower(), source_id="fred", file_path=f"fred:{series_id}"
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
