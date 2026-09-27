"""India Economic Graph + Economic Data Ingestion Foundation -- Phase 1.

Phase 1 scope only: registry schema + canonical observation model + the 94
indicators registered as metadata. The causal graph (CausalAssertion/
Mechanism), Neo4j sync, and scheduler safety fields are Phase 2, a separate
later task -- nothing here builds toward those yet.

Deliberately a top-level package distinct from `indicators/` (this repo's
existing company-level rule-triggered indicator system --
`indicators/framework.py`, `RULE_REGISTRY`, `IndicatorRule`). An
`EconomicIndicator` here is an unrelated concept (a macro concept like
"Consumer Price Inflation") and must never be confused with that package's
`TriggeredIndicator`.

Repository functions (insert/query) live in `storage/repositories.py` and
`storage/repositories_pg.py`, mirroring where `macro_observations`'
functions live -- this package holds only the dataclass models and the
human-reviewable repo-file loader for `infrastructure/economic_graph/
indicators/*.yaml`.
"""
