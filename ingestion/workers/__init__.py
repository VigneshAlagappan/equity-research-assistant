"""Built-in workers, registered on import (README: Signals Dataset-Centric
Ingestion). Import this package once from anywhere ingestion can run
(ingestion/pipeline.py, ingestion/coordinator.py already do) to make sure
every built-in worker is subscribed before the first event is published --
same registration-by-import pattern ingestion/detector.py's ADAPTER_CLASSES
dict establishes for source adapters.

Adding a new worker is adding a new module here (or anywhere else that
gets imported before publish()/replay() runs) that calls
ingestion.event_bus.register_worker(...) at import time -- never a change
to ingestion/pipeline.py, ingestion/coordinator.py, or ingestion/event_bus.py
itself.
"""

from __future__ import annotations

from ingestion.workers import (  # noqa: F401 -- imported for the registration side effect
    chunk_indexer_worker,
    financial_derivation,
    knowledge_builder_worker,
)

# embedding_indexer_worker MUST be imported (and therefore registered) AFTER
# chunk_indexer_worker above -- ingestion/event_bus.py dispatches registered
# workers in registration order, and the Embedding Indexer Worker reads the
# chunks the Chunk Indexer Worker just (re)wrote within the same publish()
# call (ingestion/workers/embedding_indexer_worker.py's module docstring).
from ingestion.workers import embedding_indexer_worker  # noqa: F401,E402
