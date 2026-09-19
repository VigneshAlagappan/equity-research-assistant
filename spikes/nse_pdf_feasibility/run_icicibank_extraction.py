"""ICICIBANK balance-sheet extraction from real NSE PDFs already in S3.

Read-only: S3 GET (boto3), Neon SELECT-only (db_readonly.get_readonly_connection,
which itself sets the session read-only + statement_timeout). Never writes to
production. Output is a local JSON file for human review, per task spec.

Hard rule: every network/DB call has an explicit timeout and a non-blocking
failure path — on any error for one quarter, log and move to the next; never
retry in a loop, never estimate/guess a value.
"""
from __future__ import annotations

import io
import json
import re
import sys
from datetime import date

import boto3
from botocore.config import Config
from pypdf import PdfReader

sys.path.insert(0, "spikes/nse_pdf_feasibility")
from db_readonly import get_readonly_connection  # noqa: E402

BUCKET = "signals-app-documents-862938824222"
OUT_PATH = "spikes/nse_pdf_feasibility/data/icicibank_balance_sheet_extraction.json"

_S3_CFG = Config(connect_timeout=15, read_timeout=30, retries={"max_attempts": 1})

def _fuzzy_word(word: str) -> str:
    """A handful of these PDFs' text layers carry a font-kerning artifact
    that inserts a single stray space mid-word (e.g. 'Capita l', 'Depos
    its', 'liabi lities') — confirmed by direct inspection of FY2024 Q1's
    extracted text, not assumed. Build a regex for `word` that tolerates at
    most one optional space between any two of its letters, so a label
    still matches whether or not that artifact hit it, without loosening
    matching enough to pick up unrelated text."""
    return r"\s?".join(re.escape(ch) for ch in word)


def _fuzzy_phrase(words: list[str]) -> str:
    """Join fuzzy-matched words with \\s+ (handles both normal spacing and
    the newline-wrapping pypdf produces for multi-word labels)."""
    return r"\s+".join(_fuzzy_word(w) for w in words)


METRIC_PATTERNS = [
    # (metric_key, compiled regex to find the ROW START, i.e. the label).
    # Every label uses _fuzzy_phrase/_fuzzy_word rather than literal text —
    # see that helper's docstring for why (both the mid-word stray-space
    # artifact and pypdf's ordinary line-wrap of multi-word labels).
    ("equity_share_capital", re.compile(
        r"(?<!Total\s)(?<!and\s)\b" + _fuzzy_word("Capital") + r"\b(?!\s+and\s+Liabilities)(?!\s+Adequacy)", re.I)),
    ("reserves", re.compile(_fuzzy_word("Reserves") + r"\s+and[\s\S]{0,20}?" + _fuzzy_word("plus"), re.I)),
    ("deposits", re.compile(r"\b" + _fuzzy_word("Deposits") + r"\b", re.I)),
    ("borrowings", re.compile(_fuzzy_word("Borrowings") + r"\s*\(", re.I)),
    ("other_liabilities", re.compile(
        _fuzzy_phrase(["Other", "liabilities"]) + r"(?:\s+and\s+" + _fuzzy_word("provisions") + r")?\b", re.I)),
    ("cash_and_bank", re.compile(
        _fuzzy_phrase(["Cash", "and", "balances", "with"]) + r"(?:\s+the)?\s+"
        + _fuzzy_phrase(["Reserve", "Bank", "of", "India"]), re.I)),
    ("investments", re.compile(r"\b" + _fuzzy_word("Investments") + r"\d{0,2}\b", re.I)),
    ("advances", re.compile(r"\b" + _fuzzy_word("Advances") + r"\b", re.I)),
    ("other_assets", re.compile(_fuzzy_phrase(["Other", "assets"]) + r"\d{0,2}\b", re.I)),
    ("total_assets", re.compile(
        _fuzzy_word("Total") + r"\s+(?:" + _fuzzy_phrase(["Capital", "and", "Liabilities"])
        + "|" + _fuzzy_word("Assets") + r")\b", re.I)),
]

MONTH_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
MON3 = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Deliberately excludes bare 1-2 digit tokens: several labels in these PDFs
# carry a footnote-reference digit glued on with no separating space right
# after the label (e.g. "Borrowings (includes subordinated debt)1 152,994"),
# and a naive "any digits" regex captures that "1" as if it were the first
# real column value, silently shifting every subsequent column by one. Real
# balance-sheet figures in these filings are always >= 3 digits (hundreds of
# crore/lac at smallest) or carry a comma/decimal, so requiring that shape
# filters footnote markers out without needing to special-case each label.
NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{2,3})+(?:\s*\.\s*\d+)?|-?\d{3,}(?:\s*\.\s*\d+)?|-?\d{1,2}\.\d+")

# Rough per-metric plausibility floors (in the stated unit, pre-conversion),
# purely a belt-and-suspenders check against mis-column extraction — never
# used to correct or estimate a value, only to reject an implausible one and
# log why, per the "never guess" rule. ICICIBANK's balance sheet is always
# well above these floors across the entire FY2016-FY2027 range in scope.
PLAUSIBILITY_FLOOR = {
    "equity_share_capital": 50,
    "reserves": 1000,
    "deposits": 10000,
    "borrowings": 1000,
    "other_liabilities": 500,
    "cash_and_bank": 500,
    "investments": 5000,
    "advances": 10000,
    "other_assets": 500,
    "total_assets": 50000,
}


def parse_number(tok: str) -> float | None:
    tok = tok.replace(" ", "").replace(",", "")
    try:
        return float(tok)
    except ValueError:
        return None


def find_dates_in_window(window: str) -> list[date]:
    """Best-effort ordered list of dates appearing in a header window,
    handling both contiguous 'Month DD, YYYY' / 'DD-Mon-YY' forms and the
    split form pypdf sometimes produces ('June March June ... 30,2019
    31,2019 30,2018') where month names and day,year pairs land in two
    separate groups in the same left-to-right order."""
    # Form A: contiguous "Month DD, YYYY"
    contiguous = re.findall(MONTH_RE + r"\s+(\d{1,2}),?\s*(\d{4})", window, re.I)
    # Form B: "DD-Mon-YY" or "DD-Mon-YYYY"
    dashform = re.findall(r"\b(\d{1,2})-([A-Za-z]{3})-(\d{2,4})\b", window)
    # Form C: split — month names as one group, "DD,YYYY" or "DD, YYYY" as another
    months_only = re.findall(MONTH_RE, window, re.I)
    daypairs = re.findall(r"\b(\d{1,2}),\s*(\d{4})\b", window)

    # Build every candidate reading, then take whichever found the MOST
    # columns — never just "the first form that found anything". A single
    # accidental contiguous match is a real, observed failure mode here:
    # in the split layout ("Particulars June March June \n30,2019 31,2019
    # 30,2018"), the LAST month name in the header sits immediately before
    # the FIRST day/year pair in the raw text, so Form A's "Month \s+
    # DD,YYYY" pattern spuriously fires once (pairing "June" with "30,2019"
    # even though they're different columns) and used to short-circuit
    # before the correct 3-column split-form reading was ever tried.
    candidates: list[list[date]] = []

    if contiguous:
        ds = []
        for mon, day, yr in contiguous:
            m = MONTHS.get(mon.lower())
            if m:
                try:
                    ds.append(date(int(yr), m, int(day)))
                except ValueError:
                    pass
        if ds:
            candidates.append(ds)

    if dashform:
        ds = []
        for day, mon, yr in dashform:
            m = MON3.get(mon.lower())
            if m:
                yr_i = int(yr)
                yr_i = 2000 + yr_i if yr_i < 100 else yr_i
                try:
                    ds.append(date(yr_i, m, int(day)))
                except ValueError:
                    pass
        if ds:
            candidates.append(ds)

    if months_only and daypairs and len(months_only) == len(daypairs):
        ds = []
        for mon, (day, yr) in zip(months_only, daypairs):
            m = MONTHS.get(mon.lower())
            if m:
                try:
                    ds.append(date(int(yr), m, int(day)))
                except ValueError:
                    pass
        if ds:
            candidates.append(ds)

    if not candidates:
        return []
    return max(candidates, key=len)


def detect_unit(window: str) -> str | None:
    m = re.search(r"in\s*(crore|lac|lakh)s?\b", window, re.I)
    if m:
        w = m.group(1).lower()
        return "crore" if w == "crore" else "lac"
    if re.search(r"crore", window, re.I):
        return "crore"
    if re.search(r"\blac|lakh", window, re.I):
        return "lac"
    return None


def find_balance_sheet_tables(text: str) -> list[dict]:
    """Locate each real balance-sheet table in the doc. Returns list of
    dicts: {statement_type, anchor_idx, cap_liab_idx, header_window,
    body_text}. statement_type may be None if undetermined (caller decides
    default only when there's exactly one table in the whole doc)."""
    tables = []
    # anchors: any "... BALANCE SHEET" occurrence (title line), in doc order
    for m in re.finditer(r"BALANCE SHEET", text, re.I):
        anchor_idx = m.start()
        preceding = text[max(0, anchor_idx - 80):anchor_idx]
        # Fuzzy-matched (same mid-word stray-space artifact as the metric
        # labels — confirmed directly: FY2020 Q1's title text reads
        # "SUMMARISED ST AN DALO NE BALANCE SHEET", which a literal
        # \bSTANDALONE\b never matches, silently dropping that whole table
        # to "type undetermined" before this fix).
        un_re = _fuzzy_word("UNCONSOLIDATED")
        con_re = _fuzzy_word("CONSOLIDATED")
        stand_re = _fuzzy_word("STANDALONE")
        if re.search(con_re, preceding, re.I) and not re.search(un_re, preceding, re.I):
            stype = "consolidated"
        elif re.search(stand_re, preceding, re.I) or re.search(un_re, preceding, re.I):
            stype = "standalone"
        else:
            stype = None

        # find the real "Capital and Liabilities" row header within next 1500 chars,
        # skipping any "Total ..." occurrence.
        search_from = anchor_idx
        cap_liab_idx = None
        cap_liab_pattern = _fuzzy_phrase(["Capital", "and", "Liabilities"])
        for cm in re.finditer(cap_liab_pattern, text[search_from:search_from + 1500], re.I):
            idx = search_from + cm.start()
            preceding2 = text[max(0, idx - 8):idx]
            if re.search(r"total\s*$", preceding2, re.I):
                continue
            cap_liab_idx = idx
            break
        if cap_liab_idx is None:
            continue

        header_window = text[anchor_idx:cap_liab_idx]
        body_text = text[cap_liab_idx:cap_liab_idx + 2500]
        tables.append({
            "statement_type": stype,
            "anchor_idx": anchor_idx,
            "cap_liab_idx": cap_liab_idx,
            "header_window": header_window,
            "body_text": body_text,
        })
    # Dedup: keep first occurrence per (statement_type or None) — formal
    # statement tables always appear before any informal press-release
    # duplicate later in these documents (verified by manual inspection).
    seen_types = set()
    deduped = []
    for t in tables:
        key = t["statement_type"]
        if key in seen_types:
            continue
        seen_types.add(key)
        deduped.append(t)
    return deduped


def extract_row_values(body_text: str, label_re: re.Pattern, n_cols: int) -> tuple[list[float], str] | None:
    m = label_re.search(body_text)
    if not m:
        return None
    # collapse whitespace from just after the label to the next label-ish
    # newline boundary, but simplest: take next 200 chars, normalize
    # whitespace, then read the first n_cols numeric tokens.
    tail = body_text[m.end():m.end() + 250].replace("\n", " ")
    tail = re.sub(r"\s+", " ", tail)
    nums = NUM_RE.findall(tail)
    if len(nums) < n_cols:
        return None
    excerpt_start = max(0, m.start() - 20)
    excerpt = body_text[excerpt_start:m.end() + 120].replace("\n", " ")
    excerpt = re.sub(r"\s+", " ", excerpt).strip()[:150]
    vals = []
    for tok in nums[:n_cols]:
        v = parse_number(tok)
        if v is None:
            return None
        vals.append(v)
    return vals, excerpt


FY_QUARTER_TO_MONTHRANGE = {
    "Q1": (4, 6), "Q2": (7, 9), "Q3": (10, 12), "Q4": (1, 3),
}


def process_document(doc: dict, existing_canonical: set[tuple], conn) -> tuple[list[dict], list[str]]:
    facts: list[dict] = []
    skips: list[str] = []
    fy, q = doc["fiscal_year"], doc["quarter"]
    target_period_end = doc["period_end"]  # 'YYYY-MM-DD' string
    try:
        target_date = date.fromisoformat(target_period_end)
    except ValueError:
        skips.append(f"{fy} {q}: unparseable period_end {target_period_end!r}")
        return facts, skips

    client = boto3.client("s3", region_name="us-east-1", config=_S3_CFG)
    try:
        resp = client.get_object(Bucket=BUCKET, Key=doc["storage_object_key"])
        pdf_bytes = resp["Body"].read()
    except Exception as exc:  # noqa: BLE001 - must never crash the batch
        skips.append(f"{fy} {q}: S3 GET failed ({exc})")
        return facts, skips

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # noqa: BLE001
        skips.append(f"{fy} {q}: PDF parse failed ({exc})")
        return facts, skips

    if len(text.strip()) < 200:
        skips.append(f"{fy} {q}: no extractable text layer (scanned image) — {len(text.strip())} chars")
        return facts, skips

    tables = find_balance_sheet_tables(text)
    if not tables:
        skips.append(f"{fy} {q}: no 'Capital and Liabilities' balance-sheet table found in text")
        return facts, skips

    # if only one table and its type is undetermined, default to standalone
    # (matches doc-wide boilerplate: filings are "on an unconsolidated basis
    # ... unless specifically stated to be on a consolidated basis" and the
    # earlier-era single-table filings never carry a second consolidated
    # table) — logged explicitly either way.
    resolved_tables = []
    for t in tables:
        stype = t["statement_type"]
        ambiguous_note = None
        if stype is None:
            if len(tables) == 1:
                stype = "standalone"
                ambiguous_note = "no STANDALONE/CONSOLIDATED header found; only one balance-sheet table in doc -> defaulted to standalone per filing boilerplate"
            else:
                ambiguous_note = "no STANDALONE/CONSOLIDATED header found and multiple tables present -> skipped, could not resolve"
        resolved_tables.append({**t, "resolved_statement_type": stype, "ambiguous_note": ambiguous_note})

    for t in resolved_tables:
        stype = t["resolved_statement_type"]
        if stype is None:
            skips.append(f"{fy} {q}: {t['ambiguous_note']}")
            continue
        if t["ambiguous_note"]:
            skips.append(f"{fy} {q} {stype}: {t['ambiguous_note']} (proceeded)")

        unit = detect_unit(t["header_window"] + t["body_text"][:150])
        if unit is None:
            skips.append(f"{fy} {q} {stype}: could not determine unit (crore/lac) near table — skipped whole table")
            continue

        dates = find_dates_in_window(t["header_window"])
        if not dates:
            skips.append(f"{fy} {q} {stype}: could not parse column header dates — skipped whole table")
            continue

        if target_date not in dates:
            skips.append(
                f"{fy} {q} {stype}: target period_end {target_date} not found among parsed column dates {dates} — skipped whole table"
            )
            continue
        col_idx = dates.index(target_date)
        n_cols = len(dates)

        for metric_key, label_re in METRIC_PATTERNS:
            row = extract_row_values(t["body_text"], label_re, n_cols)
            if row is None:
                skips.append(f"{fy} {q} {stype} {metric_key}: line item not found or fewer numeric columns than expected — skipped")
                continue
            vals, excerpt = row
            if col_idx >= len(vals):
                skips.append(f"{fy} {q} {stype} {metric_key}: column index {col_idx} out of range for parsed values {vals}")
                continue
            raw_value = vals[col_idx]

            floor = PLAUSIBILITY_FLOOR.get(metric_key, 0)
            if unit == "lac":
                floor *= 100  # 1 crore = 100 lac, so the lac-unit floor scales up
            if abs(raw_value) < floor:
                skips.append(
                    f"{fy} {q} {stype} {metric_key}: extracted value {raw_value} {unit} is below plausibility "
                    f"floor {floor} (likely a mis-parsed footnote/column) — skipped, not guessed"
                )
                continue

            key = (fy, q, stype, metric_key)
            if key in existing_canonical:
                skips.append(f"{fy} {q} {stype} {metric_key}: canonical value already exists — skipped (gap-fill only)")
                continue

            value_in_inr_crore = raw_value if unit == "crore" else round(raw_value * 0.01, 4)

            facts.append({
                "fiscal_year": fy,
                "quarter": q,
                "statement_type": stype,
                "metric_key": metric_key,
                "value": raw_value,
                "unit_before_conversion": unit,
                "value_in_inr_crore": value_in_inr_crore,
                "document_id": doc["document_id"],
                "source_url": doc["source_url"],
                "period_end_matched": str(target_date),
                "column_index": col_idx,
                "columns_detected": [str(d) for d in dates],
                "raw_text_excerpt": excerpt,
            })

    return facts, skips


def fetch_existing_canonical(conn) -> set[tuple]:
    """All existing canonical_financials keys for ICICIBANK relevant to our
    metric vocabulary — single SELECT, read-only, used purely to exclude
    already-covered facts (gap-fill only, same rule as the pilot)."""
    metric_keys = [m for m, _ in METRIC_PATTERNS]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fiscal_year, quarter, statement_type, metric_key
            FROM canonical_financials
            WHERE company_id = 'ICICIBANK' AND period_type = 'quarterly'
              AND metric_key = ANY(%s)
            """,
            (metric_keys,),
        )
        rows = cur.fetchall()
    return {(r[0], r[1], r[2], r[3]) for r in rows}


def main():
    with open("/tmp/icici_docs.json") as f:
        docs = json.load(f)

    # Skip the pilot quarter (FY2017 Q1) — already manually done and live.
    docs = [d for d in docs if not (d["fiscal_year"] == "FY2017" and d["quarter"] == "Q1")]

    conn = get_readonly_connection()
    try:
        existing_canonical = fetch_existing_canonical(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch existing canonical_financials ({exc}); proceeding with empty exclusion set", file=sys.stderr)
        existing_canonical = set()
    finally:
        conn.close()

    all_facts: list[dict] = []
    all_skips: list[str] = []
    processed_quarters = []
    zero_table_quarters = []

    # Reuse one boto3 client's underlying connection pool implicitly via
    # per-call client construction inside process_document (kept simple —
    # this is a one-shot batch script, not a long-lived service).
    conn2 = None  # not needed again; process_document doesn't need conn per-doc
    for doc in docs:
        fy, q = doc["fiscal_year"], doc["quarter"]
        facts, skips = process_document(doc, existing_canonical, conn2)
        processed_quarters.append(f"{fy} {q}")
        if not facts and skips and all("no extractable text" in s or "no 'Capital and Liabilities'" in s for s in skips):
            zero_table_quarters.append(f"{fy} {q}")
        all_facts.extend(facts)
        all_skips.extend(skips)

    summary = {
        "quarters_in_scope": len(docs),
        "quarters_with_at_least_one_fact": len({(f["fiscal_year"], f["quarter"]) for f in all_facts}),
        "total_facts_extracted": len(all_facts),
        "quarters_fully_skipped_no_table_or_text": zero_table_quarters,
        "processed_quarters": processed_quarters,
    }

    output = {"summary": summary, "facts": all_facts, "skip_log": all_skips}
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(json.dumps(summary, indent=2))
    print(f"\nWrote {len(all_facts)} facts, {len(all_skips)} skip entries -> {OUT_PATH}")


if __name__ == "__main__":
    main()
