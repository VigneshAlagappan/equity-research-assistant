"""Blocks buy/sell/invest-now questions before they reach the LLM.

This product answers evidence-grounded research questions about a
company's financials, filings, and macro context -- it has no basis for
(and isn't licensed to give) a personal investment recommendation like
"should I buy this now". The LLM's own SYSTEM_PROMPT (research/
assistant.py) never claims to give trading advice, but nothing stopped a
user from asking for it directly and getting a fluent-sounding answer
anyway, tagged with a confidence label that reads like a real
recommendation. Caught here, before any evidence gathering or LLM call,
so the rejection is instant and the same for every ask/generate entry
point (web/app.py's _compute_answer_question and research_thread_generate
both call this)."""

from __future__ import annotations

import re

# Matches a decision verb (buy/sell/invest) combined with a timing or
# recommendation cue ("now", "should I", "good time", "worth it"), or a
# few fixed high-signal phrases on their own. Deliberately narrow -- a
# plain "investment" or "buy" mention elsewhere (e.g. "capital investment
# in new plants", "who bought the company") must not trip this.
_DECISION_VERB = r"(?:buy|sell|invest(?:ing)?)"
_TIMING_CUE = r"(?:now|today|right now|this (?:week|month|quarter|year))"
_RECOMMENDATION_CUE = (
    r"(?:should i|should we|is it (?:a )?good (?:time|idea)|"
    r"good (?:investment|buy|time to)|worth (?:it|buying|investing)|"
    r"right time)"
)

_PATTERNS = [
    re.compile(rf"\b{_DECISION_VERB}\b.{{0,40}}\b{_TIMING_CUE}\b", re.IGNORECASE),
    re.compile(rf"\b{_TIMING_CUE}\b.{{0,40}}\b{_DECISION_VERB}\b", re.IGNORECASE),
    re.compile(rf"\b{_RECOMMENDATION_CUE}\b.{{0,40}}\b{_DECISION_VERB}\b", re.IGNORECASE),
    re.compile(rf"\b{_DECISION_VERB}\b.{{0,40}}\b{_RECOMMENDATION_CUE}\b", re.IGNORECASE),
    re.compile(r"\bbuy or sell\b", re.IGNORECASE),
    re.compile(r"\bshould i (?:buy|sell|invest)\b", re.IGNORECASE),
    re.compile(r"\bis (?:this|it) a good (?:investment|buy|stock to buy)\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are)\b.{0,60}\bgood investments?\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are)\b.{0,60}\bgood buys?\b", re.IGNORECASE),
    re.compile(r"\bgood investment (?:opportunity|option|now|right now|currently)\b", re.IGNORECASE),
    re.compile(r"\binvestment opportunity now\b", re.IGNORECASE),
]

REJECTION_MESSAGE = (
    "This product is not meant to answer buy/sell/investment-timing questions "
    "(e.g. \"is X a good investment now?\", \"should I buy or sell?\"). It's a "
    "research assistant for evidence-grounded questions about a company's "
    "financials, filings, and macro context -- not a source of investment "
    "recommendations. Please rephrase as a research question (e.g. \"What is "
    "the revenue growth trend?\" or \"What risks are flagged in the latest "
    "filing?\")."
)


def is_investment_decision_question(question: str) -> bool:
    """True if `question` is asking for a buy/sell/invest-now recommendation
    rather than evidence-grounded research."""
    return any(pattern.search(question) for pattern in _PATTERNS)
