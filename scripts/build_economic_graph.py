"""Initial build of the U.S. macro-economic causal graph in Neo4j --
Indicator -> MacroConcept -> EconomicEffect -> Sector/Industry -> Company ->
CompanyMetric, plus Source/Geography/Hypothesis node types.

Scope, deliberately separate from context/graph_neo4j.py's existing syncs
(sync_graph()'s SAME_SECTOR_AS/Concept/Investigation graph, and
sync_knowledge_graph()'s Entity/Claim/Evidence graph): this is a new,
additive set of node/relationship types in the same Neo4j database, reusing
the same driver/MERGE-idempotent-rebuild idiom (get_driver(), UNWIND $rows
batches per context/graph_neo4j.py's docstring) but sharing identity only
where a node already exists under the same id -- (:Company {id}) here is
the SAME node sync_graph() already MERGEs, just enriched with more labels/
edges; nothing here reads or writes SAME_SECTOR_AS/Concept/Investigation/
Entity/Claim.

Deliberately NOT implementing docs/economic-graph/PLAN.md's Phase 2
(EconomicFactor/KPI/CausalAssertion/Mechanism, evidence-in-Postgres-only
causal_evidence table) -- that plan is locked but unbuilt; this script uses
the node/relationship vocabulary given for this task instead (MacroConcept/
EconomicEffect/CompanyMetric, INFLUENCES/IMPACTS/etc. as plain Neo4j
relationship properties, no causal_evidence table). See this module's
`--help` output and the summary it prints for exactly what's populated.

Data sources (read-only -- this script writes ONLY to Neo4j):
  * S3 (storage/document_store.py::S3DocumentStore) -- the FRED historical
    snapshot this app already pulled (scripts/fred_historical_s3_pull.py),
    raw/fred/snapshots/<run_id>/manifest.json + series/<id>/metadata.json,
    for each of the 34 curated Indicator nodes' properties + provenance.
  * Neon/Postgres (storage.database.init_postgres_db(), i.e. the NEON
    connection string directly -- this repo's live data lives in Neon, not
    local SQLite: companies/sectors/industries/canonical_financials) for
    Company/Sector/Industry/CompanyMetric identity + BELONGS_TO_SECTOR/
    BELONGS_TO_INDUSTRY/HAS_METRIC edges. `sources.source_id='fred'` is
    reused as-is for the one Source node this build creates.
  * Qdrant -- not read. This build's MacroConcept->EconomicEffect and
    EconomicEffect->Sector/Industry edges are a small, explicitly-labeled
    curated seed (CURATED_CONCEPTS/CURATED_EFFECTS below), not derived from
    any document evidence, so there is no chunk/point id to attach as an
    evidence pointer yet (see the printed summary's "relationships lacking
    evidence" section). A later phase that grounds these edges in real
    filings/research would look up document_chunks.chunk_id (already the
    Qdrant point id verbatim -- see retrieval/vector_store_qdrant.py) and
    attach it as `evidence_chunk_id`, without changing this script's shape.

No CORRELATED_WITH edges are created here -- that requires an actual
statistical test against economic_observations/macro_observations x
financial_observations, not yet run anywhere in this codebase (rule: don't
invent a causal/correlated claim without evidence).

No Hypothesis instances are created -- the schema/label is documented here
(see HYPOTHESIS_NODE_NOTE below) but there is no existing curated hypothesis
source scoped to macro/company/metric (investigation_hypotheses is a
different, per-investigation concept -- see this module's own docstring
discussion in the PR/commit, not duplicated here).

Usage:
  python -m scripts.build_economic_graph
  python -m scripts.build_economic_graph --dry-run   # print plan, write nothing
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from context.graph_neo4j import get_driver
from storage.database import init_postgres_db
from storage.document_store import S3DocumentStore

# ---------------------------------------------------------------------
# Indicator -> MacroConcept mapping (structural: which concept each FRED
# series measures). category matches the "SIGNALS U.S. MACRO LAYER"
# grouping the series were curated under (scripts/fred_historical_s3_pull.py
# CURATED_SERIES). concept is this build's own MacroConcept id -- finer-
# grained than category (e.g. DGS10 and T10Y2Y are both "Monetary Policy"
# category but distinct concepts: long-term rates vs. yield-curve slope).
# ---------------------------------------------------------------------
INDICATOR_CONCEPTS: dict[str, tuple[str, str, str]] = {
    # series_id: (category, concept_id, concept_name)
    "GDP": ("Economic Growth", "economic_growth", "Economic Growth"),
    "GDPC1": ("Economic Growth", "economic_growth", "Economic Growth"),
    "INDPRO": ("Economic Growth", "industrial_production", "Industrial Production"),
    "FEDFUNDS": ("Monetary Policy", "policy_rate", "Policy Rate"),
    "SOFR": ("Monetary Policy", "policy_rate", "Policy Rate"),
    "DGS3MO": ("Monetary Policy", "short_term_rates", "Short-Term Treasury Rates"),
    "DGS2": ("Monetary Policy", "short_term_rates", "Short-Term Treasury Rates"),
    "DGS10": ("Monetary Policy", "long_term_rates", "Long-Term Treasury Rates"),
    "T10Y2Y": ("Monetary Policy", "yield_curve_slope", "Yield Curve Slope"),
    "CPIAUCSL": ("Inflation", "inflation", "Consumer Price Inflation"),
    "CPILFESL": ("Inflation", "inflation", "Consumer Price Inflation"),
    "PCEPI": ("Inflation", "inflation", "Consumer Price Inflation"),
    "PCEPILFE": ("Inflation", "inflation", "Consumer Price Inflation"),
    "T10YIE": ("Inflation", "inflation_expectations", "Inflation Expectations"),
    "UNRATE": ("Employment", "labor_market", "Labor Market"),
    "PAYEMS": ("Employment", "labor_market", "Labor Market"),
    "ICSA": ("Employment", "labor_market", "Labor Market"),
    "JTSJOL": ("Employment", "labor_market", "Labor Market"),
    "UMCSENT": ("Consumer", "consumer_sentiment", "Consumer Sentiment"),
    "RSAFS": ("Consumer", "consumer_spending", "Consumer Spending"),
    "DSPIC96": ("Consumer", "personal_income", "Personal Income"),
    "PSAVERT": ("Consumer", "savings_rate", "Savings Rate"),
    "HOUST": ("Housing", "housing_supply", "Housing Supply"),
    "PERMIT": ("Housing", "housing_supply", "Housing Supply"),
    "CSUSHPISA": ("Housing", "home_prices", "Home Prices"),
    "MORTGAGE30US": ("Housing", "mortgage_rates", "Mortgage Rates"),
    "M2SL": ("Liquidity / Credit", "money_supply", "Money Supply"),
    "WALCL": ("Liquidity / Credit", "fed_balance_sheet", "Fed Balance Sheet"),
    "NFCI": ("Liquidity / Credit", "financial_conditions", "Financial Conditions"),
    "BAMLH0A0HYM2": ("Liquidity / Credit", "credit_spreads", "Credit Spreads"),
    "VIXCLS": ("Markets", "market_volatility", "Market Volatility"),
    "DTWEXBGS": ("Markets", "usd_strength", "USD Strength"),
    "DCOILWTICO": ("Commodities", "oil_prices", "Oil Prices"),
    "DHHNGSP": ("Commodities", "natural_gas_prices", "Natural Gas Prices"),
}

# ---------------------------------------------------------------------
# MacroConcept -> EconomicEffect (INFLUENCES) and EconomicEffect -> Sector/
# Industry (IMPACTS) -- a MODEST, explicitly curated seed of well-
# established textbook macro relationships, analyst-authored by this build
# (not derived from any document/statistical evidence in this repo). Each
# edge is tagged provenance_type="analyst_hypothesis" and a confidence well
# short of 1.0 so nothing here reads as a verified causal fact -- see this
# module's own docstring and rule #3/#4 (don't invent causality from
# correlation; use CORRELATED_WITH when unestablished -- not used here
# either, since no correlation was actually computed).
#
# Sector names below are matched verbatim against the `sectors` table
# (Postgres) so IMPACTS edges land on the same Sector nodes BELONGS_TO_
# SECTOR uses -- see _fetch_sectors_industries().
# ---------------------------------------------------------------------
CURATED_EFFECTS: list[dict] = [
    {
        "effect_id": "housing_affordability", "effect_name": "Housing Affordability",
        "influences": [
            {"concept_id": "mortgage_rates", "direction": "negative", "confidence": 0.85},
            {"concept_id": "home_prices", "direction": "negative", "confidence": 0.7},
            {"concept_id": "personal_income", "direction": "positive", "confidence": 0.6},
        ],
        "impacts_sectors": [{"sector": "Real Estate", "direction": "positive", "confidence": 0.6}],
    },
    {
        "effect_id": "borrowing_cost", "effect_name": "Borrowing Cost",
        "influences": [
            {"concept_id": "policy_rate", "direction": "positive", "confidence": 0.85},
            {"concept_id": "long_term_rates", "direction": "positive", "confidence": 0.75},
            {"concept_id": "credit_spreads", "direction": "positive", "confidence": 0.65},
        ],
        "impacts_sectors": [
            {"sector": "Real Estate", "direction": "negative", "confidence": 0.65},
            {"sector": "Construction", "direction": "negative", "confidence": 0.55},
        ],
    },
    {
        "effect_id": "corporate_financing_cost", "effect_name": "Corporate Financing Cost",
        "influences": [
            {"concept_id": "policy_rate", "direction": "positive", "confidence": 0.8},
            {"concept_id": "credit_spreads", "direction": "positive", "confidence": 0.7},
            {"concept_id": "financial_conditions", "direction": "negative", "confidence": 0.6},
        ],
        "impacts_sectors": [
            {"sector": "Financial Services", "direction": "mixed", "confidence": 0.5},
            {"sector": "Capital Goods", "direction": "negative", "confidence": 0.5},
        ],
    },
    {
        "effect_id": "consumer_purchasing_power", "effect_name": "Consumer Purchasing Power",
        "influences": [
            {"concept_id": "inflation", "direction": "negative", "confidence": 0.75},
            {"concept_id": "personal_income", "direction": "positive", "confidence": 0.7},
            {"concept_id": "consumer_sentiment", "direction": "positive", "confidence": 0.55},
        ],
        "impacts_sectors": [
            {"sector": "Consumer Cyclical", "direction": "positive", "confidence": 0.6},
            {"sector": "Retail", "direction": "positive", "confidence": 0.6},
            {"sector": "Consumer Defensive", "direction": "positive", "confidence": 0.4},
        ],
    },
    {
        "effect_id": "energy_input_costs", "effect_name": "Energy Input Costs",
        "influences": [
            {"concept_id": "oil_prices", "direction": "positive", "confidence": 0.8},
            {"concept_id": "natural_gas_prices", "direction": "positive", "confidence": 0.7},
        ],
        "impacts_sectors": [
            {"sector": "Industrials", "direction": "negative", "confidence": 0.55},
            {"sector": "Automobile and Auto Components", "direction": "negative", "confidence": 0.5},
            {"sector": "Energy", "direction": "positive", "confidence": 0.7},
        ],
    },
    {
        "effect_id": "recession_risk", "effect_name": "Recession Risk",
        "influences": [
            {"concept_id": "yield_curve_slope", "direction": "negative", "confidence": 0.65},
            {"concept_id": "labor_market", "direction": "negative", "confidence": 0.6},
            {"concept_id": "financial_conditions", "direction": "negative", "confidence": 0.55},
        ],
        "impacts_sectors": [
            {"sector": "Financial Services", "direction": "negative", "confidence": 0.55},
            {"sector": "Consumer Cyclical", "direction": "negative", "confidence": 0.5},
        ],
    },
]

# A couple of EconomicEffect -> CompanyMetric (IMPACTS_METRIC) edges,
# same curated-seed status as CURATED_EFFECTS above. metric_key values are
# verified against canonical_financials' real 42-key vocabulary (see
# _fetch_metric_keys()) -- a metric_key with no matching row is skipped
# and reported as a mapping failure, not silently created.
CURATED_METRIC_IMPACTS: list[dict] = [
    {"effect_id": "borrowing_cost", "metric_key": "interest_expended", "direction": "positive", "confidence": 0.6},
    {"effect_id": "corporate_financing_cost", "metric_key": "interest_expended", "direction": "positive", "confidence": 0.55},
    {"effect_id": "consumer_purchasing_power", "metric_key": "total_revenue", "direction": "positive", "confidence": 0.45},
]

_SOURCE_ID = "fred"
_GEOGRAPHY_ID = "US"
_GEOGRAPHY_NAME = "United States"
_ANALYST_SEED_REFERENCE = "economic-graph-seed-v1 (scripts/build_economic_graph.py CURATED_EFFECTS)"

# Not populated in this build -- documents the intended node shape only,
# so a future script adding real hypotheses knows the label/id convention
# to MERGE onto rather than inventing a new one.
HYPOTHESIS_NODE_NOTE = (
    "Hypothesis {id, statement, confidence} -[:ABOUT]-> Indicator|Company|CompanyMetric "
    "-- no instances created; no curated macro/company hypothesis source exists yet."
)


def _fetch_fred_indicator_metadata(store: S3DocumentStore) -> dict[str, dict]:
    """Reads both existing FRED snapshot manifests (scripts/fred_historical_
    s3_pull.py's two runs) and returns {series_id: {...}} merged, preferring
    the newer snapshot's data on a duplicate series_id. Each entry carries
    the S3 metadata/observations keys as provenance pointers -- FRED's own
    title/frequency/units are read out of metadata.json, never re-derived."""
    run_ids = ["20260926T163031Z", "20260926T163310Z"]  # oldest first, newest wins on overlap
    merged: dict[str, dict] = {}
    for run_id in run_ids:
        manifest_key = f"raw/fred/snapshots/{run_id}/manifest.json"
        if not store.exists(manifest_key):
            continue
        manifest = json.loads(store.retrieve(manifest_key))
        for entry in manifest["series"]:
            if entry["status"] != "ok":
                continue
            series_id = entry["series_id"]
            metadata_key = entry["s3_locations"]["metadata"]
            fred_meta = json.loads(store.retrieve(metadata_key))["seriess"][0]
            merged[series_id] = {
                "run_id": run_id,
                "title": fred_meta.get("title"),
                "frequency": fred_meta.get("frequency"),
                "units": fred_meta.get("units"),
                "seasonal_adjustment": fred_meta.get("seasonal_adjustment"),
                "observation_start": entry["first_observation_date"],
                "observation_end": entry["last_observation_date"],
                "observation_count": entry["observation_count"],
                "s3_metadata_key": metadata_key,
                "s3_observations_key": entry["s3_locations"]["observations"],
            }
    return merged


def _fetch_sectors_industries(pg_cur) -> tuple[list[str], list[str]]:
    pg_cur.execute("SELECT name FROM sectors ORDER BY name")
    sectors = [r["name"] for r in pg_cur.fetchall()]
    pg_cur.execute("SELECT name FROM industries ORDER BY name")
    industries = [r["name"] for r in pg_cur.fetchall()]
    return sectors, industries


def _fetch_companies(pg_cur) -> list[dict]:
    pg_cur.execute(
        "SELECT company_id, display_name, sector, industry FROM companies "
        "WHERE sector IS NOT NULL OR industry IS NOT NULL"
    )
    return list(pg_cur.fetchall())


def _fetch_metric_keys(pg_cur) -> set[str]:
    pg_cur.execute("SELECT DISTINCT metric_key FROM canonical_financials")
    return {r["metric_key"] for r in pg_cur.fetchall()}


def _fetch_company_metric_pairs(pg_cur) -> list[dict]:
    pg_cur.execute("SELECT DISTINCT company_id, metric_key FROM canonical_financials")
    return list(pg_cur.fetchall())


# ---------------------------------------------------------------------
# Neo4j writers -- one function per node/relationship batch, each a single
# UNWIND $rows MERGE, matching context/graph_neo4j.py's existing idiom.
# ---------------------------------------------------------------------

def _sync_source_and_geography(tx) -> None:
    tx.run(
        "MERGE (s:Source {id: $id}) SET s.name = $name",
        id=_SOURCE_ID, name="Federal Reserve Economic Data (FRED)",
    )
    tx.run(
        "MERGE (g:Geography {id: $id}) SET g.name = $name",
        id=_GEOGRAPHY_ID, name=_GEOGRAPHY_NAME,
    )


def _sync_indicators(tx, indicator_rows: list[dict]) -> None:
    tx.run(
        "UNWIND $rows AS row "
        "MERGE (i:Indicator {id: row.series_id}) "
        "SET i.title = row.title, i.category = row.category, i.frequency = row.frequency, "
        "    i.units = row.units, i.seasonal_adjustment = row.seasonal_adjustment, "
        "    i.observation_start = row.observation_start, i.observation_end = row.observation_end, "
        "    i.observation_count = row.observation_count, "
        "    i.s3_metadata_key = row.s3_metadata_key, i.s3_observations_key = row.s3_observations_key "
        "MERGE (c:MacroConcept {id: row.concept_id}) SET c.name = row.concept_name "
        "MERGE (i)-[:MEASURES]->(c) "
        "MERGE (src:Source {id: $source_id}) "
        "MERGE (i)-[:SOURCED_FROM]->(src) "
        "MERGE (geo:Geography {id: $geography_id}) "
        "MERGE (i)-[:APPLIES_TO]->(geo)",
        rows=indicator_rows, source_id=_SOURCE_ID, geography_id=_GEOGRAPHY_ID,
    )


def _sync_sectors_industries(tx, sectors: list[str], industries: list[str]) -> None:
    tx.run("UNWIND $names AS n MERGE (s:Sector {id: n}) SET s.name = n", names=sectors)
    tx.run("UNWIND $names AS n MERGE (i:Industry {id: n}) SET i.name = n", names=industries)


def _sync_companies(tx, companies: list[dict]) -> None:
    tx.run(
        "UNWIND $rows AS row "
        "MERGE (c:Company {id: row.company_id}) "
        "SET c.display_name = row.display_name, c.sector = row.sector, c.industry = row.industry",
        rows=[dict(r) for r in companies],
    )
    with_sector = [{"company_id": r["company_id"], "sector": r["sector"]} for r in companies if r["sector"]]
    with_industry = [{"company_id": r["company_id"], "industry": r["industry"]} for r in companies if r["industry"]]
    if with_sector:
        tx.run(
            "UNWIND $rows AS row "
            "MATCH (c:Company {id: row.company_id}), (s:Sector {id: row.sector}) "
            "MERGE (c)-[:BELONGS_TO_SECTOR]->(s)",
            rows=with_sector,
        )
    if with_industry:
        tx.run(
            "UNWIND $rows AS row "
            "MATCH (c:Company {id: row.company_id}), (i:Industry {id: row.industry}) "
            "MERGE (c)-[:BELONGS_TO_INDUSTRY]->(i)",
            rows=with_industry,
        )


def _sync_company_metrics(tx, metric_keys: set[str], pairs: list[dict]) -> None:
    tx.run(
        "UNWIND $keys AS key MERGE (m:CompanyMetric {id: key}) SET m.metric_key = key",
        keys=sorted(metric_keys),
    )
    batch: list[dict] = [dict(r) for r in pairs]
    step = 2000
    for i in range(0, len(batch), step):
        tx.run(
            "UNWIND $rows AS row "
            "MATCH (c:Company {id: row.company_id}), (m:CompanyMetric {id: row.metric_key}) "
            "MERGE (c)-[:HAS_METRIC]->(m)",
            rows=batch[i : i + step],
        )


def _sync_curated_causal_edges(tx, sector_names: set[str], industry_names: set[str], metric_keys: set[str]) -> dict:
    """Returns a report of unresolved sectors/industries/metrics (curated
    edge referenced a name absent from Postgres) so the summary can flag
    mapping failures instead of silently creating an orphan Sector node."""
    unresolved: dict[str, list[str]] = {"sector": [], "industry": [], "metric": []}

    effect_rows = [{"id": e["effect_id"], "name": e["effect_name"]} for e in CURATED_EFFECTS]
    tx.run("UNWIND $rows AS row MERGE (e:EconomicEffect {id: row.id}) SET e.name = row.name", rows=effect_rows)

    influences_rows = [
        {
            "concept_id": inf["concept_id"], "effect_id": effect["effect_id"],
            "direction": inf["direction"], "confidence": inf["confidence"],
            "evidence_type": "analyst_hypothesis", "provenance_type": "analyst_hypothesis",
            "source_reference": _ANALYST_SEED_REFERENCE,
        }
        for effect in CURATED_EFFECTS for inf in effect["influences"]
    ]
    tx.run(
        "UNWIND $rows AS row "
        "MATCH (c:MacroConcept {id: row.concept_id}), (e:EconomicEffect {id: row.effect_id}) "
        "MERGE (c)-[r:INFLUENCES]->(e) "
        "SET r.direction = row.direction, r.confidence = row.confidence, "
        "    r.evidence_type = row.evidence_type, r.provenance_type = row.provenance_type, "
        "    r.source_reference = row.source_reference, r.created_at = datetime()",
        rows=influences_rows,
    )

    impacts_rows = []
    for effect in CURATED_EFFECTS:
        for imp in effect["impacts_sectors"]:
            if imp["sector"] not in sector_names:
                unresolved["sector"].append(f"{effect['effect_id']} -> {imp['sector']}")
                continue
            impacts_rows.append({
                "effect_id": effect["effect_id"], "sector": imp["sector"],
                "direction": imp["direction"], "confidence": imp["confidence"],
                "provenance_type": "analyst_hypothesis", "source_reference": _ANALYST_SEED_REFERENCE,
            })
    if impacts_rows:
        tx.run(
            "UNWIND $rows AS row "
            "MATCH (e:EconomicEffect {id: row.effect_id}), (s:Sector {id: row.sector}) "
            "MERGE (e)-[r:IMPACTS]->(s) "
            "SET r.direction = row.direction, r.confidence = row.confidence, "
            "    r.provenance_type = row.provenance_type, r.source_reference = row.source_reference, "
            "    r.created_at = datetime()",
            rows=impacts_rows,
        )

    metric_impact_rows = []
    for row in CURATED_METRIC_IMPACTS:
        if row["metric_key"] not in metric_keys:
            unresolved["metric"].append(f"{row['effect_id']} -> {row['metric_key']}")
            continue
        metric_impact_rows.append({**row, "provenance_type": "analyst_hypothesis", "source_reference": _ANALYST_SEED_REFERENCE})
    if metric_impact_rows:
        tx.run(
            "UNWIND $rows AS row "
            "MATCH (e:EconomicEffect {id: row.effect_id}), (m:CompanyMetric {id: row.metric_key}) "
            "MERGE (e)-[r:IMPACTS_METRIC]->(m) "
            "SET r.direction = row.direction, r.confidence = row.confidence, "
            "    r.provenance_type = row.provenance_type, r.source_reference = row.source_reference, "
            "    r.created_at = datetime()",
            rows=metric_impact_rows,
        )

    return unresolved


def _assemble_payload() -> dict:
    """The pure S3+Neon read/join step, factored out of build() so it can
    run -- and be cached to S3 -- independently of whether Neo4j is
    reachable. Returns everything build()'s Neo4j-writing half needs, in
    plain JSON-serializable form."""
    store = S3DocumentStore()
    indicator_metadata = _fetch_fred_indicator_metadata(store)

    pg_conn = init_postgres_db()
    pg_cur = pg_conn.cursor()
    sectors, industries = _fetch_sectors_industries(pg_cur)
    companies = [dict(r) for r in _fetch_companies(pg_cur)]
    metric_keys = _fetch_metric_keys(pg_cur)
    company_metric_pairs = [dict(r) for r in _fetch_company_metric_pairs(pg_cur)]
    pg_conn.close()

    unresolved_indicators = [sid for sid in INDICATOR_CONCEPTS if sid not in indicator_metadata]
    indicator_rows = []
    for series_id, (category, concept_id, concept_name) in INDICATOR_CONCEPTS.items():
        if series_id not in indicator_metadata:
            continue
        meta = indicator_metadata[series_id]
        indicator_rows.append({
            "series_id": series_id, "category": category, "concept_id": concept_id, "concept_name": concept_name,
            **{k: meta[k] for k in (
                "title", "frequency", "units", "seasonal_adjustment", "observation_start",
                "observation_end", "observation_count", "s3_metadata_key", "s3_observations_key",
            )},
        })

    return {
        "indicator_rows": indicator_rows,
        "unresolved_indicators": unresolved_indicators,
        "sectors": sectors,
        "industries": industries,
        "companies": companies,
        "metric_keys": sorted(metric_keys),
        "company_metric_pairs": company_metric_pairs,
    }


def export_payload_to_s3(store: S3DocumentStore | None = None) -> str:
    """Caches _assemble_payload()'s S3+Neon join as one JSON object in S3,
    under the same raw/fred/ tree the FRED snapshots themselves live in --
    so the Neo4j write, once the graph DB is reachable again, is a pure
    `json.loads` + Cypher load with zero re-reads of Postgres/S3. Returns
    the S3 key written."""
    from datetime import datetime, timezone

    store = store or S3DocumentStore()
    payload = _assemble_payload()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"raw/fred/economic_graph_build_plan/{run_id}/payload.json"
    store.store(key, json.dumps(payload, indent=2, default=str).encode("utf-8"))
    return key


def build(dry_run: bool = False, payload: dict | None = None) -> dict:
    if payload is None:
        payload = _assemble_payload()
    indicator_rows = payload["indicator_rows"]
    unresolved_indicators = payload["unresolved_indicators"]
    sectors = payload["sectors"]
    industries = payload["industries"]
    companies = payload["companies"]
    metric_keys = set(payload["metric_keys"])
    company_metric_pairs = payload["company_metric_pairs"]

    sector_set, industry_set = set(sectors), set(industries)

    counts = Counter()
    unresolved: dict[str, list[str]] = {}

    if not dry_run:
        driver = get_driver()
        with driver.session() as session:
            session.execute_write(_sync_source_and_geography)
            session.execute_write(_sync_indicators, indicator_rows)
            session.execute_write(_sync_sectors_industries, sectors, industries)
            session.execute_write(_sync_companies, companies)
            session.execute_write(_sync_company_metrics, metric_keys, company_metric_pairs)
            unresolved = session.execute_write(_sync_curated_causal_edges, sector_set, industry_set, metric_keys)

    counts["Indicator"] = len(indicator_rows)
    counts["MacroConcept"] = len({row["concept_id"] for row in indicator_rows})
    counts["EconomicEffect"] = len(CURATED_EFFECTS)
    counts["Sector"] = len(sectors)
    counts["Industry"] = len(industries)
    counts["Company"] = len(companies)
    counts["CompanyMetric"] = len(metric_keys)
    counts["Source"] = 1
    counts["Geography"] = 1
    counts["Hypothesis"] = 0

    rel_counts = Counter()
    rel_counts["MEASURES"] = len(indicator_rows)
    rel_counts["SOURCED_FROM"] = len(indicator_rows)
    rel_counts["APPLIES_TO"] = len(indicator_rows)
    rel_counts["BELONGS_TO_SECTOR"] = sum(1 for r in companies if r["sector"])
    rel_counts["BELONGS_TO_INDUSTRY"] = sum(1 for r in companies if r["industry"])
    rel_counts["HAS_METRIC"] = len(company_metric_pairs)
    rel_counts["INFLUENCES"] = sum(len(e["influences"]) for e in CURATED_EFFECTS)
    rel_counts["IMPACTS"] = sum(len(e["impacts_sectors"]) for e in CURATED_EFFECTS) - len(unresolved.get("sector", []))
    rel_counts["IMPACTS_METRIC"] = len(CURATED_METRIC_IMPACTS) - len(unresolved.get("metric", []))
    rel_counts["CORRELATED_WITH"] = 0
    rel_counts["SUPPORTED_BY"] = 0
    rel_counts["CONTRADICTED_BY"] = 0
    rel_counts["LEADS"] = 0
    rel_counts["LAGS"] = 0

    return {
        "node_counts": dict(counts),
        "relationship_counts": dict(rel_counts),
        "unresolved_indicators": unresolved_indicators,
        "unresolved_sector_impacts": unresolved.get("sector", []),
        "unresolved_metric_impacts": unresolved.get("metric", []),
        "companies_without_sector_or_industry_edge": sum(
            1 for r in companies if not r["sector"] and not r["industry"]
        ),
    }


def _print_summary(result: dict, dry_run: bool) -> None:
    print("\n--- Economic Graph Build Summary ---", flush=True)
    print("(dry run -- nothing written to Neo4j)" if dry_run else "(written to Neo4j)", flush=True)

    print("\nNodes created/merged by type:", flush=True)
    for label, count in result["node_counts"].items():
        print(f"  {label}: {count}", flush=True)

    print("\nRelationships created/merged by type:", flush=True)
    for rel, count in result["relationship_counts"].items():
        print(f"  {rel}: {count}", flush=True)

    print("\nUnresolved entities:", flush=True)
    if result["unresolved_indicators"]:
        print(f"  Indicators with no S3 snapshot metadata (skipped): {result['unresolved_indicators']}", flush=True)
    else:
        print("  Indicators: none (all 34 curated series resolved to S3 metadata)", flush=True)

    print("\nRelationships lacking evidence/provenance:", flush=True)
    print(
        "  All INFLUENCES/IMPACTS/IMPACTS_METRIC edges are analyst-curated seed content "
        f"(provenance_type=analyst_hypothesis, source_reference={_ANALYST_SEED_REFERENCE!r}), "
        "not grounded in a specific document/chunk or a statistical test -- no CORRELATED_WITH, "
        "SUPPORTED_BY, LEADS, or LAGS edges were created (would require real evidence/computation).",
        flush=True,
    )
    print(f"  Hypothesis node type: {HYPOTHESIS_NODE_NOTE}", flush=True)

    print("\nDuplicates prevented:", flush=True)
    print(
        "  Every write above is a Cypher MERGE keyed on a stable id (series_id, sector/industry name, "
        "company_id, metric_key, effect_id, concept_id) -- re-running this script is a no-op for anything "
        "unchanged, and never creates a second node/edge for the same key.",
        flush=True,
    )

    print("\nMapping failures:", flush=True)
    if result["unresolved_sector_impacts"]:
        print(f"  IMPACTS edges skipped (sector not in Postgres `sectors` table): {result['unresolved_sector_impacts']}", flush=True)
    if result["unresolved_metric_impacts"]:
        print(f"  IMPACTS_METRIC edges skipped (metric_key not in canonical_financials): {result['unresolved_metric_impacts']}", flush=True)
    if not result["unresolved_sector_impacts"] and not result["unresolved_metric_impacts"]:
        print("  none", flush=True)
    print(
        f"  Companies with neither a sector nor industry edge: {result['companies_without_sector_or_industry_edge']} "
        "(companies table has a NULL sector/industry for these)",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="compute and print the summary; write nothing to Neo4j")
    parser.add_argument(
        "--export-payload", action="store_true",
        help="join S3 (FRED snapshots) + Neon (companies/sectors/metrics) into one cached JSON "
        "payload in S3 (raw/fred/economic_graph_build_plan/<run_id>/payload.json) and exit -- "
        "does not touch Neo4j. Use this when Neo4j isn't reachable yet but you want the S3/Neon "
        "read+join work done in advance.",
    )
    parser.add_argument(
        "--payload-key",
        help="S3 key of a payload previously written by --export-payload -- skips re-reading S3/Neon "
        "and writes straight to Neo4j from the cached payload instead.",
    )
    args = parser.parse_args()

    if args.export_payload:
        key = export_payload_to_s3()
        print(f"payload written: s3://{S3DocumentStore()._bucket}/{key}", flush=True)
        return

    payload = None
    if args.payload_key:
        store = S3DocumentStore()
        payload = json.loads(store.retrieve(args.payload_key))
        print(f"loaded cached payload from {args.payload_key}", flush=True)

    result = build(dry_run=args.dry_run, payload=payload)
    _print_summary(result, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
