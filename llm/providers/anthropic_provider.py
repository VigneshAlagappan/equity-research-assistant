"""Talks to Anthropic's API — the exact request/response shape
research/assistant.py, research/insights.py and research/signals_report.py
used to build inline before this refactor. The only behavioral addition is
turning any operational failure (rate limit, quota, outage, auth) into
ProviderUnavailable so llm/router.py can fall back instead of the request
crashing.
"""

from __future__ import annotations

import anthropic

from llm.providers.base import ProviderResponse, ProviderUnavailable

PROVIDER_NAME = "anthropic"


def generate(*, system: str, user_message: str, model: str, max_tokens: int) -> ProviderResponse:
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
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
    )
