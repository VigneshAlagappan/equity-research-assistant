"""Evidence labeling: FACT / CALCULATION / MANAGEMENT_STATEMENT / INFERENCE.

(README: Evidence & Citations) Every claim the research assistant makes must
carry one of these labels, with FACT/CALCULATION citing their source input
and INFERENCE never presented as confirmed:

    FACT                  reported number or statement, with source
    CALCULATION           deterministic computation, with inputs cited
    MANAGEMENT STATEMENT  quoted/paraphrased commentary, with source
    INFERENCE             reasoning that connects facts — never presented as confirmed

MANAGEMENT_STATEMENT isn't producible yet — no documents are ingested until
the Investor Relations pipeline lands (README: Implementation Sequence, step
7) — but the label exists now so the scheme doesn't change shape later.
"""

from __future__ import annotations

from dataclasses import dataclass

EVIDENCE_KINDS = ("FACT", "CALCULATION", "MANAGEMENT_STATEMENT", "INFERENCE")


@dataclass(frozen=True)
class Evidence:
    """One retrieved, deterministic data point handed to the LLM as grounding.

    The LLM only ever sees Evidence, never raw database rows — retrieval never
    calls the LLM (README: Retrieval Architecture), and the assistant is
    instructed to restate these lines, not invent new numbers.
    """

    kind: str  # one of EVIDENCE_KINDS
    company_id: str
    label: str  # e.g. "Net Profit FY2024" or "ROA (FY2024)"
    value: str  # pre-formatted value string, e.g. "20,500.00 INR_CRORE" or "1.24%"
    citation: str  # e.g. "reported for FY2024 (only source available)"

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError(f"kind must be one of {EVIDENCE_KINDS}, got {self.kind!r}")

    def as_prompt_line(self) -> str:
        return f"[{self.kind}] {self.company_id} — {self.label}: {self.value} ({self.citation})"


def render_evidence_block(evidence: list[Evidence]) -> str:
    """Render Evidence into the exact text block handed to the LLM prompt, in order given."""
    return "\n".join(e.as_prompt_line() for e in evidence)
