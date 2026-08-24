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
