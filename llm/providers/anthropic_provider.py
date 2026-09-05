"""Talks to Anthropic's API — the exact request/response shape
research/assistant.py, research/insights.py and research/signals_report.py
used to build inline before this refactor. The only behavioral addition is
turning any operational failure (rate limit, quota, outage, auth) into
ProviderUnavailable so llm/router.py can fall back instead of the request
crashing.

Prompt caching: `cacheable_prefix` (llm/providers/base.py's Provider
Protocol) is sent as its own leading content block with `cache_control`, so
the same byte-identical prefix reused across calls — a company's financials
evidence block, stable across different questions about that company, per
research/assistant.py::answer_question()'s evidence split — gets written to
Anthropic's server-side cache once and read back cheaply on every later call
within the cache's TTL, instead of being billed as full-price input tokens
every time. Caller's responsibility, not this module's: `cacheable_prefix`
must actually BE stable across calls for this to pay off — a prefix that
changes every call (or one under Anthropic's per-model minimum cacheable
size, roughly 1024-2048 tokens) just gets a wasted cache_control marker,
never a real cache write/read. Omitted entirely (None) means "no caching
requested" — the exact same single user-turn message this always sent.
"""

from __future__ import annotations

import anthropic

from llm.providers.base import ProviderResponse, ProviderUnavailable

PROVIDER_NAME = "anthropic"

# The SDK's own default (10 minutes) lets one stuck call block a whole batch
# ingestion run — observed hanging well past any reasonable single-call
# duration. Failing fast here raises APITimeoutError (an APIError subclass,
# already turned into ProviderUnavailable below) so llm/router.py's fallback
# chain — or knowledge_builder's own corrective retry — gets a turn instead.
REQUEST_TIMEOUT_SECONDS = 90.0


def generate(
    *, system: str, user_message: str, model: str, max_tokens: int, cacheable_prefix: str | None = None,
) -> ProviderResponse:
    client = anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS)
    if cacheable_prefix:
        content = [
            {"type": "text", "text": cacheable_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user_message},
        ]
    else:
        content = user_message
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as exc:
        raise ProviderUnavailable(f"anthropic:{model}: {exc}") from exc
    except TypeError as exc:
        # No ANTHROPIC_API_KEY configured at all: the SDK doesn't raise
        # anthropic.APIError for this (no request is ever attempted) — it
        # raises a bare TypeError from header-building
        # ("Could not resolve authentication method"). That's still just
        # "this provider isn't usable right now", same as a rate limit or
        # outage — must fall back like every other operational failure here,
        # not crash the whole route() call before local_provider ever gets a
        # chance to run.
        raise ProviderUnavailable(f"anthropic:{model}: {exc}") from exc

    text = "".join(block.text for block in response.content if block.type == "text")
    usage = getattr(response, "usage", None)
    return ProviderResponse(
        text=text,
        stop_reason=response.stop_reason,
        input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        model=model,
        provider=PROVIDER_NAME,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
    )
