"""Entity resolution (Step 2B follow-up) — decides whether a free-form
`Company`-type name the Knowledge Builder (Step 2A) extracted from a
document's text is genuinely the same real-world company as the document's
own registered `company_id`, so extraction stops silently creating a second
`knowledge_entities` row for the one company a document is already scoped
to.

A read-only query against the real `data/equity_research.db` (see the
implementation plan this module ships as part of) found 127 companies with
duplicate `Company`-type entity rows — but most of those duplicates are
NOT simple spelling variants of the same company. `ADANIPOWER` has 17 rows
including genuine, distinct subsidiaries ("Korba West Power Company
Limited", "Adani Power Dahej Ltd."), and `ADANIENT` includes an auditor
("M/s. Dharmesh Parikh & Co.") and garbled extraction noise. Merging any of
those into the parent company would silently destroy a real, correctly-
extracted distinction.

So `is_same_company_identity()` is deliberately narrow: an EXACT match,
after normalization, against one of the company's own known identifiers —
never a similarity/fuzzy score. Under-merging (leaving a genuine duplicate
unmerged, e.g. "ACC" for AMBUJACEM) is the accepted, safe failure mode;
over-merging a real subsidiary into its parent is not.
"""

from __future__ import annotations

import re

from storage.db_types import Row

#: One trailing corporate suffix is stripped (not multiple, not mid-string
#: occurrences) — "Ambuja Cements Limited" -> "ambuja cements", but a
#: genuinely different entity whose own name happens to end the same way
#: (there are none in the real duplicate set this was checked against)
#: isn't silently conflated by stripping something the actual company name
#: also legitimately ends with.
_CORPORATE_SUFFIXES = ("limited", "ltd", "inc", "corp", "plc")

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace, strip one
    trailing corporate suffix. Shared by both sides of the comparison so
    "AMBUJA CEMENTS LIMITED" and a stored "Ambuja Cements Ltd." identifier
    normalize to the identical string."""
    if not text:
        return ""
    candidate = _PUNCTUATION_RE.sub(" ", text.lower())
    candidate = _WHITESPACE_RE.sub(" ", candidate).strip()
    for suffix in _CORPORATE_SUFFIXES:
        if candidate.endswith(f" {suffix}"):
            candidate = candidate[: -(len(suffix) + 1)].strip()
            break
    return candidate


def is_same_company_identity(entity_name: str, company_row: Row) -> bool:
    """True only if `entity_name` (a free-form name the Knowledge Builder
    extracted) is an EXACT match, after normalization, against this
    company's own `legal_name`, `display_name`, `nse_symbol`, `bse_code`, or
    `company_id` — never a similarity score. A subsidiary, auditor, or
    extraction-noise name (genuinely different from the parent company)
    correctly returns False here, even though it shares the same
    `company_id` scope in `knowledge_entities` — that scoping means "this
    document is about this company," not "this entity IS this company.\""""
    normalized_name = _normalize(entity_name)
    if not normalized_name:
        return False
    candidates = (
        company_row["legal_name"],
        company_row["display_name"],
        company_row["nse_symbol"],
        company_row["bse_code"],
        company_row["company_id"],
    )
    return any(normalized_name == _normalize(candidate) for candidate in candidates)
