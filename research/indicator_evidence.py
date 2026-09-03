"""Deterministic triggered indicators as investigation Evidence — the
"Trusted Facts / **Indicators** / Question" half of the research loop's
starting point.

The Configurable Indicator Framework (`indicators/`) already turns canonical
facts into deterministic, rule-based, versioned, provenanced findings
("Promoter holding fell 2.1pp over the last two quarters", "Net profit fell
18% YoY"). Until this module existed those findings were a company-page
presentation layer only: the hypothesis generator and the investigation
planner never saw them, so an investigation re-derived from raw series what
a frozen rule had already established, and could not cite the rule.

This module is the adapter, and only the adapter: it runs nothing new and
decides nothing. It calls the evaluation engine through the same `FactStore`
seam the rest of research/ uses, and renders each `TriggeredIndicator` as a
CALCULATION `Evidence` line — CALCULATION rather than FACT because an
indicator is a deterministic computation over facts, not a reported figure,
and the distinction is exactly the one Step 2G is told never to blur. The
citation carries the rule id, its version and the rule's own provenance
string, so a conclusion resting on an indicator is reproducible: the same
rule version over the same facts yields the same line.

Read-only by design (`persist=False`): the append-only
`indicator_evaluations` audit trail records what a *user* was shown on a
company page, and an investigation gathering evidence is not that event.
An investigation must never mutate the indicator audit trail as a
side effect of reading it.

Point-in-time: indicator rules evaluate against the latest facts on file and
have no `as_of` concept, so under a cutoff this capability returns nothing
rather than leaking post-cutoff findings — see
`research/capabilities.py::default_capabilities`.
"""

from __future__ import annotations

import logging

from research.evidence import Evidence
from storage.db_types import DBConnection
from storage.fact_store import FactStore, default_fact_store

logger = logging.getLogger(__name__)

#: Evidence lines are handed to the LLM verbatim; a rule's `facts` mapping is
#: small by construction, but this bounds a pathological one.
_MAX_FACT_PAIRS = 8


def _render_facts(facts) -> str:
    pairs = []
    for key, value in list(dict(facts).items())[:_MAX_FACT_PAIRS]:
        if isinstance(value, float):
            pairs.append(f"{key}={value:,.2f}")
        else:
            pairs.append(f"{key}={value}")
    return ", ".join(pairs)


def get_indicator_evidence(
    conn: DBConnection, company_id: str, *, user_id: int | None = None, fact_store: FactStore | None = None
) -> list[Evidence]:
    """Every currently-triggered indicator for this company, as Evidence.

    `user_id=None` means "the system defaults" — no user's configuration
    overrides are applied, which is the right grounding for an investigation
    (a conclusion should not silently depend on whose thresholds happened to
    be in effect). Returns [] and logs if the indicator layer raises: a
    failing rule engine must degrade the evidence available to an
    investigation, never fail the investigation.
    """
    fs = fact_store or default_fact_store()
    try:
        from indicators.evaluation import evaluate_company_indicators

        triggered = evaluate_company_indicators(
            conn, company_id, user_id=user_id, fact_store=fs, persist=False
        )
    except Exception:  # noqa: BLE001 — evidence gathering degrades, it never fails the investigation
        logger.exception("Indicator evaluation failed for %s — continuing without indicator evidence", company_id)
        return []

    evidence: list[Evidence] = []
    for indicator in triggered:
        period = f" [{indicator.period_label}]" if indicator.period_label else ""
        citation_parts = [
            f"indicator rule {indicator.rule_id} v{indicator.rule_version}",
            f"classification={indicator.classification}",
            f"severity={indicator.severity}",
        ]
        if indicator.threshold_summary:
            citation_parts.append(indicator.threshold_summary)
        if indicator.provenance:
            citation_parts.append(indicator.provenance)
        facts = _render_facts(indicator.facts)
        evidence.append(
            Evidence(
                kind="CALCULATION",
                company_id=company_id,
                label=f"Indicator: {indicator.rule_name}{period}",
                value=indicator.explanation + (f" ({facts})" if facts else ""),
                citation=" · ".join(citation_parts),
            )
        )
    return evidence
