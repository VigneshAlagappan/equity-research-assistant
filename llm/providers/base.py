"""Provider abstraction — the Model Router (llm/router.py) talks to every
model through this shape, so a new provider (a different cloud vendor, a
different local runtime) only has to add a `generate()` function matching
this signature; it never touches router.py or the research/*.py call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    #: Anthropic prompt caching (llm/providers/anthropic_provider.py) —
    #: tokens written to/read from the cache on this call, both 0 for a
    #: provider/call that doesn't support or use `cacheable_prefix`. A cache
    #: read costs a fraction of a normal input token (Anthropic's pricing),
    #: so cache_read_input_tokens > 0 is the signal caching actually paid off
    #: on this call, not just that it was attempted.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ProviderUnavailable(Exception):
    """Raised by a provider's generate() when it cannot serve this request
    right now — quota exhausted, rate-limited, outage, auth failure, or (for
    a local provider) the runtime isn't reachable. llm/router.py catches this
    and falls back to the next candidate model. Never raised for a refusal or
    an empty response — those are valid outcomes carried in ProviderResponse."""


class Provider(Protocol):
    def __call__(
        self, *, system: str, user_message: str, model: str, max_tokens: int,
        cacheable_prefix: str | None = None,
    ) -> ProviderResponse: ...
