"""Seed the persistent causal graph (causal_graph/seed.py).

Default is a DRY RUN against an in-memory graph: it shows what would be
written and exercises every validation rule, touching nothing. Writing to the
real Neo4j (settings NEO4J_URI) and the history tables in the configured
database needs --apply, and --ensure-schema to create the constraints and
indexes first. The seed is idempotent: re-running adds nothing.

  python -m scripts.seed_causal_graph                      # dry run
  python -m scripts.seed_causal_graph --apply --ensure-schema
"""

from __future__ import annotations

import argparse

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from causal_graph.history import InMemoryHistory, SqlHistory  # noqa: E402
from causal_graph.neo4j_store import Neo4jGraphStore  # noqa: E402
from causal_graph.seed import SEED_ID, seed_graph  # noqa: E402
from causal_graph.service import CausalKnowledgeService  # noqa: E402
from causal_graph.store import InMemoryGraphStore  # noqa: E402
from storage.backend_bootstrap import open_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write to Neo4j and the history tables (default: dry run)")
    ap.add_argument("--ensure-schema", action="store_true", help="create Neo4j constraints/indexes (needs --apply)")
    args = ap.parse_args()
    if args.ensure_schema and not args.apply:
        ap.error("--ensure-schema needs --apply")

    if not args.apply:
        service = CausalKnowledgeService(InMemoryGraphStore(), InMemoryHistory())
        print("DRY RUN (in-memory graph, nothing written):", seed_graph(service))
        return

    from companies.registry import get_company

    conn = open_db()
    store = Neo4jGraphStore()
    if args.ensure_schema:
        print(f"ensured {len(store.ensure_schema())} constraints/indexes")
    service = CausalKnowledgeService(
        store, SqlHistory(conn),
        company_lookup=lambda cid: (lambda row: row["display_name"] if row else None)(get_company(conn, cid)),
    )
    print(f"{SEED_ID}:", seed_graph(service))
    conn.close()


if __name__ == "__main__":
    main()
