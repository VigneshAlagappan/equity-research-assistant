"""DatasetIngestedEvent -- the one event shape every dataset type's
ingestion publishes on success (README: Signals Dataset-Centric Ingestion).

One schema for every dataset (NSE/BSE/XBRL company financials, RBI/Fed
macro, rainfall, shareholding, documents, ...); dataset-specific detail
lives inside `dataset_type`/`scope`/`storage_reference`/`metadata`, never as
a new field or subclass -- adding a dataset type is choosing values for
these fields, not changing this contract. Carries metadata describing what
was ingested and where it landed, never the dataset itself: a worker
re-reads the normalized/validated data from its own table using
`storage_reference`, so replaying an event (ingestion/event_bus.py) never
needs to re-fetch or re-ingest source data.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DatasetIngestedEvent:
    dataset_id: str
    dataset_type: str
    source: str
    storage_reference: dict
    ingestion_id: str
    event_type: str = "DATASET_INGESTED"
    scope: dict = field(default_factory=dict)
    period: str | None = None
    metadata: dict = field(default_factory=dict)
    event_id: str = ""     # filled by event_bus.publish() if blank (uuid4)
    ingested_at: str = ""  # filled by event_bus.publish() if blank
