from __future__ import annotations

from config.knowledge_ontology import (
    CANONICAL_HOME,
    CLAIM_TYPES,
    ENTITY_TYPES,
    RELATIONSHIP_TYPES,
    STRUCTURAL_NODE_TYPES,
    is_valid_claim_type,
    is_valid_entity_type,
    is_valid_relationship_type,
)


def test_entity_and_structural_node_types_are_disjoint() -> None:
    """A Claim/Evidence/Document/TimePeriod is never something the
    extraction LLM is asked to name as an entity — the two vocabularies
    must never overlap."""
    assert ENTITY_TYPES.isdisjoint(STRUCTURAL_NODE_TYPES)


def test_validators_match_the_underlying_sets() -> None:
    assert is_valid_entity_type("Company") is True
    assert is_valid_entity_type("NotAType") is False
    assert is_valid_relationship_type("MAY_AFFECT") is True
    assert is_valid_relationship_type("NOT_A_RELATIONSHIP") is False
    assert is_valid_claim_type("FACT") is True
    assert is_valid_claim_type("NOT_A_CLAIM_TYPE") is False


def test_canonical_home_covers_every_knowledge_concept() -> None:
    """Every concept the ontology's own module docstring/knowledge_builder.py
    produces has a documented, real canonical home."""
    expected_concepts = {
        "Metric historical value", "Macro/regulatory historical series",
        "Management claim", "Claim relationship/history", "Document passage (exact text)",
    }
    assert expected_concepts <= set(CANONICAL_HOME)
    assert all(isinstance(v, str) and v for v in CANONICAL_HOME.values())
