"""Signals Eval Set — a small, versioned set of golden questions spanning
all 5 Jev complexity levels (docs/ADR/023), used by
scripts/run_signals_eval.py to answer, on a recurring basis: "did Jev
classify this question the way we expect it to?"

Level 5 cases reuse the exact companies/questions already verified against
real ingested data in docs/SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md
(HDFCBANK, ICICIBANK, IDFCFIRSTB all have real financials/documents on
file) rather than inventing new ones — one less thing that can fail for
"no data ingested", a data-coverage problem, not a "Jev got it wrong"
problem the eval is actually trying to catch.

Deliberately small: Level 5 cases run the FULL hypothesis-driven
investigation pipeline (research/investigation.py) — several real LLM
calls and minutes of wall-clock time each, real recurring spend if this
is scheduled to run unattended (same warning scripts/
batch_generate_insights.py's own docstring gives for its own monthly LLM
job). Extend this list deliberately, not by reflex — and prefer widening
coverage of an under-represented level over duplicating an existing one.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm.complexity import ComplexityLevel


@dataclass(frozen=True)
class EvalCase:
    #: Stable identifier — becomes the batch_job_items row's company_id
    #: column (BatchRun's per-item key isn't company-specific, just a
    #: label), so it must stay unique and should stay stable across edits
    #: (renaming a case loses its run history's identity, same as renaming
    #: a job_name would).
    name: str
    question: str
    company_ids: list[str]
    expected_level: ComplexityLevel


EVAL_CASES: list[EvalCase] = [
    # ---- Level 1 -- Retrieve: a single reported fact, no calculation ----
    EvalCase(
        "l1_hdfc_net_profit_fy24", "What was HDFC Bank's net profit in FY2024?",
        ["HDFCBANK"], ComplexityLevel.RETRIEVE,
    ),
    EvalCase(
        "l1_icici_net_profit_fy24", "What was ICICI Bank's net profit in FY2024?",
        ["ICICIBANK"], ComplexityLevel.RETRIEVE,
    ),
    # ---- Level 2 -- Calculate: one deterministic calculation over reported facts ----
    EvalCase(
        "l2_hdfc_5y_cagr", "What was HDFC Bank's 5-year net profit CAGR?",
        ["HDFCBANK"], ComplexityLevel.CALCULATE,
    ),
    EvalCase(
        "l2_idfcfirstb_yoy_growth",
        "What was IDFC FIRST Bank's year-over-year net profit growth in FY2024?",
        ["IDFCFIRSTB"], ComplexityLevel.CALCULATE,
    ),
    # ---- Level 3 -- Interpret: one company's own data, no external comparison ----
    EvalCase(
        "l3_hdfc_profit_trend", "Analyze HDFC Bank's profit growth over the last five years.",
        ["HDFCBANK"], ComplexityLevel.INTERPRET,
    ),
    EvalCase(
        "l3_idfcfirstb_asset_quality", "How has IDFC FIRST Bank's asset quality trended recently?",
        ["IDFCFIRSTB"], ComplexityLevel.INTERPRET,
    ),
    # ---- Level 4 -- Compare: requires another dataset (peer/industry/macro) ----
    EvalCase(
        "l4_hdfc_vs_industry", "Compare HDFC Bank's credit growth with the broader banking industry.",
        ["HDFCBANK"], ComplexityLevel.COMPARE,
    ),
    EvalCase(
        "l4_icici_vs_hdfc_profitability", "How does ICICI Bank's profitability compare to HDFC Bank?",
        ["ICICIBANK", "HDFCBANK"], ComplexityLevel.COMPARE,
    ),
    # ---- Level 5 -- Hypothesize: causal reasoning, competing explanations ----
    # (docs/SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md questions #1, #2, #4 verbatim)
    EvalCase(
        "l5_hdfc_post_merger_profitability",
        "Why has HDFC Bank's profitability changed following the merger? Evaluate competing "
        "explanations such as funding/deposit costs, NIM, loan growth, asset quality, operating "
        "costs, and merger-related balance-sheet effects.",
        ["HDFCBANK"], ComplexityLevel.HYPOTHESIZE,
    ),
    EvalCase(
        "l5_idfcfirstb_growth_sustainability",
        "Is IDFC FIRST Bank's growth translating into sustainable profitability, or are credit "
        "costs, funding characteristics, asset quality, or other factors creating risks beneath "
        "the growth?",
        ["IDFCFIRSTB"], ComplexityLevel.HYPOTHESIZE,
    ),
    EvalCase(
        "l5_hdfc_vs_icici_divergence",
        "Why have HDFC Bank and ICICI Bank performed differently? Evaluate factors such as credit "
        "growth, deposits/funding, NIM, asset quality, operating efficiency, fee mix, "
        "profitability, and capital allocation, and distinguish company-specific factors from "
        "sector-wide effects.",
        ["HDFCBANK", "ICICIBANK"], ComplexityLevel.HYPOTHESIZE,
    ),
]
