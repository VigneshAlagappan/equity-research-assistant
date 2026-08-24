from __future__ import annotations

import pytest

from research.evidence import Evidence, render_evidence_block


def test_evidence_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError):
        Evidence(kind="OPINION", company_id="HDFCBANK", label="x", value="1", citation="y")


@pytest.mark.parametrize("kind", ["FACT", "CALCULATION", "MANAGEMENT_STATEMENT", "INFERENCE"])
def test_evidence_accepts_every_documented_kind(kind: str) -> None:
    Evidence(kind=kind, company_id="HDFCBANK", label="x", value="1", citation="y")  # must not raise


def test_as_prompt_line_format() -> None:
    evidence = Evidence(
        kind="FACT", company_id="HDFCBANK", label="Net Profit FY2024",
        value="20,500.00 INR_CRORE", citation="reported for FY2024 (only source available)",
    )
    assert evidence.as_prompt_line() == (
        "[FACT] HDFCBANK — Net Profit FY2024: 20,500.00 INR_CRORE "
        "(reported for FY2024 (only source available))"
    )


def test_render_evidence_block_joins_lines_in_order() -> None:
    evidence = [
        Evidence(kind="FACT", company_id="HDFCBANK", label="a", value="1", citation="c1"),
        Evidence(kind="CALCULATION", company_id="HDFCBANK", label="b", value="2", citation="c2"),
    ]
    block = render_evidence_block(evidence)
    lines = block.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("[FACT]")
    assert lines[1].startswith("[CALCULATION]")


def test_render_evidence_block_empty_list() -> None:
    assert render_evidence_block([]) == ""
