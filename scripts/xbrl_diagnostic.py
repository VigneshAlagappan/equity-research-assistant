"""Milestone-1 diagnostic for sources/xbrl_generic.py's generic XBRL parser.

Prints a validation summary (context/fact/unit counts, dimensional-fact
count, earliest/latest reporting date, company/scrip/quarter/FY) and the
milestone-1 line items (revenue, net profit, EPS, cash, total assets, total
liabilities, equity, operating cash flow — each with concept/value/period/
context/unit shown) for one XBRL filing, then writes the full generic
{filing, units, contexts, facts} JSON alongside it under
data/normalized/xbrl_generic/ (mirroring the input file's path under
data/raw/).

This script exists only to validate the generic parser end-to-end against a
real filing — the parser itself (sources/xbrl_generic.py) stays taxonomy-
generic; nothing here feeds financial_observations or any other DB table.

Usage:
  python -m scripts.xbrl_diagnostic data/raw/INFY/nse/2026-03-31_consolidated_152465.xml
  python -m scripts.xbrl_diagnostic <file> --output /tmp/parsed.json
  python -m scripts.xbrl_diagnostic <file> --no-write
(a plain `python scripts/xbrl_diagnostic.py` fails on the `sources` import
below -- run as a module so the repo root, not scripts/, lands on
sys.path, same as every other script here.)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import settings
from sources.xbrl_generic import build_validation_summary, parse_xbrl_document

#: (display label, canonical_metric, period_length filter — None means "take
#: the first numeric match regardless of period length", used for line
#: items this taxonomy only ever reports one version of, e.g. operating
#: cash flow being YTD-only).
_MILESTONE_LINE_ITEMS: list[tuple[str, str, str | None]] = [
    ("Revenue — quarter", "revenue", "quarterly"),
    ("Revenue — FY/YTD", "revenue", "annual"),
    ("Net profit — quarter", "net_profit", "quarterly"),
    ("Net profit — FY/YTD", "net_profit", "annual"),
    ("EPS", "eps", None),
    ("Cash", "cash", None),
    ("Total assets", "total_assets", None),
    ("Total liabilities", "total_liabilities", None),
    ("Equity", "equity", None),
    ("Operating cash flow", "operating_cash_flow", None),
]


def _default_output_path(file_path: Path) -> Path:
    try:
        relative = file_path.resolve().relative_to(settings.RAW_DIR.resolve())
    except ValueError:
        relative = Path(file_path.name)
    return (settings.NORMALIZED_DIR / "xbrl_generic" / relative).with_suffix(".json")


def _find_fact(facts: list[dict], canonical_metric: str, period_length: str | None) -> dict | None:
    for fact in facts:
        if fact["canonical_metric"] != canonical_metric or not fact["is_numeric"]:
            continue
        if period_length is not None and fact["period_length"] != period_length:
            continue
        return fact
    return None


def print_diagnostic(parsed: dict) -> None:
    filing = parsed["filing"]
    summary = build_validation_summary(parsed)

    print("=== Filing ===")
    print(f"Company: {filing['company_name']}")
    print(f"Scrip code: {filing['scrip_code']}")
    print(f"Reporting period: {filing['reporting_quarter']} ({filing['type_of_reporting_period']})")
    print(f"Financial year: {filing['financial_year_start']} -> {filing['financial_year_end']}")
    print(f"Currency: {filing['currency']}  Scale: {filing['scale']}  Consolidation: {filing['consolidation']}")

    print("\n=== Validation summary ===")
    print(f"Contexts: {summary['num_contexts']}")
    print(f"Facts: {summary['num_facts']}")
    print(f"Units: {summary['num_units']}")
    print(f"Dimensional facts: {summary['num_dimensional_facts']}")
    print(f"Reporting dates span: {summary['earliest_date']} .. {summary['latest_date']}")

    print("\n=== Milestone-1 line items ===")
    for label, metric, period_length in _MILESTONE_LINE_ITEMS:
        fact = _find_fact(parsed["facts"], metric, period_length)
        if fact is None:
            print(f"{label}: NOT FOUND")
            continue
        period = fact["instant_date"] or f"{fact['period_start']} -> {fact['period_end']}"
        print(
            f"{label}: {fact['normalized_value']} {fact['unit'] or ''} "
            f"[concept={fact['concept']} context={fact['context_id']} period={period}]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Path to an XBRL instance document (.xml)")
    parser.add_argument(
        "--output",
        help="Where to write the parsed {filing,units,contexts,facts} JSON "
        "(default: data/normalized/xbrl_generic/<path mirrored from data/raw/>.json)",
    )
    parser.add_argument("--no-write", action="store_true", help="Print the diagnostic only, don't write JSON")
    args = parser.parse_args()

    file_path = Path(args.file)
    parsed = parse_xbrl_document(file_path)
    print_diagnostic(parsed)

    if not args.no_write:
        output_path = Path(args.output) if args.output else _default_output_path(file_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(parsed, indent=2))
        print(f"\nWrote {output_path}")


if __name__ == "__main__":
    main()
