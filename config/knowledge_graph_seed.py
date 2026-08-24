"""Curated domain-knowledge edges for context/graph.py — relationships
between metrics/macro variables that no table in this app derives on its own
(unlike sector-peer or investigation-discussed-metric edges, which are
computed live from data that already exists). This is a hand-maintained
list, the same pattern as config/settings.py's DEFAULT_SOURCES: start small,
extend it as you learn the domain. Every metric_key referenced on the
company-metric side must be a real key from financials/report.py's
TREND_METRICS/VENDOR_RATIO_METRICS — context/graph.py matches against those.

Each entry: (source, relationship_type, target, strength, reason)
  - source/target: a metric_key (financials/report.py) or a macro variable
    name (descriptive only today — not yet joined against
    macro_observations.series_key; see architecture.md's Known Gaps)
  - relationship_type: currently only "AFFECTS" is used
  - strength: 0-1, how strong the causal link is — feeds
    context/graph.py's path scoring (README §7: relationship_strength)
  - reason: the actual domain reasoning, shown in the LLM prompt and in
    the traversal path explanation, so a suggested connection is never a
    black box
"""

from __future__ import annotations

KNOWLEDGE_GRAPH_SEED_EDGES: list[tuple[str, str, str, float, str]] = [
    (
        "rbi_repo_rate", "AFFECTS", "net_interest_margin", 0.7,
        "repo rate moves propagate to bank funding cost and deposit pricing, which drives NIM",
    ),
    (
        "casa_percent", "AFFECTS", "net_interest_margin", 0.6,
        "a higher CASA (low-cost deposit) mix lowers funding cost, supporting NIM",
    ),
    (
        "gross_npa_percent", "AFFECTS", "return_on_equity_percent", 0.5,
        "rising gross NPAs increase provisioning expense, compressing ROE",
    ),
    (
        "net_npa_percent", "AFFECTS", "return_on_equity_percent", 0.5,
        "net NPA growth signals credit-cost pressure that ultimately hits ROE",
    ),
    (
        "advances", "AFFECTS", "net_interest_margin", 0.4,
        "loan mix/growth shifts the earning-asset yield that determines NIM",
    ),
    (
        "deposits", "AFFECTS", "casa_percent", 0.4,
        "how deposit growth splits between CASA and term deposits drives the CASA ratio",
    ),
]
