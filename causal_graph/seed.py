"""Deterministic seed for the causal graph: a small, hand-written set of
relationships that proves the architecture. No LLM involved, and no claim here
is presented as validated -- every edge starts as a CANDIDATE with MANUAL_SEED
provenance; evidence, validation and promotion happen through the service.

Coverage (one of each kind the foundation must hold):
  Macro -> EconomicDriver       RBI repo rate -> lending rate
  Macro -> Sector               RBI repo rate -> banking
  Commodity -> EconomicDriver   crude oil -> fuel cost; iron ore -> steel price
  Sector -> BusinessDriver      banking depends on deposits; auto -> material cost
  Company -> Sector             HDFC Bank / IDFC First -> banking
  Company -> BusinessDriver     companies exposed to deposits, loan growth, material cost
  BusinessDriver -> Metric      loan growth -> revenue; material cost -> margin
  Cross-sector                  iron ore -> steel -> auto
  US macro chain                fed funds / 10Y -> lending rate (scope: US)
The graph is reusable structure: the India and US chains share the Lending
Rate -> Credit Demand -> Loan Growth -> Revenue nodes and differ only in scope.
"""

from __future__ import annotations

from causal_graph.service import CausalKnowledgeService
from causal_graph.validation import DuplicateRelationshipError

SEED_ID = "cg-seed-v1"

#: (family, display_name, references)
NODES: list[tuple[str, str, dict]] = [
    ("MacroIndicator", "RBI Policy Repo Rate", {"series_key": "policy_repo_rate"}),
    ("MacroIndicator", "US Federal Funds Rate", {"series_key": "fedfunds"}),
    ("MacroIndicator", "US 10-Year Treasury Yield", {"series_key": "dgs10"}),
    ("Commodity", "Crude Oil", {"series_key": "dcoilwtico"}),
    ("Commodity", "Iron Ore", {}),
    ("EconomicDriver", "Lending Rate", {}),
    ("EconomicDriver", "Credit Demand", {}),
    ("EconomicDriver", "Fuel Cost", {}),
    ("EconomicDriver", "Transportation Cost", {}),
    ("EconomicDriver", "Input Cost", {}),
    ("EconomicDriver", "Steel Price", {}),
    ("EconomicDriver", "Auto Financing Cost", {}),
    ("EconomicDriver", "Vehicle Demand", {}),
    ("EconomicDriver", "Competitive Intensity", {}),
    ("Sector", "Banking", {}),
    ("Sector", "Auto", {}),
    ("Sector", "Steel", {}),
    ("BusinessDriver", "Loan Growth", {}),
    ("BusinessDriver", "Deposits", {}),
    ("BusinessDriver", "Material Cost", {}),
    ("BusinessDriver", "Vehicle Volume", {}),
    ("BusinessDriver", "Pricing Pressure", {}),
    ("FinancialMetric", "Revenue", {"metric_key": "total_revenue"}),
    ("FinancialMetric", "Operating Margin", {}),
]
COMPANIES = {"HDFCBANK": "Banking", "IDFCFIRSTB": "Banking", "MARUTI": "Auto", "TATASTEEL": "Steel"}
COMPANY_EXPOSURES = {
    "HDFCBANK": ("Deposits", "Funding mix and deposit growth set a bank's cost of funds and loan capacity"),
    "IDFCFIRSTB": ("Loan Growth", "Retail loan growth is the main driver of the bank's interest income"),
    "MARUTI": ("Material Cost", "Steel, precious metals and electronics are a large share of cost of sales"),
}

_G = {"geography": "IN"}
_US = {"geography": "US"}
#: (source, type, target, direction, mechanism, confidence, strength, lag(min,max,unit)|None, scope)
EDGES: list[tuple] = [
    ("RBI Policy Repo Rate", "AFFECTS", "Lending Rate", "POSITIVE",
     "Banks reprice loans off the policy rate through MCLR/EBLR benchmarks", 0.9, "HIGH", (0, 3, "months"), _G),
    ("US Federal Funds Rate", "AFFECTS", "Lending Rate", "POSITIVE",
     "Policy rate sets short-term funding cost that banks pass into loan pricing", 0.85, "HIGH", (0, 3, "months"), _US),
    ("US 10-Year Treasury Yield", "AFFECTS", "Lending Rate", "POSITIVE",
     "Long-term yields anchor fixed-rate and mortgage pricing", 0.8, "MEDIUM", (0, 2, "quarters"), _US),
    ("Lending Rate", "DECREASES", "Credit Demand", "NEGATIVE",
     "Higher borrowing cost lowers the quantity of credit households and firms take", 0.88, "MEDIUM", (1, 2, "quarters"), {}),
    ("Credit Demand", "DRIVES", "Loan Growth", "POSITIVE",
     "Loan growth is credit demand that lenders choose to underwrite", 0.9, "HIGH", (0, 1, "quarters"), {}),
    ("Loan Growth", "DRIVES", "Revenue", "POSITIVE",
     "A larger loan book earns more interest and fee income", 0.9, "HIGH", (0, 2, "quarters"), {"sector": "Banking"}),
    ("RBI Policy Repo Rate", "AFFECTS", "Banking", "UNKNOWN",
     "Rate moves change margins and credit growth; the net sign depends on funding mix", 0.75, "MEDIUM", (1, 4, "quarters"), _G),
    ("Banking", "DEPENDS_ON", "Deposits", "POSITIVE",
     "Deposits are the primary funding source for lending", 0.9, "HIGH", None, {}),
    ("Crude Oil", "INCREASES", "Fuel Cost", "POSITIVE",
     "Fuel is refined crude; pump prices follow crude with pass-through", 0.9, "HIGH", (0, 2, "months"), {}),
    ("Fuel Cost", "INCREASES", "Transportation Cost", "POSITIVE",
     "Fuel is a large share of freight and logistics cost", 0.85, "MEDIUM", (0, 1, "quarters"), {}),
    ("Transportation Cost", "INCREASES", "Input Cost", "POSITIVE",
     "Inbound logistics adds to the landed cost of materials", 0.7, "LOW", (0, 1, "quarters"), {}),
    ("Input Cost", "DECREASES", "Operating Margin", "NEGATIVE",
     "Cost increases not passed through to prices compress margin", 0.8, "MEDIUM", (0, 2, "quarters"), {}),
    ("Iron Ore", "INCREASES", "Steel Price", "POSITIVE",
     "Ore is the main raw material of steel production", 0.8, "MEDIUM", (1, 2, "quarters"), {}),
    ("Iron Ore", "SUPPLIES", "Steel", "POSITIVE",
     "Steelmakers consume iron ore as their primary input", 0.9, "HIGH", None, {}),
    ("Steel Price", "AFFECTS", "Auto", "POSITIVE",
     "Steel is the largest raw material by weight in vehicles", 0.8, "MEDIUM", (1, 2, "quarters"), _G),
    ("Steel", "SUPPLIES", "Auto", "POSITIVE",
     "Automakers buy sheet and long steel from domestic mills", 0.9, "HIGH", None, _G),
    ("Auto", "AFFECTS", "Material Cost", "POSITIVE",
     "Vehicle production volume and mix set the raw material bill", 0.7, "MEDIUM", (0, 1, "quarters"), _G),
    ("Lending Rate", "AFFECTS", "Auto Financing Cost", "POSITIVE",
     "Vehicle loans are priced off bank lending rates", 0.85, "HIGH", (0, 2, "quarters"), _G),
    ("Banking", "AFFECTS", "Auto Financing Cost", "POSITIVE",
     "Banks and NBFCs set the price and availability of vehicle finance", 0.75, "MEDIUM", (0, 2, "quarters"), _G),
    ("Auto Financing Cost", "DECREASES", "Vehicle Demand", "NEGATIVE",
     "Most vehicles are financed; a higher instalment lowers affordability", 0.8, "MEDIUM", (1, 2, "quarters"), {"sector": "Auto"}),
    ("Vehicle Demand", "DRIVES", "Vehicle Volume", "POSITIVE",
     "Volumes follow demand once inventory is cleared", 0.9, "HIGH", (0, 1, "quarters"), {}),
    ("Vehicle Volume", "DRIVES", "Operating Margin", "POSITIVE",
     "Higher volume spreads fixed costs (operating leverage)", 0.75, "MEDIUM", (0, 1, "quarters"), {"sector": "Auto"}),
    ("Competitive Intensity", "INCREASES", "Pricing Pressure", "POSITIVE",
     "More competition forces discounts and incentives", 0.7, "MEDIUM", (0, 2, "quarters"), {}),
    ("Pricing Pressure", "DECREASES", "Operating Margin", "NEGATIVE",
     "Lower realised prices cut margin unless costs fall equally", 0.75, "MEDIUM", (0, 2, "quarters"), {}),
    ("Material Cost", "DECREASES", "Operating Margin", "NEGATIVE",
     "Raw material is the largest cost line for manufacturers; unrecovered increases cut margin", 0.85, "HIGH", (0, 2, "quarters"), {}),
]


def _node_id(service: CausalKnowledgeService, family: str, name: str) -> str:
    found = [n for n in service.find_node(family=family, name=name, limit=10) if n["display_name"] == name]
    return found[0]["id"]


def seed_graph(service: CausalKnowledgeService, *, source: str = SEED_ID) -> dict:
    created_nodes = created_edges = skipped_edges = 0
    ids: dict[str, str] = {}
    for family, name, refs in NODES:
        node, created = service.ensure_node(family, name, references=refs, actor_kind="seed", source=source)
        ids[name], created_nodes = node["id"], created_nodes + int(created)
    for company_id in COMPANIES:
        node, created = service.ensure_node("Company", company_id, canonical_name=company_id, actor_kind="seed", source=source)
        ids[company_id], created_nodes = node["id"], created_nodes + int(created)

    provenance = {"type": "MANUAL_SEED", "ref": source, "note": "hand-written seed relationship"}

    def add(src, rel, tgt, **kw):
        nonlocal created_edges, skipped_edges
        try:
            service.create_relationship(ids[src], rel, ids[tgt], provenance=provenance, actor_kind="seed",
                                        source=source, reason="initial seed", **kw)
            created_edges += 1
        except DuplicateRelationshipError:
            skipped_edges += 1

    for src, rel, tgt, direction, mech, conf, strength, lag, scope in EDGES:
        add(src, rel, tgt, direction=direction, mechanism=mech, confidence=conf, effect_strength=strength,
            lag=None if lag is None else {"min": lag[0], "max": lag[1], "unit": lag[2]}, scope=scope)
    for company_id, sector in COMPANIES.items():
        add(company_id, "BELONGS_TO", sector)
    for company_id, (driver, mech) in COMPANY_EXPOSURES.items():
        add(company_id, "EXPOSED_TO", driver, direction="UNKNOWN", mechanism=mech, confidence=0.7,
            effect_strength="MEDIUM", lag=None, scope={"company_id": company_id})
    return {"nodes_created": created_nodes, "edges_created": created_edges, "edges_already_present": skipped_edges,
            "nodes_total": len(NODES) + len(COMPANIES), "edges_total": len(EDGES) + len(COMPANIES) + len(COMPANY_EXPOSURES)}
