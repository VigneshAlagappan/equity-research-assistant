"""research/macro_knowledge_builder.py -- the Anthropic client is mocked
throughout, same pattern as tests/test_knowledge_builder.py."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from research.macro_knowledge_builder import MacroClassificationResult, classify_macro_factors
from storage.repositories import add_industry, add_sector, list_all_knowledge_entities, list_all_knowledge_relationships


class _FakeMessages:
    def __init__(self, text: str | None, stop_reason: str, captured: list) -> None:
        self._text = text
        self._stop_reason = stop_reason
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        content = [SimpleNamespace(type="text", text=self._text)] if self._text else []
        return SimpleNamespace(content=content, stop_reason=self._stop_reason)


class _FakeClient:
    def __init__(self, text: str | None, stop_reason: str, captured: list) -> None:
        self.messages = _FakeMessages(text, stop_reason, captured)


def _install_fake_client(monkeypatch, text: str | None, stop_reason: str = "end_turn") -> list:
    captured: list = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(text, stop_reason, captured),
    )
    return captured


@pytest.fixture
def macro_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    add_sector(db_conn, "Financial Services")
    add_industry(db_conn, "Banks")
    db_conn.execute(
        "INSERT INTO macro_observations (series_key, region, period_type, period, value, unit, source, "
        "source_file, source_url, retrieved_at, parser_version, created_at) "
        "VALUES ('repo_rate', NULL, 'annual', '2024', 6.5, 'percent', 'rbi', NULL, NULL, '2024-01-01', 'v1', '2024-01-01')"
    )
    db_conn.execute(
        "INSERT INTO macro_observations (series_key, region, period_type, period, value, unit, source, "
        "source_file, source_url, retrieved_at, parser_version, created_at) "
        "VALUES ('district_rainfall_kerala_idukki', NULL, 'annual', '2024', 120.0, 'mm', 'iitm', NULL, NULL, "
        "'2024-01-01', 'v1', '2024-01-01')"
    )
    db_conn.commit()
    return db_conn


_VALID_RESPONSE = """{
  "factors": [
    {
      "series_key": "repo_rate",
      "relationships": [
        {"relationship_type": "DRIVES", "target_industry": "Banks"},
        {"relationship_type": "EXPOSED_TO", "target_industry": "Financial Services"}
      ]
    },
    {
      "series_key": "district_rainfall_kerala_idukki",
      "relationships": []
    }
  ]
}"""


def test_classifies_new_series_into_factor_entities_and_relationships(macro_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, _VALID_RESPONSE)

    result = classify_macro_factors(macro_conn)

    assert result.series_classified == 1  # rainfall series had zero relationships -- not counted
    assert result.factors_created == 1
    assert result.relationships_created == 2
    assert result.batches_failed == 0

    entities = list_all_knowledge_entities(macro_conn)
    factor = next(e for e in entities if e["entity_type"] == "MacroFactor")
    assert factor["name"] == "Repo Rate"
    assert factor["company_id"] is None

    industries = {e["name"] for e in entities if e["entity_type"] == "Industry"}
    assert industries == {"Banks", "Financial Services"}

    relationships = list_all_knowledge_relationships(macro_conn)
    assert len(relationships) == 2
    assert all(r["claim_id"] is None for r in relationships)
    assert {r["relationship_type"] for r in relationships} == {"DRIVES", "EXPOSED_TO"}


def test_already_classified_series_is_not_resent_to_the_model(macro_conn: sqlite3.Connection, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch, _VALID_RESPONSE)
    classify_macro_factors(macro_conn)
    assert len(captured) == 1

    # A second run should find nothing new to classify -- repo_rate already
    # has a MacroFactor entity, and the rainfall series produced none last
    # time (an orphan-avoiding, not a "retry forever" outcome) so it's
    # still eligible... but with no fake-client call captured this time,
    # any attempt to actually call the model would raise on an empty
    # captured list assumption below if it happened.
    captured.clear()
    result = classify_macro_factors(macro_conn)
    # repo_rate is skipped (already classified); rainfall is retried since
    # it never got a MacroFactor entity -- one series, one batch, one call.
    assert len(captured) == 1
    assert result.series_classified == 0  # still produces nothing for the rainfall series


def test_hallucinated_industry_name_is_dropped_not_stored(macro_conn: sqlite3.Connection, monkeypatch) -> None:
    response = """{
      "factors": [
        {
          "series_key": "repo_rate",
          "relationships": [{"relationship_type": "DRIVES", "target_industry": "Made Up Sector"}]
        }
      ]
    }"""
    _install_fake_client(monkeypatch, response)

    result = classify_macro_factors(macro_conn)

    assert result.series_classified == 0
    assert result.relationships_created == 0
    assert list_all_knowledge_entities(macro_conn) == []


def test_hallucinated_series_key_is_dropped_not_stored(macro_conn: sqlite3.Connection, monkeypatch) -> None:
    response = """{
      "factors": [
        {"series_key": "not_a_real_series", "relationships": [{"relationship_type": "DRIVES", "target_industry": "Banks"}]}
      ]
    }"""
    _install_fake_client(monkeypatch, response)

    result = classify_macro_factors(macro_conn)

    assert result.series_classified == 0
    assert list_all_knowledge_entities(macro_conn) == []


def test_no_sectors_or_industries_on_file_skips_without_calling_the_model(db_conn: sqlite3.Connection, monkeypatch) -> None:
    db_conn.execute(
        "INSERT INTO macro_observations (series_key, region, period_type, period, value, unit, source, "
        "source_file, source_url, retrieved_at, parser_version, created_at) "
        "VALUES ('repo_rate', NULL, 'annual', '2024', 6.5, 'percent', 'rbi', NULL, NULL, '2024-01-01', 'v1', '2024-01-01')"
    )
    db_conn.commit()
    captured = _install_fake_client(monkeypatch, _VALID_RESPONSE)

    result = classify_macro_factors(db_conn)

    assert result == MacroClassificationResult()
    assert captured == []


def test_unparseable_response_is_logged_as_a_failed_batch_not_raised(macro_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "not json at all")

    result = classify_macro_factors(macro_conn)

    assert result.batches_failed == 1
    assert result.series_classified == 0


def test_a_persistence_failure_stops_the_loop_cleanly_without_raising(macro_conn: sqlite3.Connection, monkeypatch) -> None:
    # A connection dying mid-run (the real, observed production case on a
    # long-lived Neon-pooled connection) surfaces as some other exception
    # from the persistence layer, not MacroClassificationError -- simulated
    # here by making get_or_create_knowledge_entity explode.
    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    monkeypatch.setattr(
        "research.macro_knowledge_builder.get_or_create_knowledge_entity",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("connection already closed")),
    )

    result = classify_macro_factors(macro_conn)  # must not raise

    assert result.batches_failed == 1
    assert result.factors_created == 0
