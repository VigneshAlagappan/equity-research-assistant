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


class ProviderUnavailable(Exception):
    """Raised by a provider's generate() when it cannot serve this request
    right now — quota exhausted, rate-limited, outage, auth failure, or (for
    a local provider) the runtime isn't reachable. llm/router.py catches this
    and falls back to the next candidate model. Never raised for a refusal or
    an empty response — those are valid outcomes carried in ProviderResponse."""


class Provider(Protocol):
    def __call__(self, *, system: str, user_message: str, model: str, max_tokens: int) -> ProviderResponse: ...
