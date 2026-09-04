"""llm/providers/* — each provider's generate() in isolation. No real network
access: the Anthropic SDK client and `requests` are both monkeypatched."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest
import requests

from llm.providers import anthropic_provider, local_provider
from llm.providers.base import ProviderUnavailable


class _FakeMessages:
    def __init__(self, content, stop_reason, usage=None, captured=None):
        self._content = content
        self._stop_reason = stop_reason
        self._usage = usage
        self._captured = captured

    def create(self, **kwargs):
        if self._captured is not None:
            self._captured.append(kwargs)
        return SimpleNamespace(content=self._content, stop_reason=self._stop_reason, usage=self._usage)


class _FakeClient:
    def __init__(self, content, stop_reason, usage=None, captured=None):
        self.messages = _FakeMessages(content, stop_reason, usage, captured)


def test_anthropic_provider_returns_text_and_usage(monkeypatch) -> None:
    usage = SimpleNamespace(input_tokens=42, output_tokens=7)
    content = [SimpleNamespace(type="text", text="hello")]
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(content, "end_turn", usage),
    )

    response = anthropic_provider.generate(system="s", user_message="u", model="claude-sonnet-5", max_tokens=100)

    assert response.text == "hello"
    assert response.input_tokens == 42
    assert response.output_tokens == 7
    assert response.provider == "anthropic"


def test_anthropic_provider_sends_a_plain_string_without_cacheable_prefix(monkeypatch) -> None:
    """No cacheable_prefix -> the exact same single-string message shape
    this always sent, not a needless one-element content-block list."""
    captured: list = []
    content = [SimpleNamespace(type="text", text="hello")]
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(content, "end_turn", captured=captured),
    )

    anthropic_provider.generate(system="s", user_message="the question", model="claude-sonnet-5", max_tokens=100)

    assert captured[0]["messages"][0]["content"] == "the question"


def test_anthropic_provider_marks_the_cacheable_prefix_with_cache_control(monkeypatch) -> None:
    captured: list = []
    content = [SimpleNamespace(type="text", text="hello")]
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(content, "end_turn", captured=captured),
    )

    anthropic_provider.generate(
        system="s", user_message="the question", model="claude-sonnet-5", max_tokens=100,
        cacheable_prefix="the stable evidence block",
    )

    sent = captured[0]["messages"][0]["content"]
    assert sent == [
        {"type": "text", "text": "the stable evidence block", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "the question"},
    ]


def test_anthropic_provider_surfaces_cache_token_usage(monkeypatch) -> None:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=800, cache_read_input_tokens=0)
    content = [SimpleNamespace(type="text", text="hello")]
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(content, "end_turn", usage),
    )

    response = anthropic_provider.generate(
        system="s", user_message="u", model="claude-sonnet-5", max_tokens=100, cacheable_prefix="evidence",
    )

    assert response.cache_creation_input_tokens == 800
    assert response.cache_read_input_tokens == 0


def test_anthropic_provider_raises_provider_unavailable_on_rate_limit(monkeypatch) -> None:
    def _raise(*a, **kw):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request)
        raise anthropic.RateLimitError("rate limited", response=response, body=None)

    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: SimpleNamespace(messages=SimpleNamespace(create=_raise)),
    )

    with pytest.raises(ProviderUnavailable):
        anthropic_provider.generate(system="s", user_message="u", model="claude-sonnet-5", max_tokens=100)


def test_anthropic_provider_raises_provider_unavailable_when_no_api_key_configured(monkeypatch) -> None:
    """No ANTHROPIC_API_KEY at all: the SDK never gets far enough to make a
    request — it raises a bare TypeError from header-building, not
    anthropic.APIError. That must still convert to ProviderUnavailable so
    llm/router.py falls back to the next model instead of crashing."""
    def _raise(*a, **kw):
        raise TypeError(
            "Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set."
        )

    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: SimpleNamespace(messages=SimpleNamespace(create=_raise)),
    )

    with pytest.raises(ProviderUnavailable):
        anthropic_provider.generate(system="s", user_message="u", model="claude-sonnet-5", max_tokens=100)


def test_local_provider_returns_text_and_usage(monkeypatch) -> None:
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "hi"}, "done": True, "prompt_eval_count": 12, "eval_count": 3}

    monkeypatch.setattr("llm.providers.local_provider.requests.post", lambda *a, **kw: _FakeResponse())

    response = local_provider.generate(system="s", user_message="u", model="llama3.1:8b", max_tokens=100)

    assert response.text == "hi"
    assert response.input_tokens == 12
    assert response.output_tokens == 3
    assert response.provider == "ollama"


def test_local_provider_folds_cacheable_prefix_into_the_plain_user_message(monkeypatch) -> None:
    """Ollama has no server-side prompt caching to opt into (module
    docstring) — cacheable_prefix is accepted for Provider Protocol
    conformance and folded back into one plain user turn, not dropped."""
    captured: list = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "hi"}, "done": True, "prompt_eval_count": 12, "eval_count": 3}

    def fake_post(url, json, timeout):
        captured.append(json)
        return _FakeResponse()

    monkeypatch.setattr("llm.providers.local_provider.requests.post", fake_post)

    local_provider.generate(
        system="s", user_message="the question", model="llama3.1:8b", max_tokens=100,
        cacheable_prefix="the stable evidence block",
    )

    sent_user_message = captured[0]["messages"][1]["content"]
    assert sent_user_message == "the stable evidence block\n\nthe question"


def test_local_provider_raises_provider_unavailable_when_unreachable(monkeypatch) -> None:
    def _raise(*a, **kw):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("llm.providers.local_provider.requests.post", _raise)

    with pytest.raises(ProviderUnavailable):
        local_provider.generate(system="s", user_message="u", model="llama3.1:8b", max_tokens=100)
