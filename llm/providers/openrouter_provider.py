"""Talks to OpenRouter's API — an OpenAI-compatible chat-completions
endpoint fronting many hosted models (this app targets a Gemma model,
config.settings.OPENROUTER_MODEL_ID). A local model participates in
routing through the exact same generate() shape as anthropic_provider.py/
local_provider.py (llm/providers/base.py) — llm/router.py never needs to
know which cloud vendor is behind a given model.

Raw `requests` calls, not the `openai` SDK: this app has no other OpenAI-
compatible integration to justify adding that dependency, and OpenRouter's
API surface used here (one chat/completions POST, no streaming, no tool
use) is small enough that a bare HTTP call is simpler than wrapping a
whole SDK client for it.
"""

from __future__ import annotations

import os

import requests

from config.settings import OPENROUTER_API_KEY_SET
from llm.providers.base import ProviderResponse, ProviderUnavailable

PROVIDER_NAME = "openrouter"
_API_URL = "https://openrouter.ai/api/v1/chat/completions"
# Cloud call over the open internet, not a local process — same reasoning
# as anthropic_provider.py's timeout (fail fast so llm/router.py's fallback
# chain gets a turn instead of one stuck call blocking a whole batch run).
_REQUEST_TIMEOUT_SECONDS = 90.0


def generate(
    *, system: str, user_message: str, model: str, max_tokens: int, cacheable_prefix: str | None = None,
) -> ProviderResponse:
    # OpenRouter has no Anthropic-style server-side prompt caching to opt
    # into here — cacheable_prefix is accepted only to satisfy llm/
    # providers/base.py's Provider Protocol and folded into one plain user
    # turn, same shape local_provider.py already uses for the same reason.
    if cacheable_prefix:
        user_message = f"{cacheable_prefix}\n\n{user_message}"

    if not OPENROUTER_API_KEY_SET:
        # Fails the same way anthropic_provider.py's "no API key" branch
        # does: a configuration gap is an operational unavailability the
        # router should fall back past, not a crash.
        raise ProviderUnavailable(f"openrouter:{model}: OPENROUTER_API_KEY is not set")

    try:
        response = requests.post(
            _API_URL,
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
                "max_tokens": max_tokens,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Covers rate limits, quota/credit exhaustion, and outages alike —
        # OpenRouter reports all of these as ordinary HTTP error statuses
        # (401/402/429/5xx), which raise_for_status() turns into
        # HTTPError, a requests.RequestException subclass.
        raise ProviderUnavailable(f"openrouter:{model}: {exc}") from exc

    payload = response.json()
    choice = payload["choices"][0]
    usage = payload.get("usage", {})
    return ProviderResponse(
        text=choice["message"]["content"] or "",
        stop_reason=choice.get("finish_reason") or "end_turn",
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        model=model,
        provider=PROVIDER_NAME,
    )
