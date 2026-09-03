"""Configurable Indicator Framework (indicators/*.py).

Covers the four things V1 promises: a registry of versioned system rules,
per-field Company > Sector > Global > System configuration resolution, a
real end-to-end rule evaluation producing the expected classification with
an audit row, and the Settings routes' save/reset behaviour.

Fixtures are the suite's existing ones — `db_conn` (tests/conftest.py) and
tests/test_web.py's `_build_app` — no new fixture machinery.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

from companies.registry import register_company, seed_companies
from indicators.config import (
    RuleOverride,
    resolve_effective_config,
    validate_override,
)
from indicators.evaluation import (
    effective_configs_for_company,
    evaluate_company_indicators,
    group_by_classification,
)
from indicators.framework import (
    CLASSIFICATIONS,
    OBSERVATION,
    POSITIVE,
    SEVERITIES,
    WARNING,
    InvalidIndicatorConfigError,
    get_rule,
    list_rules,
)
from indicators.settings import build_rules_settings, reset_rule_override, save_rule_override
from ingestion.pipeline import ingest_file
from normalization.financials import ensure_metric_vocabulary
from storage.database import init_db
from storage.indicator_repository import select_indicator_evaluations
from storage.repositories import create_user, insert_shareholding_observations
from tests.test_screener_adapter import _make_screener_workbook
from tests.test_web import _build_app

DECLINE_RULE = "shareholding.promoter_holding_decline"
INCREASE_RULE = "shareholding.promoter_holding_increase"
NET_PROFIT_RULE = "financial_trajectory.net_profit_yoy_move"


def _quarter(fiscal_year: str, quarter: str, promoter: float) -> SimpleNamespace:
    """The shape storage.repositories.insert_shareholding_observations reads
    (sources/nse_shareholding.py's summary object) — using the real write
    path rather than hand-written INSERTs in a test."""
    return SimpleNamespace(
        fiscal_year=fiscal_year,
        quarter=quarter,
        promoter_percent=promoter,
        public_percent=100.0 - promoter,
        employee_trust_percent=None,
        source_url=f"https://nsearchives.nseindia.com/{fiscal_year}{quarter}.xml",
        submission_date=f"{fiscal_year[2:]}-01-15",
    )


@pytest.fixture
def shareholding_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    """HDFCBANK with two quarters of promoter holding: 26.00% -> 23.50%,
    a 2.50pp decline (over the 1.0pp system default, under a 3.0pp one)."""
    seed_companies(db_conn)
    insert_shareholding_observations(
        db_conn, "HDFCBANK", [_quarter("FY2025", "Q1", 26.00), _quarter("FY2025", "Q2", 23.50)]
    )
    return db_conn


# ------------------------------------------------------------------
# Rule registry
# ------------------------------------------------------------------


def test_registry_exposes_versioned_rules_across_families() -> None:
    rules = list_rules()
    assert len(rules) >= 3
    assert {r.family for r in rules} >= {"shareholding", "financial_trajectory"}
    for rule in rules:
        assert rule.default_classification in CLASSIFICATIONS
        assert rule.default_severity in SEVERITIES
        assert rule.version  # every rule carries a version for the audit trail
        assert rule.required_facts


def test_registry_lookup_and_search() -> None:
    rule = get_rule(DECLINE_RULE)
    assert rule is not None and rule.default_classification == WARNING
    assert get_rule("nope.not_a_rule") is None
    assert [r.rule_id for r in list_rules(search="promoter")] == [DECLINE_RULE, INCREASE_RULE]


def test_registry_rules_are_frozen_so_config_cannot_mutate_them() -> None:
    """Spec section 4: a user's configuration must never modify the system
    rule. The rule object is frozen, so it can't be, even by accident."""
    rule = get_rule(DECLINE_RULE)
    with pytest.raises(Exception):
        rule.default_classification = OBSERVATION  # type: ignore[misc]


# ------------------------------------------------------------------
# Configuration resolution (pure)
# ------------------------------------------------------------------


def test_system_default_applies_with_no_overrides() -> None:
    rule = get_rule(DECLINE_RULE)
    config = resolve_effective_config(rule, [], sector="Banks", company_id="HDFCBANK")
    assert config.enabled is True
    assert config.classification == WARNING
    assert config.thresholds["decline_pp"] == 1.0
    assert config.most_specific_scope == "system"
    assert config.is_overridden is False


def test_company_beats_sector_beats_global_beats_system() -> None:
    rule = get_rule(DECLINE_RULE)
    overrides = [
        RuleOverride(DECLINE_RULE, "global", "", classification=OBSERVATION),
        RuleOverride(DECLINE_RULE, "sector", "Banks", classification=POSITIVE),
        RuleOverride(DECLINE_RULE, "company", "HDFCBANK", classification=WARNING),
    ]
    assert resolve_effective_config(
        rule, overrides, sector="Banks", company_id="HDFCBANK"
    ).classification == WARNING
    assert resolve_effective_config(
        rule, overrides, sector="Banks", company_id="ICICIBANK"
    ).classification == POSITIVE
    assert resolve_effective_config(
        rule, overrides, sector="IT", company_id="INFY"
    ).classification == OBSERVATION
    assert resolve_effective_config(rule, [], sector="IT", company_id="INFY").classification == WARNING


def test_fields_resolve_independently() -> None:
    """A company-scoped classification override must not freeze the
    threshold the user configured globally."""
    rule = get_rule(DECLINE_RULE)
    overrides = [
        RuleOverride(DECLINE_RULE, "global", "", thresholds={"decline_pp": 4.0}),
        RuleOverride(DECLINE_RULE, "company", "HDFCBANK", classification=OBSERVATION),
    ]
    config = resolve_effective_config(rule, overrides, sector="Banks", company_id="HDFCBANK")
    assert config.classification == OBSERVATION
    assert config.thresholds["decline_pp"] == 4.0
    assert config.sources["classification"] == "company:HDFCBANK"
    assert config.sources["threshold:decline_pp"] == "global"
    assert config.most_specific_scope == "company:HDFCBANK"


def test_disabled_at_sector_scope() -> None:
    rule = get_rule(DECLINE_RULE)
    overrides = [RuleOverride(DECLINE_RULE, "sector", "Banks", enabled=False)]
    assert resolve_effective_config(rule, overrides, sector="Banks", company_id="HDFCBANK").enabled is False
    assert resolve_effective_config(rule, overrides, sector="IT", company_id="INFY").enabled is True


def test_override_of_another_rule_or_scope_value_is_ignored() -> None:
    rule = get_rule(DECLINE_RULE)
    overrides = [
        RuleOverride(INCREASE_RULE, "global", "", classification=OBSERVATION),
        RuleOverride(DECLINE_RULE, "company", "INFY", classification=POSITIVE),
    ]
    assert resolve_effective_config(
        rule, overrides, sector="Banks", company_id="HDFCBANK"
    ).classification == WARNING


def test_validation_rejects_unknown_vocabulary() -> None:
    rule = get_rule(DECLINE_RULE)
    with pytest.raises(InvalidIndicatorConfigError):
        validate_override(rule, RuleOverride(DECLINE_RULE, "planet", "Mars"))
    with pytest.raises(InvalidIndicatorConfigError):
        validate_override(rule, RuleOverride(DECLINE_RULE, "global", "", classification="critical"))
    with pytest.raises(InvalidIndicatorConfigError):
        validate_override(rule, RuleOverride(DECLINE_RULE, "global", "", thresholds={"made_up": 1.0}))
    with pytest.raises(InvalidIndicatorConfigError):
        validate_override(rule, RuleOverride(DECLINE_RULE, "global", "", thresholds={"decline_pp": 999.0}))
    with pytest.raises(InvalidIndicatorConfigError):
        validate_override(rule, RuleOverride(DECLINE_RULE, "sector", ""))


# ------------------------------------------------------------------
# Evaluation — real facts, real classification, real audit row
# ------------------------------------------------------------------


def test_promoter_decline_triggers_as_warning(shareholding_conn: sqlite3.Connection) -> None:
    triggered = evaluate_company_indicators(shareholding_conn, "HDFCBANK")
    decline = next(i for i in triggered if i.rule_id == DECLINE_RULE)
    assert decline.classification == WARNING
    assert decline.classification_label == "Warning"
    assert decline.facts["previous_promoter_percent"] == 26.00
    assert decline.facts["latest_promoter_percent"] == 23.50
    assert decline.facts["change_pp"] == pytest.approx(-2.5)
    assert decline.period_label == "Q2 FY2025"
    # Strictly factual explanation, citing both values and the threshold.
    assert "26.00%" in decline.explanation and "23.50%" in decline.explanation
    assert "2.50 percentage points" in decline.explanation
    assert "1pp threshold" in decline.explanation
    # The mirror-image rule must not also fire on a decline.
    assert not any(i.rule_id == INCREASE_RULE for i in triggered)


def test_threshold_override_can_silence_a_rule(shareholding_conn: sqlite3.Connection) -> None:
    user_id = create_user(shareholding_conn, "a@example.com", generate_password_hash("x" * 8))
    assert any(
        i.rule_id == DECLINE_RULE
        for i in evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=user_id)
    )
    save_rule_override(
        shareholding_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="global", scope_value="",
        enabled=None, classification=None, thresholds={"decline_pp": 3.0},
    )
    assert not any(
        i.rule_id == DECLINE_RULE
        for i in evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=user_id)
    )


def test_company_scoped_classification_override_changes_the_column(
    shareholding_conn: sqlite3.Connection,
) -> None:
    user_id = create_user(shareholding_conn, "b@example.com", generate_password_hash("x" * 8))
    save_rule_override(
        shareholding_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="company",
        scope_value="HDFCBANK", enabled=None, classification=OBSERVATION, thresholds=None,
    )
    grouped = group_by_classification(
        evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=user_id)
    )
    assert [i.rule_id for i in grouped[OBSERVATION]] == [DECLINE_RULE]
    assert grouped[WARNING] == []
    assert grouped[OBSERVATION][0].scope_applied == "company:HDFCBANK"
    # A different company still resolves to the system default.
    assert effective_configs_for_company(
        shareholding_conn, "ICICIBANK", user_id=user_id
    )[DECLINE_RULE].classification == WARNING


def test_disabled_rule_is_never_evaluated(shareholding_conn: sqlite3.Connection) -> None:
    user_id = create_user(shareholding_conn, "c@example.com", generate_password_hash("x" * 8))
    save_rule_override(
        shareholding_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="global", scope_value="",
        enabled=False, classification=None, thresholds=None,
    )
    triggered = evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=user_id)
    assert not any(i.rule_id == DECLINE_RULE for i in triggered)


def test_no_facts_means_no_indicators_not_an_error(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    assert evaluate_company_indicators(db_conn, "ICICIBANK") == []


def test_single_quarter_on_file_does_not_trigger(db_conn: sqlite3.Connection) -> None:
    """One quarter has nothing to compare against — absence isn't a decline."""
    seed_companies(db_conn)
    insert_shareholding_observations(db_conn, "HDFCBANK", [_quarter("FY2025", "Q1", 26.0)])
    assert evaluate_company_indicators(db_conn, "HDFCBANK") == []


def test_yoy_rule_fires_on_real_ingested_financials(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    """The same fixture tests/test_analytics_patterns.py uses: HDFCBANK
    net_profit 17,000 (FY2023) -> 20,500 (FY2024), +20.6% — under the 25%
    system default, over a user-configured 15% threshold."""
    seed_companies(db_conn)
    workbook = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(workbook)
    ingest_file(db_conn, workbook, company_id="HDFCBANK", source_id="screener")

    assert not any(
        i.rule_id == NET_PROFIT_RULE for i in evaluate_company_indicators(db_conn, "HDFCBANK")
    )

    user_id = create_user(db_conn, "d@example.com", generate_password_hash("x" * 8))
    save_rule_override(
        db_conn, user_id=user_id, rule_id=NET_PROFIT_RULE, scope_type="global", scope_value="",
        enabled=None, classification=None, thresholds={"move_percent": 15.0},
    )
    triggered = evaluate_company_indicators(db_conn, "HDFCBANK", user_id=user_id)
    yoy = next(i for i in triggered if i.rule_id == NET_PROFIT_RULE)
    assert yoy.classification == OBSERVATION  # direction-agnostic by design
    assert yoy.facts["yoy_percent"] == pytest.approx(20.588, abs=0.01)
    assert yoy.period_label == "FY2024"
    assert "Net Profit rose 20.6% year over year" in yoy.explanation


def test_evaluation_is_deterministic(shareholding_conn: sqlite3.Connection) -> None:
    """Same facts + same rule version + same configuration -> same result."""
    first = evaluate_company_indicators(shareholding_conn, "HDFCBANK", persist=False)
    second = evaluate_company_indicators(shareholding_conn, "HDFCBANK", persist=False)
    assert [i.result_hash() for i in first] == [i.result_hash() for i in second]


# ------------------------------------------------------------------
# Auditability
# ------------------------------------------------------------------


def test_audit_row_captures_everything_needed_to_reproduce(
    shareholding_conn: sqlite3.Connection,
) -> None:
    evaluate_company_indicators(shareholding_conn, "HDFCBANK")
    rows = [r for r in select_indicator_evaluations(shareholding_conn, "HDFCBANK") if r["rule_id"] == DECLINE_RULE]
    assert len(rows) == 1
    row = rows[0]
    assert row["rule_version"] == get_rule(DECLINE_RULE).version
    assert row["classification"] == WARNING
    assert row["severity"] in SEVERITIES
    assert row["scope_applied"] == "system"
    assert row["period_label"] == "Q2 FY2025"
    assert row["provenance"].startswith("https://")
    assert row["evaluated_at"]
    assert json.loads(row["facts_json"])["previous_promoter_percent"] == 26.0
    config = json.loads(row["effective_config_json"])
    assert config["thresholds"]["decline_pp"] == 1.0
    assert config["enabled"] is True


def test_unchanged_result_is_not_re_appended(shareholding_conn: sqlite3.Connection) -> None:
    for _ in range(3):
        evaluate_company_indicators(shareholding_conn, "HDFCBANK")
    rows = [r for r in select_indicator_evaluations(shareholding_conn, "HDFCBANK") if r["rule_id"] == DECLINE_RULE]
    assert len(rows) == 1


def test_changed_configuration_appends_a_new_row_and_keeps_the_old(
    shareholding_conn: sqlite3.Connection,
) -> None:
    user_id = create_user(shareholding_conn, "e@example.com", generate_password_hash("x" * 8))
    evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=user_id)
    save_rule_override(
        shareholding_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="global", scope_value="",
        enabled=None, classification=OBSERVATION, thresholds=None,
    )
    evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=user_id)
    rows = [r for r in select_indicator_evaluations(shareholding_conn, "HDFCBANK") if r["rule_id"] == DECLINE_RULE]
    assert len(rows) == 2
    assert {r["classification"] for r in rows} == {WARNING, OBSERVATION}  # history preserved


def test_signed_out_evaluations_are_a_separate_audit_stream(
    shareholding_conn: sqlite3.Connection,
) -> None:
    user_id = create_user(shareholding_conn, "f@example.com", generate_password_hash("x" * 8))
    evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=None)
    evaluate_company_indicators(shareholding_conn, "HDFCBANK", user_id=user_id)
    rows = [r for r in select_indicator_evaluations(shareholding_conn, "HDFCBANK") if r["rule_id"] == DECLINE_RULE]
    assert {r["user_id"] for r in rows} == {None, user_id}


# ------------------------------------------------------------------
# Settings read model + routes
# ------------------------------------------------------------------


def test_settings_read_model_shows_defaults_and_overrides(db_conn: sqlite3.Connection) -> None:
    user_id = create_user(db_conn, "g@example.com", generate_password_hash("x" * 8))
    entries = {e["rule_id"]: e for e in build_rules_settings(db_conn, user_id)}
    entry = entries[DECLINE_RULE]
    assert entry["system"]["classification"] == WARNING
    assert entry["effective"]["overridden"] is False
    assert entry["overrides"] == []

    save_rule_override(
        db_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="sector", scope_value="Banks",
        enabled=None, classification=OBSERVATION, thresholds={"decline_pp": 2.0},
    )
    entry = {e["rule_id"]: e for e in build_rules_settings(db_conn, user_id)}[DECLINE_RULE]
    assert entry["system"]["classification"] == WARNING  # system rule untouched
    assert entry["effective"]["classification"] == WARNING  # global baseline unaffected
    assert [o["scope_label"] for o in entry["overrides"]] == ["sector:Banks"]
    assert entry["overrides"][0]["classification"] == OBSERVATION


def test_settings_read_model_search_filters(db_conn: sqlite3.Connection) -> None:
    user_id = create_user(db_conn, "h@example.com", generate_password_hash("x" * 8))
    assert [e["rule_id"] for e in build_rules_settings(db_conn, user_id, search="net_profit_yoy")] == [
        NET_PROFIT_RULE
    ]


def test_reset_removes_the_override_only(db_conn: sqlite3.Connection) -> None:
    user_id = create_user(db_conn, "i@example.com", generate_password_hash("x" * 8))
    save_rule_override(
        db_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="global", scope_value="",
        enabled=None, classification=OBSERVATION, thresholds=None,
    )
    assert reset_rule_override(
        db_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="global", scope_value=""
    ) is True
    # Falls back to the system default, and a second reset is a no-op.
    entry = {e["rule_id"]: e for e in build_rules_settings(db_conn, user_id)}[DECLINE_RULE]
    assert entry["effective"]["classification"] == WARNING
    assert reset_rule_override(
        db_conn, user_id=user_id, rule_id=DECLINE_RULE, scope_type="global", scope_value=""
    ) is False


def _signed_in_client(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "indicators_web.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    register_company(conn, "TESTCO", "Test Co Ltd", "Test Co", sector="Banks")
    insert_shareholding_observations(
        conn, "TESTCO", [_quarter("FY2025", "Q1", 40.0), _quarter("FY2025", "Q2", 35.0)]
    )
    user_id = create_user(conn, "web@example.com", generate_password_hash("x" * 8))
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
    return client


def test_settings_page_renders_indicator_rules(tmp_path: Path, monkeypatch) -> None:
    client = _signed_in_client(tmp_path, monkeypatch)
    response = client.get("/settings")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Indicator Rules" in body
    assert DECLINE_RULE in body


def test_settings_save_and_reset_routes(tmp_path: Path, monkeypatch) -> None:
    client = _signed_in_client(tmp_path, monkeypatch)
    save = client.post(
        "/settings/indicators",
        data={
            "rule_id": DECLINE_RULE, "scope_type": "sector", "scope_value": "Banks",
            "enabled": "inherit", "classification": OBSERVATION, "threshold__decline_pp": "2.5",
        },
    )
    assert save.status_code == 302

    body = client.get("/settings").get_data(as_text=True)
    assert "sector:Banks" not in body  # rendered as "Sector: Banks"
    assert "Sector: Banks" in body

    # And it really applies on the company page: TESTCO is in Banks and fell
    # 5.00pp, so the sector override reclassifies it out of Warnings.
    company = client.get("/companies/TESTCO?tab=indicators").get_data(as_text=True)
    assert "Promoter holding declined" in company
    assert "Observation" in company

    reset = client.post(
        "/settings/indicators/reset",
        data={"rule_id": DECLINE_RULE, "scope_type": "sector", "scope_value": "Banks"},
    )
    assert reset.status_code == 302
    body = client.get("/settings").get_data(as_text=True)
    assert "Sector: Banks" not in body


def test_settings_save_rejects_an_unknown_rule(tmp_path: Path, monkeypatch) -> None:
    client = _signed_in_client(tmp_path, monkeypatch)
    response = client.post("/settings/indicators", data={"rule_id": "nope.not_a_rule"})
    assert response.status_code == 404


def test_settings_save_requires_a_signed_in_user(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "anon.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    response = app.test_client().post(
        "/settings/indicators", data={"rule_id": DECLINE_RULE, "scope_type": "global"}
    )
    assert response.status_code == 403


def test_company_page_renders_the_indicators_section(tmp_path: Path, monkeypatch) -> None:
    client = _signed_in_client(tmp_path, monkeypatch)
    response = client.get("/companies/TESTCO")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="sec-indicators"' in body
    assert "Promoter holding declined" in body
    # Classification text always accompanies the color.
    assert "Warning" in body
    # Warnings column auto-opens because something fired; Positive stays collapsed.
    assert '<details class="ind-column ind-column-warning" open>' in body
    assert '<details class="ind-column ind-column-positive" >' in body


def test_company_page_with_no_triggered_indicators(tmp_path: Path, monkeypatch) -> None:
    client = _signed_in_client(tmp_path, monkeypatch)
    body = client.get("/companies/ICICIBANK").get_data(as_text=True)
    assert "No indicators are currently triggered" in body
