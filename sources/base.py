"""SourceAdapter interface and the NormalizedObservation shape.

Every adapter (Screener, NSE, BSE, ...) outputs the same NormalizedObservation
regardless of the vendor-specific file format it read — the research,
retrieval, and calculation layers never depend on a vendor-specific shape
(README: Source Adapters).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NormalizedObservation:
    """One (company, metric, period) data point from a single source, pre-reconciliation.

    Mirrors financial_observations columns, minus the auto-assigned
    observation_id and the created_at timestamp the pipeline stamps on insert.
    """

    company_id: str
    metric_key: str
    period_type: str  # "annual" | "quarterly"
    fiscal_year: str  # "FY2025"
    value: float
    unit: str  # INR_CRORE | INR_LAKH | INR | PERCENT | RATIO | NUMBER
    source: str  # source_id, e.g. "screener"
    source_file: str
    parser_version: str
    quarter: str | None = None  # "Q1".."Q4", None for annual
    statement_type: str | None = None  # "consolidated" | "standalone"
    currency: str = "INR"
    source_url: str | None = None
    retrieved_at: str = ""  # ISO-8601; filled by the caller (pipeline stamps if blank)


class SourceAdapter(ABC):
    """Interface every vendor-specific adapter implements."""

    #: source_id this adapter parses for (must match a row in the sources table)
    source_id: str

    @abstractmethod
    def parse(self, file_path: Path, company_id: str, **kwargs: object) -> list[NormalizedObservation]:
        """Parse a raw vendor file into NormalizedObservations. Never mutates file_path."""
        raise NotImplementedError
