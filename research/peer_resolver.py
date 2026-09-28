"""Level 4 ("Compare + Contextualize") grounding -- resolves an ambiguous
comparison term in a question ("industry", "peers", "the market",
"benchmark", "competitors") into concrete peer company_ids, per
docs/ADR/023's Level 4 rules:

  - interpret ambiguous terms such as "industry"/"market"/"peers"/"benchmark";
  - ground that interpretation against entities Signals actually knows;
  - allow a bounded refinement, never an unlimited agent loop;
  - prefer one strong comparison dataset, cap at
    config.settings.MAX_COMPARISON_DATASETS unless the question explicitly
    asks for broader analysis;
  - never invent an unavailable benchmark or relationship;
  - state the limitation when data is unavailable, rather than guessing.

Grounding here is deterministic (companies.registry.list_companies_by_sector_field,
reading companies.basic_industry/macro_economic_sector -- the same
sector-classification columns context/graph.py's own sector-peer traversal
already uses, Neo4j-backed when config.settings.GRAPH_BACKEND="neo4j" and
falling back to the SQLite path otherwise), not a second LLM call: the
anchor company's own sector is already a known, closed value once
company_ids names it, so there's nothing genuinely ambiguous left for an
LLM to interpret in the common case -- ADR-006's "prefer deterministic code
over an LLM wherever a stable rule exists" still applies to the GROUNDING
step even though Jev (llm/complexity.py) already used an LLM to decide this
question needed Level 4 in the first place. A question naming a macro
benchmark (e.g. "compare X's credit growth to the RBI repo rate") is
already grounded by research/macro_evidence.py's own existing LLM-based
series planner, invoked generically for every level via
research.assistant.gather_evidence -- this module only owns the
company-peer half of Level 4 grounding, not macro series selection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from companies.registry import get_company, list_companies_by_sector_field
from config.settings import GRAPH_BACKEND, MAX_COMPARISON_DATASETS
from storage.db_types import DBConnection

# Any of these signals the question wants SOME comparison group, without
# naming concrete peer companies itself -- if company_ids already names
# more than one company, the comparison group is already explicit and this
# module has nothing to add.
_COMPARISON_TERM_RE = re.compile(
    r"\b(industry|sector|peers?|market|benchmark|competitors?|banking system)\b", re.IGNORECASE
)
# Lifts the MAX_COMPARISON_DATASETS cap -- "the user explicitly requests
# broader analysis" (policy, Level 4 rules).
_BROADER_SCOPE_RE = re.compile(r"\b(all|every|entire|broader|whole|whole industry)\b", re.IGNORECASE)

_SECTOR_FIELDS_BY_PREFERENCE = ("basic_industry", "macro_economic_sector")


@dataclass(frozen=True)
class ComparisonResolution:
    peer_company_ids: list[str] = field(default_factory=list)
    grounding_field: str | None = None
    grounding_value: str | None = None
    #: True whenever grounding was attempted against the graph-backed
    #: sector-peer traversal, regardless of which concrete backend served it
    #: (context/graph.py falls back to its own SQLite traversal if Neo4j is
    #: configured but unreachable) -- reflects config intent, not a live
    #: connectivity check, same as the rest of this app's GRAPH_BACKEND use.
    neo4j_used: bool = False
    planner_used: bool = False
    #: Non-empty exactly when Level 4 must "state the limitation" per
    #: policy -- no comparison group could be grounded, or one had to be
    #: truncated to the cap.
    notes: list[str] = field(default_factory=list)


def resolve_comparison_group(conn: DBConnection, company_ids: list[str], question: str) -> ComparisonResolution:
    """No-op (empty resolution) when the question doesn't ask for an
    implicit comparison group at all, or already names >1 company itself --
    in the latter case the caller's own company_ids already IS the
    comparison group, so there's nothing this module should add to it."""
    if len(company_ids) != 1 or not _COMPARISON_TERM_RE.search(question):
        return ComparisonResolution()

    anchor = company_ids[0]
    row = get_company(conn, anchor)
    if row is None:
        return ComparisonResolution(planner_used=True, notes=[f"{anchor} is not a registered company"])

    grounding_field = next((f for f in _SECTOR_FIELDS_BY_PREFERENCE if row[f]), None)
    if grounding_field is None:
        return ComparisonResolution(
            planner_used=True,
            notes=[f"{anchor} has no sector/industry classification on file to ground a peer comparison"],
        )

    grounding_value = row[grounding_field]
    peers = list_companies_by_sector_field(conn, grounding_field, grounding_value, anchor)
    peer_ids = [p["company_id"] for p in peers]

    if not peer_ids:
        return ComparisonResolution(
            grounding_field=grounding_field, grounding_value=grounding_value,
            neo4j_used=(GRAPH_BACKEND == "neo4j"), planner_used=True,
            notes=[f"no other companies on file share {anchor}'s {grounding_field}={grounding_value!r}"],
        )

    broader = bool(_BROADER_SCOPE_RE.search(question))
    capped = peer_ids if broader else peer_ids[:MAX_COMPARISON_DATASETS]
    notes = (
        [f"comparison limited to {MAX_COMPARISON_DATASETS} of {len(peer_ids)} {grounding_value} peers on file "
         "(broader analysis wasn't explicitly requested)"]
        if not broader and len(capped) < len(peer_ids) else []
    )
    return ComparisonResolution(
        peer_company_ids=capped, grounding_field=grounding_field, grounding_value=grounding_value,
        neo4j_used=(GRAPH_BACKEND == "neo4j"), planner_used=True, notes=notes,
    )
