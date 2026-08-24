"""Static model metadata. Routing decisions (llm/router.py) key off these
fields instead of scattered `if model == "claude-opus-5"` checks through
research/*.py.

reasoning_strength is a hand-set 1-5 scale used only to gate fallback
(llm/router.py won't offer a candidate whose reasoning_strength is below what
the current task's hardness tier requires) — it's a rough ordering, not a
benchmark score. It exists so a hard question never silently lands on a model
too weak to do it justice even after every stronger model has failed
(graceful degradation, not blind failover).
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import DISABLED_MODELS, LOCAL_MODEL_ENABLED, LOCAL_MODEL_ID


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    provider: str  # key into llm.router._PROVIDERS
    local: bool
    context_window: int
    reasoning_strength: int  # 1 (weakest) .. 5 (strongest)
    cost_class: str  # "free" | "low" | "medium" | "high"
    speed_class: str  # "fast" | "medium" | "slow"
    enabled: bool = True


# config.settings.DISABLED_MODELS drives `enabled` below — a model listed
# there is filtered out of enabled_models() (below), and llm/router.py's
# fallback chain is built only from enabled_models(), so a disabled model
# never gets offered: not as a tier's preferred model, not as a fallback
# candidate, and not even via an explicit pinned_model= — _fallback_chain
# checks .enabled on the pinned path too. Kept in this list rather than
# deleted so its metadata is still available for cost-estimation of any
# already-logged llm_call_log rows (llm/observability.py's pricing table
# still prices a disabled model).
MODELS: list[ModelSpec] = [
    ModelSpec(
        "claude-opus-5", provider="anthropic", local=False, context_window=200_000,
        reasoning_strength=5, cost_class="high", speed_class="slow",
        enabled="claude-opus-5" not in DISABLED_MODELS,
    ),
    ModelSpec(
        "claude-sonnet-5", provider="anthropic", local=False, context_window=200_000,
        reasoning_strength=4, cost_class="medium", speed_class="medium",
        enabled="claude-sonnet-5" not in DISABLED_MODELS,
    ),
    ModelSpec(
        "claude-haiku-4-5", provider="anthropic", local=False, context_window=200_000,
        reasoning_strength=2, cost_class="low", speed_class="fast",
        enabled="claude-haiku-4-5" not in DISABLED_MODELS,
    ),
    # Last-resort fallback — see config.settings.LOCAL_MODEL_ENABLED/LOCAL_MODEL_ID
    # to turn it off or point it at a different Ollama model without touching code.
    ModelSpec(
        LOCAL_MODEL_ID, provider="ollama", local=True, context_window=128_000,
        reasoning_strength=2, cost_class="free", speed_class="medium",
        enabled=LOCAL_MODEL_ENABLED and LOCAL_MODEL_ID not in DISABLED_MODELS,
    ),
]


def get_model(model_id: str) -> ModelSpec | None:
    return next((m for m in MODELS if m.model_id == model_id), None)


def enabled_models() -> list[ModelSpec]:
    return [m for m in MODELS if m.enabled]
