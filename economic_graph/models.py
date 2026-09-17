"""Dataclass models for the economic graph, Phase 1.

Mirrors the shape of `sources/base.py`'s `NormalizedObservation` and
`sources/macro.py`'s `MacroNormalizedObservation`: a plain, frozen
dataclass per row shape, with auto-assigned id/created_at/updated_at
columns omitted (the repository layer stamps those on insert). These are
plain in-memory value objects -- the schema in schemas/sqlite_schema.sql /
schemas/postgres_schema.sql is the actual source of truth; nothing here
does ORM-style mapping.

Indicator vs Series is the one modeling decision worth restating here:
`EconomicIndicator` is a CONCEPT (e.g. "Consumer Price Inflation") and
owns no `series_key`; `EconomicSeries` is a measurable STREAM (e.g. "CPI
Combined YoY -- India") and owns `series_key`. One indicator maps to many
series (by geography/frequency/source) -- see `EconomicSeries.indicator_id`.
"""

from __future__ import annotations

from dataclasses import dataclass

#: economic_indicator_registry.status -- see that column's comment in
#: schemas/sqlite_schema.sql for what each value promises.
INDICATOR_STATUSES = ("registered_only", "ingesting", "live")

#: economic_observations.revision_status
REVISION_STATUSES = ("provisional", "revised", "final")


@dataclass(frozen=True)
class SourceOrganization:
    """A real-world data-publishing body, e.g. "Reserve Bank of India"."""

    name: str
    authority_level: str | None = None  # official_primary | official_secondary | aggregator
    description: str | None = None
    source_org_id: int | None = None  # assigned on insert


@dataclass(frozen=True)
class SourceDataset:
    """One dataset a SourceOrganization publishes, e.g. RBI's "Weekly
    Statistical Supplement". Deliberately no html_source_url/machine_
    source_url fields -- those live one level down, on SourceEndpoint."""

    source_org_id: int
    authority_level: str | None = None
    priority: int | None = None
    access_method: str | None = None  # csv_download | api | manual_entry | scrape | None (unresearched)
    cadence: str | None = None  # daily | weekly | monthly | quarterly | annual
    historical_start: str | None = None  # ISO-8601
    backfill_supported: bool | None = None
    license_notes: str | None = None
    dataset_id: int | None = None


@dataclass(frozen=True)
class SourceEndpoint:
    """One concrete access point for a SourceDataset. A dataset may have
    zero, one, or many endpoints of different formats -- never required to
    declare exactly one HTML and one machine URL."""

    dataset_id: int
    url: str | None = None  # None when not yet verified -- never a guessed URL
    access_method: str | None = None
    priority: int | None = None
    enabled: bool = True
    authentication_type: str | None = None
    parser_config: str | None = None  # JSON text
    availability_status: str | None = None  # unverified | verified | broken
    last_verified_at: str | None = None
    endpoint_id: int | None = None


@dataclass(frozen=True)
class EconomicIndicator:
    """A CONCEPT, e.g. "Consumer Price Inflation" -- semantic/reporting
    metadata only. No series_key: that belongs to EconomicSeries, the
    measurable stream(s) this concept maps to."""

    name: str
    category: str
    economic_meaning: str | None = None
    higher_is: str | None = None  # good | bad | neutral
    leading_lagging: str | None = None  # leading | lagging | coincident
    report_section: str | None = None
    headline_weight: float | None = None
    preferred_chart_window: str | None = None
    material_change_mom: float | None = None
    material_change_yoy: float | None = None
    material_change_ytd: float | None = None
    status: str = "registered_only"
    indicator_id: int | None = None

    def __post_init__(self) -> None:
        if self.status not in INDICATOR_STATUSES:
            raise ValueError(f"invalid status: {self.status!r}, must be one of {INDICATOR_STATUSES}")


@dataclass(frozen=True)
class EconomicSeries:
    """A measurable STREAM, e.g. "CPI Combined YoY -- India". Owns
    series_key; many of these can point at the same indicator_id (by
    geography/frequency/source)."""

    indicator_id: int
    series_key: str
    dataset_id: int | None = None
    geography: str | None = None
    unit: str | None = None
    frequency: str | None = None  # daily | weekly | monthly | quarterly | annual
    seasonal_adjustment: str | None = None  # sa | nsa
    notes: str | None = None
    series_id: int | None = None


@dataclass(frozen=True)
class EconomicObservation:
    """One (series, period, vintage) fact -- a single row in the canonical
    economic_observations vintage table. Never overwritten: a revision is
    a new row with the same series_id/period and a new vintage."""

    series_id: int
    period: str  # "2026" | "2026-07" | "2026-07-15", matching the series' frequency
    period_type: str  # annual | quarterly | monthly | weekly | daily
    release_date: str  # ISO-8601 date this vintage was published
    vintage: str  # ISO-8601 date identifying this specific revision
    revision_status: str  # provisional | revised | final
    value: float
    unit: str
    raw_object_id: int | None = None
    ingested_at: str = ""  # ISO-8601; filled by the repository layer if blank
    observation_id: int | None = None

    def __post_init__(self) -> None:
        if self.revision_status not in REVISION_STATUSES:
            raise ValueError(
                f"invalid revision_status: {self.revision_status!r}, must be one of {REVISION_STATUSES}"
            )
