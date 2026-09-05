"""Model Router — given a hardness tier and a rendered prompt, picks a model
and calls it, falling back through a chain when a candidate is unavailable
(rate-limited, quota exhausted, provider outage, auth problem) instead of the
request failing outright.

Fallback chain, per tier: the tier's preferred cloud model first, then other
enabled cloud models strongest-first, then the local model last — skipping
any candidate whose capability_registry.ModelSpec.reasoning_strength is below
what this tier requires (never blindly push a hard question onto a model too
weak for it) or that's disabled (e.g. local model turned off/unreachable).
A pinned model (explicit `model=` argument, or the ANTHROPIC_MODEL env var,
threaded through by the caller as `pinned_model`) is tried alone — pinning
means "always this model," not "prefer this model, then fall back."

Every attempt (success, unavailable, or skipped-too-weak) is recorded in
RouteResult.attempts for observability — llm/observability.py turns that into
a log line and an llm_call_log row.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from config.settings import TIER_PREFERRED_MODEL
from llm import capability_registry
from llm.hardness import TIER_MIN_REASONING_STRENGTH, HardnessResult, Tier
from llm.providers import anthropic_provider, local_provider
from llm.providers.base import ProviderResponse, ProviderUnavailable

# Looked up by provider name at call time (not bound into a dict at import
# time) so tests can monkeypatch e.g. llm.router.anthropic_provider.generate
# and have route() see the replacement.
_PROVIDER_MODULES = {
    anthropic_provider.PROVIDER_NAME: anthropic_provider,
    local_provider.PROVIDER_NAME: local_provider,
}

# TIER_PREFERRED_MODEL (imported above, config/settings.py): the top-of-chain
# model per tier, string-keyed ("quick"/"standard"/"deep" — routing policy
# lives in one settings file, not scattered enum-keyed dicts). DEEP prefers
# Sonnet, not Opus — Opus is disabled via config.settings.DISABLED_MODELS
# and every candidate list below is built from enabled_models(), so it would
# never be reachable here regardless.


@dataclass(frozen=True)
class Attempt:
    model: str
    provider: str
    outcome: str  # "success" | "unavailable" | "skipped_insufficient_reasoning"
    detail: str = ""


@dataclass
class RouteResult:
    response: ProviderResponse
    hardness: HardnessResult
    attempts: list[Attempt] = field(default_factory=list)
    fallback_used: bool = False
    latency_ms: float = 0.0


class AllProvidersUnavailableError(Exception):
    """Every candidate in the fallback chain for this tier either declined to
    serve the request (ProviderUnavailable) or was skipped as too weak for
    the task. The caller should show a degraded/unavailable message rather
    than let this propagate as a 500."""

    def __init__(self, attempts: list[Attempt]) -> None:
        self.attempts = attempts
        super().__init__(f"no provider could serve this request: {attempts}")


def _fallback_chain(
    tier: Tier, pinned_model: str | None
) -> tuple[list[capability_registry.ModelSpec], list[capability_registry.ModelSpec]]:
    """Returns (chain, excluded). chain is the ordered list of candidates to
    try; excluded is enabled models this tier ruled out as too weak, kept
    only so route() can log them for observability."""
    if pinned_model:
        spec = capability_registry.get_model(pinned_model)
        # A disabled model (e.g. "claude-opus-5" — see capability_registry.py)
        # can't be reached even by explicit pin: "pin to this model" doesn't
        # override the operator policy that model is turned off entirely.
        return ([spec] if spec and spec.enabled else []), []

    min_strength = TIER_MIN_REASONING_STRENGTH[tier.value]
    preferred_id = TIER_PREFERRED_MODEL[tier.value]
    enabled = capability_registry.enabled_models()
    eligible = [m for m in enabled if m.reasoning_strength >= min_strength]
    excluded = [m for m in enabled if m.reasoning_strength < min_strength]

    preferred = [m for m in eligible if m.model_id == preferred_id]
    other_cloud = sorted(
        (m for m in eligible if not m.local and m.model_id != preferred_id),
        key=lambda m: -m.reasoning_strength,
    )
    local = [m for m in eligible if m.local]
    return preferred + other_cloud + local, excluded


def route(
    *,
    system: str,
    user_message: str,
    hardness: HardnessResult,
    max_tokens: int,
    pinned_model: str | None = None,
    cacheable_prefix: str | None = None,
) -> RouteResult:
    chain, excluded = _fallback_chain(hardness.tier, pinned_model)
    attempts = [
        Attempt(
            m.model_id, m.provider, "skipped_insufficient_reasoning",
            f"reasoning_strength {m.reasoning_strength} < required {hardness.min_reasoning_strength}",
        )
        for m in excluded
    ]

    start = time.monotonic()
    for spec in chain:
        provider_fn = _PROVIDER_MODULES[spec.provider].generate
        try:
            response = provider_fn(
                system=system, user_message=user_message, model=spec.model_id, max_tokens=max_tokens,
                cacheable_prefix=cacheable_prefix,
            )
        except ProviderUnavailable as exc:
            attempts.append(Attempt(spec.model_id, spec.provider, "unavailable", str(exc)))
            continue
        attempts.append(Attempt(spec.model_id, spec.provider, "success"))
        fallback_used = sum(1 for a in attempts if a.outcome == "unavailable") > 0
        return RouteResult(
            response=response,
            hardness=hardness,
            attempts=attempts,
            fallback_used=fallback_used,
            latency_ms=(time.monotonic() - start) * 1000,
        )

    raise AllProvidersUnavailableError(attempts)
