"""Link tagger (proof of concept): assigns each piece of evidence in an
evaluated hypothesis to the causal LINK of the hypothesis's chain it bears on.

Why it exists: the L5 evaluation step runs on Haiku by owner decision
(config.settings.CAUSAL_EVALUATION_MODEL), and Haiku tags evidence to links far
less often than Sonnet did (25-33% vs 88% in the first measured runs). Tagging
is a narrow task, so it gets its own small call on Jev's model chain
(config.settings.JEV_CLASSIFIER_MODEL_CHAIN via llm.router.route_explicit_chain:
the configured cheap/free-tier model first, Haiku as fallback) -- the same
routing helper llm/complexity.py uses, but a different task and prompt. This is
NOT Jev (the complexity classifier) and not deterministic: it is an LLM call
whose accuracy must be measured, not assumed (scripts/tagger_poc.py).

Link i connects chain step i to step i+1, so N steps give links 0..N-2. An item
that bears on the hypothesis as a whole, or that the model is unsure of, is
tagged None -- never guessed.
"""

from __future__ import annotations

import json
import logging
import re

from config.settings import JEV_CLASSIFIER_MODEL_CHAIN
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, RouteResult, route_explicit_chain

logger = logging.getLogger(__name__)

_MAX_TOKENS = 700
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = """You assign pieces of evidence to the causal link of a hypothesis's chain that they bear on.

You are given numbered chain steps (0, 1, 2, ...) and numbered evidence items. Link L connects step L to step L+1, \
so N steps give links 0 .. N-2. For each evidence item choose the single link whose cause or effect the item most \
directly measures or describes — for example, with steps 0 "Steel prices rise", 1 "Material cost per unit rises", \
2 "Operating margin falls": a steel price figure is link 0, a cost-of-materials figure is link 0 (it measures that \
link's effect), and an operating-margin figure is link 1.

Use null when the item bears on the hypothesis as a whole, or on no single link, or when you are not sure. Never \
invent a link outside 0 .. N-2 and never force a tag.

Respond with ONLY a JSON object, no other text:
{"tags": [{"item": <item number>, "link": <integer or null>}, ...]}
Include every item number exactly once."""


def _render(steps: list[str], items: list[dict]) -> str:
    lines = ["Chain steps:"]
    lines += [f"  {i}: {s}" for i, s in enumerate(steps)]
    lines.append(f"(links 0..{len(steps) - 2})")
    lines.append("\nEvidence items:")
    for i, it in enumerate(items):
        value = (it.get("value") or "")[:160]
        lines.append(f"  {i}: {it.get('label', '')} — {value}".rstrip(" —"))
    return "\n".join(lines)


def parse_tags(text: str, n_items: int, link_count: int) -> list[int | None]:
    """One entry per item; anything missing, malformed or out of range is None."""
    out: list[int | None] = [None] * n_items
    match = _JSON_RE.search(text or "")
    if not match:
        return out
    try:
        parsed = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError:
        return out
    for entry in parsed.get("tags", []) if isinstance(parsed, dict) else []:
        if not isinstance(entry, dict):
            continue
        item, link = entry.get("item"), entry.get("link")
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < n_items:
            continue
        if isinstance(link, bool) or not isinstance(link, int) or not 0 <= link < link_count:
            continue
        out[item] = link
    return out


def tag_links(chain_steps: list[str], items: list[dict], *, model_chain: list[str] | None = None) -> tuple[list[int | None], RouteResult | None]:
    """Returns (tags aligned to `items`, the RouteResult for cost/model logging).
    Fewer than two steps or no items: nothing to tag. Every model unavailable: all None."""
    steps = [s for s in chain_steps if isinstance(s, str) and s.strip()]
    if len(steps) < 2 or not items:
        return [None] * len(items), None
    try:
        result = route_explicit_chain(
            system=SYSTEM_PROMPT, user_message=_render(steps, items), hardness=fixed(Tier.QUICK, "causal link tagging"),
            model_chain=model_chain or JEV_CLASSIFIER_MODEL_CHAIN, max_tokens=_MAX_TOKENS,
        )
    except AllProvidersUnavailableError:
        logger.warning("Link tagger: every configured model unavailable; leaving items untagged")
        return [None] * len(items), None
    return parse_tags(result.response.text or "", len(items), len(steps) - 1), result
