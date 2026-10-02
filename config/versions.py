"""Version stamps for L5 causal investigations (docs/L5_MVP_TASK_PLAN.md, M1).

Every persisted investigation, metrics row, feedback row and golden eval run
records WHICH engine produced it, so two runs can be compared and a change in
quality can be traced to a change in code, prompts or settings rather than
guessed at.

  ENGINE_VERSION               hand-bumped when the investigation pipeline's
                               behaviour changes in a way that should break
                               trend comparisons (new loop, new gating, ...)
  METRICS_DEFINITION_VERSION   hand-bumped when research/investigation_metrics
                               changes what a metric means -- history is never
                               rewritten, a bump starts a new series
  prompt_version()             derived: hash of the four L5 system prompts, so
                               any prompt edit changes it without anyone
                               remembering to bump a number
  config_hash()                derived: hash of the settings that bound an
                               investigation (loop count, timeout, model chains)

The model(s) actually used are NOT configured here -- they come from
llm_call_log per investigation.
"""

from __future__ import annotations

import hashlib
import json

ENGINE_VERSION = "l5-0.1"
METRICS_DEFINITION_VERSION = "mvp-1"
#: Placeholder until a durable graph exists (parent plan, graph_version).
GRAPH_VERSION = "mvp-0"


def _short_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def prompt_version() -> str:
    """Hash of the generator / evaluator / synthesis system prompts. Imported
    lazily: these modules import heavy dependencies and this module must stay
    importable from config/ without a cycle."""
    from research.hypothesis_evaluator import HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT
    from research.hypothesis_generator import HYPOTHESIS_GENERATOR_SYSTEM_PROMPT
    from research.research_synthesis import RESEARCH_SYNTHESIS_SYSTEM_PROMPT

    return _short_hash(
        "\x1f".join((
            HYPOTHESIS_GENERATOR_SYSTEM_PROMPT, HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT, RESEARCH_SYNTHESIS_SYSTEM_PROMPT,
        ))
    )


def config_hash() -> str:
    """Hash of the settings that bound an investigation."""
    from config import settings
    from research import investigation

    return _short_hash(json.dumps({
        "max_evidence_iterations": investigation.MAX_EVIDENCE_ITERATIONS,
        "timeout_seconds": investigation.INVESTIGATION_TIMEOUT_SECONDS,
        "level_model_chain": {str(k): v for k, v in sorted(settings.LEVEL_MODEL_CHAIN.items())},
    }, sort_keys=True))


def version_stamp() -> dict[str, str]:
    """Everything stamped onto one investigation row."""
    return {
        "engine_version": ENGINE_VERSION,
        "prompt_version": prompt_version(),
        "config_hash": config_hash(),
        "metrics_definition_version": METRICS_DEFINITION_VERSION,
    }
