"""Macro Knowledge Builder -- Step 2A's document-extraction counterpart for
macro/regulatory series (research/macro_evidence.py's data): classifies each
macro series (by series_key, e.g. "repo_rate", "rainfall_kerala") against
the real sector/industry taxonomy (storage.repositories.list_sectors/
list_industries) into MacroFactor entities and MAY_AFFECT/DRIVES/EXPOSED_TO
relationships (config/knowledge_ontology.py -- both were already part of the
ontology, just never populated by anything until this module). Once
persisted into knowledge_entities/knowledge_relationships, these reach
Neo4j automatically the same way document-derived claims already do --
context/graph_neo4j.py's sync_knowledge_graph() resyncs the whole graph
lazily, unaware of (and not needing to know) which module wrote a row.

Unlike research/knowledge_builder.py, there's no source text to extract
from -- a macro series is a numeric time series, not a narrative document.
The LLM call here classifies by series_key alone (general domain reasoning:
"does the repo rate plausibly affect Banking companies"), not grounded
evidence from a specific document -- knowledge_relationships already
supports this (claim_id is nullable: "the claim this relationship was
asserted in, if any"), so these rows are created with claim_id=None and no
knowledge_evidence row, honestly reflecting that provenance rather than
inventing a fake claim/quote to satisfy the document-extraction shape.

Idempotent by construction, not by an explicit dedup check: only series
with no existing MacroFactor entity are ever sent to the LLM (a completed
classification is permanent -- whether the repo rate affects Banking
doesn't change day to day the way a document's claims accumulate), so a
scheduled rerun only ever classifies genuinely NEW series (e.g. mospi/irda
once ingested) and costs nothing on a day with no new series.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from storage.db_types import DBConnection

from config.knowledge_ontology import RELATIONSHIP_TYPES
from config.settings import ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from storage.repositories import (
    get_or_create_knowledge_entity,
    insert_knowledge_relationship,
    list_all_knowledge_entities,
    list_industries,
    list_macro_series_summary,
    list_sectors,
)

logger = logging.getLogger(__name__)

#: Kept well under any provider's per-call output limit even at 4
#: relationships/series (MAX_TOKENS below) -- mirrors research/
#: knowledge_builder.py's MAX_CLAIMS_PER_DOCUMENT reasoning for why a cap
#: exists at all (a big flat batch risks silent mid-JSON truncation).
MAX_SERIES_PER_BATCH = 40
MAX_RELATIONSHIPS_PER_SERIES = 4
MAX_TOKENS = 4096

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class MacroClassificationError(Exception):
    """Raised when one batch's classification call fails outright (LLM
    unavailable, unparseable response) -- classify_macro_factors() logs and
    moves on to the next batch rather than letting one bad batch abort the
    whole job, same graceful-degradation spirit as ingestion/coordinator.py's
    chunk_indexer being independent of knowledge_builder."""


@dataclass
class MacroClassificationResult:
    series_classified: int = 0
    factors_created: int = 0
    relationships_created: int = 0
    batches_failed: int = 0


def _pretty_label(series_key: str) -> str:
    """Same rendering as research/macro_evidence.py's own _pretty_label
    (kept as a separate copy rather than importing that module's private
    helper across module boundaries) -- MacroFactor entity names must match
    it exactly, since that's how classify_macro_factors() recognizes a
    series as already classified on a rerun."""
    label = series_key.replace("_", " ").title()
    return re.sub(r"\s+Annual$", "", label)


MACRO_CLASSIFIER_SYSTEM_PROMPT = """You classify macro/regulatory data series by which industries they plausibly \
affect, for a research knowledge graph. You are given a list of series names and a fixed list of valid industry \
names -- for each series, name zero or more industries it plausibly affects, using ONLY industry names from the \
given list (never invent one), and the relationship type that best describes the connection.

Relationship types:
- DRIVES: the series is a direct input/driver of that industry's core economics (e.g. a policy rate driving bank margins)
- MAY_AFFECT: a plausible but less direct or occasional effect
- EXPOSED_TO: the industry's results are sensitive/vulnerable to this series' movements, without the series driving it

Respond with ONLY a JSON object, no other text, in exactly this shape:

{{
  "factors": [
    {{
      "series_key": "<exactly one series_key from the list given>",
      "relationships": [
        {{"relationship_type": "<one of: {relationship_types}>", "target_industry": "<exactly one name from the given industry list>"}}
      ]
    }}
  ]
}}

Rules:
- Omit a series entirely from "factors" (or give it an empty relationships list) if it has no plausible \
industry-level effect -- most narrow regional/administrative series (a specific district's rainfall subtype, a \
niche accounting table) have none. Don't force a connection that isn't real.
- At most {max_relationships} relationships per series -- the clearest, most material ones only.
- Every target_industry must be copied EXACTLY from the given industry list -- never invent, abbreviate, or rename one."""


def _build_system_prompt() -> str:
    return MACRO_CLASSIFIER_SYSTEM_PROMPT.format(
        relationship_types=", ".join(sorted(RELATIONSHIP_TYPES)), max_relationships=MAX_RELATIONSHIPS_PER_SERIES,
    )


def _parse_response(text: str) -> dict:
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        raise MacroClassificationError(f"model response contained no JSON object: {text[:200]!r}")
    try:
        # strict=False tolerates literal control characters -- see
        # research/knowledge_builder.py's _parse_response for why.
        parsed = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError as exc:
        raise MacroClassificationError(f"model response wasn't valid JSON: {exc}") from None
    if not isinstance(parsed, dict) or "factors" not in parsed:
        raise MacroClassificationError(f"model response missing a top-level 'factors' list: {text[:200]!r}")
    return parsed


def _classify_batch(
    conn: DBConnection, series_keys: list[str], valid_industries: set[str], *, model: str | None,
) -> MacroClassificationResult:
    result = MacroClassificationResult()
    catalog = "\n".join(f"- {key}" for key in series_keys)
    industry_list = "\n".join(f"- {name}" for name in sorted(valid_industries))
    user_message = f"Series:\n{catalog}\n\nValid industry names:\n{industry_list}"

    hardness = fixed(Tier.STANDARD, "macro factor classification")
    pinned_model = model or ANTHROPIC_MODEL

    attempt_message = user_message
    parsed: dict | None = None
    parse_error: MacroClassificationError | None = None
    for _attempt in range(2):
        try:
            llm_result = route(
                system=_build_system_prompt(), user_message=attempt_message, hardness=hardness,
                max_tokens=MAX_TOKENS, pinned_model=pinned_model,
            )
        except AllProvidersUnavailableError as exc:
            raise MacroClassificationError(f"all configured models failed: {exc}") from exc

        observability.record(
            conn, task_name="macro_factor_classification", company_ids=[], question=None, result=llm_result,
        )

        response = llm_result.response
        if response.stop_reason == "refusal" or not response.text:
            raise MacroClassificationError(f"model returned no usable response (stop_reason={response.stop_reason})")
        if response.stop_reason == "max_tokens":
            raise MacroClassificationError(f"model response was truncated at the {MAX_TOKENS}-token limit")

        try:
            parsed = _parse_response(response.text)
            break
        except MacroClassificationError as exc:
            parse_error = exc
            attempt_message = (
                f"{user_message}\n\nYour previous response did not contain a single valid JSON object as "
                "instructed. Respond again with ONLY the JSON object described above -- no prose, no markdown "
                "code fences, no commentary before or after it."
            )

    if parsed is None:
        raise parse_error

    valid_series = set(series_keys)
    for raw_factor in parsed.get("factors") or []:
        series_key = raw_factor.get("series_key")
        if series_key not in valid_series:
            continue  # not trusted blindly -- must be one of the series we actually asked about

        valid_rels: list[tuple[str, str]] = []
        for raw_rel in (raw_factor.get("relationships") or [])[:MAX_RELATIONSHIPS_PER_SERIES]:
            relationship_type = raw_rel.get("relationship_type")
            target_name = raw_rel.get("target_industry")
            if relationship_type in RELATIONSHIP_TYPES and target_name in valid_industries:
                valid_rels.append((relationship_type, target_name))
        if not valid_rels:
            continue  # no relationship survived validation -- don't create an orphan MacroFactor entity for nothing

        result.series_classified += 1
        factor_entity = get_or_create_knowledge_entity(conn, "MacroFactor", _pretty_label(series_key), None)
        result.factors_created += 1

        for relationship_type, target_name in valid_rels:
            industry_entity = get_or_create_knowledge_entity(conn, "Industry", target_name, None)
            insert_knowledge_relationship(
                conn, claim_id=None, source_entity_id=factor_entity["entity_id"],
                relationship_type=relationship_type, target_entity_id=industry_entity["entity_id"],
            )
            result.relationships_created += 1

    return result


def classify_macro_factors(conn: DBConnection, *, model: str | None = None) -> MacroClassificationResult:
    """Classify every macro series that doesn't already have a MacroFactor
    entity, MAX_SERIES_PER_BATCH at a time. One bad batch (LLM unavailable,
    unparseable response) is logged and skipped, not fatal to the rest."""
    valid_industries = set(list_sectors(conn)) | set(list_industries(conn))
    if not valid_industries:
        logger.warning("No sectors/industries on file -- nothing to classify macro factors against.")
        return MacroClassificationResult()

    existing_factor_names = {
        row["name"] for row in list_all_knowledge_entities(conn)
        if row["entity_type"] == "MacroFactor" and row["company_id"] is None
    }

    all_series = list_macro_series_summary(conn)
    new_series_keys = sorted({
        row["series_key"] for row in all_series
        if _pretty_label(row["series_key"]) not in existing_factor_names
    })

    result = MacroClassificationResult()
    for i in range(0, len(new_series_keys), MAX_SERIES_PER_BATCH):
        batch = new_series_keys[i : i + MAX_SERIES_PER_BATCH]
        try:
            batch_result = _classify_batch(conn, batch, valid_industries, model=model)
        except MacroClassificationError as exc:
            logger.warning("Macro factor classification batch failed (%d series): %s", len(batch), exc)
            result.batches_failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 -- a persistence-layer failure (the DB
            # connection itself died mid-run -- observed in practice on a
            # long-lived Neon-pooled connection held across many sequential
            # STANDARD-tier LLM calls) means every subsequent batch on this
            # same conn would fail identically. Stop here rather than
            # burning further LLM calls on batches guaranteed to fail at
            # the write step too -- this function is idempotent (only
            # un-classified series are ever re-sent), so simply calling it
            # again with a fresh connection resumes exactly where this run
            # stopped, no lost progress.
            logger.warning("Macro factor classification stopped early (likely connection loss): %s", exc)
            result.batches_failed += 1
            break
        result.series_classified += batch_result.series_classified
        result.factors_created += batch_result.factors_created
        result.relationships_created += batch_result.relationships_created

    return result
