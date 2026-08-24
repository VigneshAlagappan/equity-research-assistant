"""Talks to a locally running Ollama server — a local model participates in
routing through the exact same generate() shape as anthropic_provider.py
(llm/providers/base.py), so llm/router.py never needs to know it isn't a
cloud call. Ollama itself is not started or stopped by this code (README
§20, local-first: start it yourself when you want the fallback available);
if nothing is listening at OLLAMA_BASE_URL, generate() raises
ProviderUnavailable and the router falls back onward exactly like a cloud
outage.
"""

from __future__ import annotations

import requests

from config.settings import OLLAMA_BASE_URL
from llm.providers.base import ProviderResponse, ProviderUnavailable

PROVIDER_NAME = "ollama"
_REQUEST_TIMEOUT_SECONDS = 120


def generate(*, system: str, user_message: str, model: str, max_tokens: int) -> ProviderResponse:
    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ProviderUnavailable(f"ollama:{model}: {exc}") from exc

    payload = response.json()
    text = payload.get("message", {}).get("content", "")
    return ProviderResponse(
        text=text,
        stop_reason="end_turn" if payload.get("done") else "max_tokens",
        input_tokens=payload.get("prompt_eval_count", 0),
        output_tokens=payload.get("eval_count", 0),
        model=model,
        provider=PROVIDER_NAME,
    )
