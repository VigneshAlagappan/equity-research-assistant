"""sources/fred.py — the US macro-data adapter, live-fetched from FRED's
public CSV endpoint. HTTP is mocked (same spirit as yfinance/live_quote.py
tests elsewhere — never hit a real external service in the test suite)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from sources.fred import fetch_fred_series


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body


def _install_fake_fred_csv(monkeypatch, csv_text: str) -> None:
    @contextmanager
    def fake_urlopen(req, timeout=None):
        yield _FakeResponse(csv_text.encode("utf-8"))

    monkeypatch.setattr("sources.fred.urllib.request.urlopen", fake_urlopen)


def test_fetch_parses_series_into_observations(monkeypatch) -> None:
    _install_fake_fred_csv(monkeypatch, "observation_date,FEDFUNDS\n2020-01-01,1.55\n2020-02-01,1.58\n")

    observations = fetch_fred_series("FEDFUNDS", unit="PERCENT")

    assert len(observations) == 2
    assert observations[0].series_key == "fedfunds"  # defaults to series_id lowercased
    assert observations[0].period == "2020-01-01"
    assert observations[0].period_type == "dated"  # FRED's CSV is always full YYYY-MM-DD
    assert observations[0].value == 1.55
    assert observations[0].unit == "PERCENT"
    assert observations[0].source == "fred"


def test_fetch_series_key_override(monkeypatch) -> None:
    _install_fake_fred_csv(monkeypatch, "observation_date,FEDFUNDS\n2020-01-01,1.55\n")

    observations = fetch_fred_series("FEDFUNDS", unit="PERCENT", series_key="fed_funds_rate")

    assert observations[0].series_key == "fed_funds_rate"


def test_fetch_skips_missing_value_marker(monkeypatch) -> None:
    """FRED publishes "." for a period with no observation — not a parse
    failure, just genuinely absent data, same as a blank cell elsewhere."""
    _install_fake_fred_csv(monkeypatch, "observation_date,DGS10\n2020-01-01,.\n2020-01-02,1.55\n")

    observations = fetch_fred_series("DGS10", unit="PERCENT")

    assert len(observations) == 1
    assert observations[0].period == "2020-01-02"


def test_fetch_stamps_provenance(monkeypatch) -> None:
    _install_fake_fred_csv(monkeypatch, "observation_date,FEDFUNDS\n2020-01-01,1.55\n")

    obs = fetch_fred_series("FEDFUNDS", unit="PERCENT")[0]

    assert obs.source_file == "fred:FEDFUNDS"
    assert obs.parser_version
    assert obs.region is None


def test_fetch_region_override(monkeypatch) -> None:
    _install_fake_fred_csv(monkeypatch, "observation_date,CAUR\n2020-01-01,4.2\n")

    obs = fetch_fred_series("CAUR", unit="PERCENT", region="California")[0]

    assert obs.region == "California"


def test_fetch_returns_empty_list_for_no_observations(monkeypatch) -> None:
    _install_fake_fred_csv(monkeypatch, "observation_date,FEDFUNDS\n")

    assert fetch_fred_series("FEDFUNDS", unit="PERCENT") == []


def test_fetch_rejects_unexpected_csv_shape(monkeypatch) -> None:
    _install_fake_fred_csv(monkeypatch, "not_a_date_column,FEDFUNDS\n2020-01-01,1.55\n")

    with pytest.raises(ValueError, match="Unexpected FRED CSV shape"):
        fetch_fred_series("FEDFUNDS", unit="PERCENT")
