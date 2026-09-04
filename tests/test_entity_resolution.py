"""context/entity_resolution.py — pure unit tests, no DB. Exercised against
the real duplicate-entity cases a read-only query against the live database
found (this feature's implementation plan): AMBUJACEM's extracted legal
name IS the same company, but ADANIPOWER's genuine subsidiaries and
ADANIENT's auditor/extraction-noise rows are NOT — the whole point of the
narrow, exact-match-only rule over any kind of fuzzy similarity score."""

from __future__ import annotations

from context.entity_resolution import is_same_company_identity


def _company_row(**overrides) -> dict:
    defaults = dict(
        company_id="AMBUJACEM", legal_name="Ambuja Cements Limited", display_name="Ambuja Cements",
        nse_symbol="AMBUJACEM", bse_code="500425",
    )
    defaults.update(overrides)
    return defaults


def test_exact_legal_name_match_with_corporate_suffix() -> None:
    assert is_same_company_identity("AMBUJA CEMENTS LIMITED", _company_row()) is True


def test_exact_display_name_match_is_case_insensitive() -> None:
    assert is_same_company_identity("ambuja cements", _company_row()) is True


def test_exact_ticker_match() -> None:
    assert is_same_company_identity("AMBUJACEM", _company_row()) is True


def test_exact_company_id_match() -> None:
    assert is_same_company_identity("AMBUJACEM", _company_row(nse_symbol=None)) is True


def test_a_real_but_different_company_is_not_a_match() -> None:
    """ACC is genuinely a different, separately-registered company (a real
    cement peer) that happens to be extracted alongside AMBUJACEM's own
    documents -- must never be merged into it."""
    assert is_same_company_identity("ACC", _company_row()) is False


def test_a_genuine_subsidiary_is_not_a_match() -> None:
    """ADANIPOWER real duplicate-entity case: 'Korba West Power Company
    Limited' is a real, distinct subsidiary sharing ADANIPOWER's company_id
    scope, not a spelling variant of ADANIPOWER itself."""
    adanipower = dict(
        company_id="ADANIPOWER", legal_name="Adani Power Limited", display_name="Adani Power",
        nse_symbol="ADANIPOWER", bse_code="533096",
    )
    assert is_same_company_identity("Korba West Power Company Limited", adanipower) is False
    assert is_same_company_identity("Adani Power Dahej Ltd.", adanipower) is False


def test_an_auditor_or_extraction_noise_is_not_a_match() -> None:
    """ADANIENT real duplicate-entity case: an auditor's name and garbled
    extraction noise both correctly fail to match."""
    adanient = dict(
        company_id="ADANIENT", legal_name="Adani Enterprises Limited", display_name="Adani Enterprises",
        nse_symbol="ADANIENT", bse_code="512599",
    )
    assert is_same_company_identity("M/s. Dharmesh Parikh & Co.", adanient) is False
    assert is_same_company_identity("회사", adanient) is False


def test_empty_or_none_name_is_never_a_match() -> None:
    assert is_same_company_identity("", _company_row()) is False
    assert is_same_company_identity("   ", _company_row()) is False


def test_null_identifier_columns_do_not_crash_or_falsely_match() -> None:
    """A company with no nse_symbol/bse_code on file (common for a
    manually-added or foreign company) must not let an empty-string
    normalization of NULL accidentally match an empty/whitespace-only
    extracted name."""
    row = dict(company_id="FOOCO", legal_name="Foo Company Limited", display_name="Foo Company", nse_symbol=None, bse_code=None)
    assert is_same_company_identity("", row) is False
    assert is_same_company_identity("Foo Company", row) is True
