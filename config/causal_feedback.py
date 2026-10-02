"""Controlled vocabulary for structured causal feedback
(docs/L5_CAUSAL_INTELLIGENCE_PLAN.md 42.2, docs/L5_MVP_TASK_PLAN.md M8).

Feedback is captured at four levels -- investigation, hypothesis, path (a
hypothesis's chain in the MVP) and edge (one link of that chain) -- and only
ever recorded in the ledger. In the MVP nothing reads it to change a graph or
a weight; changing the taxonomy is an ontology-version change.
"""

from __future__ import annotations

FEEDBACK_TYPES: dict[str, str] = {
    "CORRECT": "Correct and useful",
    "NOT_RELEVANT": "True in general, not important here",
    "WRONG_RELATIONSHIP": "This link does not hold",
    "MISSING_DRIVER": "An important cause is missing",
    "MISSING_MEDIATOR": "An intermediate step is missing",
    "OVERSTATED": "The effect is overstated",
    "UNDERSTATED": "The effect is understated",
    "WRONG_TIMING": "The timing is off",
    "INSUFFICIENT_EVIDENCE": "Not enough evidence shown",
}

TARGET_LEVELS = ("investigation", "hypothesis", "path", "edge")

#: Which feedback types make sense at which level. A type outside its level is
#: rejected rather than stored, so analysis never has to guess what a vote meant.
TYPES_BY_LEVEL: dict[str, frozenset[str]] = {
    "investigation": frozenset({"CORRECT", "MISSING_DRIVER", "INSUFFICIENT_EVIDENCE"}),
    "hypothesis": frozenset({"CORRECT", "NOT_RELEVANT", "MISSING_DRIVER", "INSUFFICIENT_EVIDENCE"}),
    "path": frozenset({"CORRECT", "NOT_RELEVANT", "MISSING_DRIVER", "MISSING_MEDIATOR"}),
    "edge": frozenset({
        "CORRECT", "NOT_RELEVANT", "WRONG_RELATIONSHIP", "OVERSTATED", "UNDERSTATED", "WRONG_TIMING",
        "INSUFFICIENT_EVIDENCE",
    }),
}

USER_CLASSES = ("ordinary", "expert", "internal")
MAX_COMMENT_CHARS = 2000


class FeedbackValidationError(ValueError):
    pass


def validate_feedback(level: str, feedback_type: str, comment: str | None) -> str | None:
    """Returns the cleaned comment (or None); raises FeedbackValidationError."""
    if level not in TARGET_LEVELS:
        raise FeedbackValidationError(f"unknown target level {level!r}")
    if feedback_type not in FEEDBACK_TYPES:
        raise FeedbackValidationError(f"unknown feedback type {feedback_type!r}")
    if feedback_type not in TYPES_BY_LEVEL[level]:
        raise FeedbackValidationError(f"{feedback_type} is not valid at the {level} level")
    cleaned = (comment or "").strip()
    if len(cleaned) > MAX_COMMENT_CHARS:
        raise FeedbackValidationError(f"comment longer than {MAX_COMMENT_CHARS} characters")
    return cleaned or None
