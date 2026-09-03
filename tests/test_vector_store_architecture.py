"""Architecture guardrail: no business/research/planner/route/ingestion code
may import a specific vector-database or embeddings SDK directly (section 1,
section 2, section 14 — "a test that greps for direct vector-SDK imports
outside the implementation module"). Only the concrete implementation
modules named below may import these third-party SDKs — every other .py
file in the repo (tests and the .venv excluded) must not.

Mirrors this repo's existing "grep the tree" style of guardrail checks
(e.g. storage/'s repository-only-touches-sqlite convention) rather than
introducing a new testing pattern."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: file path (relative to repo root) -> the SDK import pattern it alone is
#: allowed to contain.
_ALLOWED_SDK_IMPORTS = {
    "retrieval/vector_store_qdrant.py": re.compile(r"\bqdrant_client\b"),
    "retrieval/embedding_provider_local.py": re.compile(r"\bsentence_transformers\b"),
    "retrieval/embedding_provider_voyage.py": re.compile(r"\bvoyageai\b"),
}

_SDK_IMPORT_PATTERNS = {
    "qdrant_client": re.compile(r"^\s*(import|from)\s+qdrant_client\b", re.MULTILINE),
    "sentence_transformers": re.compile(r"^\s*(import|from)\s+sentence_transformers\b", re.MULTILINE),
    "voyageai": re.compile(r"^\s*(import|from)\s+voyageai\b", re.MULTILINE),
}

_EXCLUDED_DIR_PARTS = {".venv", ".git", "__pycache__", "node_modules", ".claude"}


def _all_python_files() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _EXCLUDED_DIR_PARTS for part in path.parts):
            continue
        files.append(path)
    return files


def test_no_module_outside_its_own_implementation_imports_a_vector_or_embeddings_sdk() -> None:
    violations = []
    for path in _all_python_files():
        relative = str(path.relative_to(REPO_ROOT))
        if relative.startswith("tests/"):
            continue  # test doubles (tests/conftest.py) never import real SDKs; not part of this guardrail either way
        text = path.read_text(encoding="utf-8", errors="ignore")
        for sdk_name, pattern in _SDK_IMPORT_PATTERNS.items():
            if not pattern.search(text):
                continue
            allowed_pattern = _ALLOWED_SDK_IMPORTS.get(relative)
            if allowed_pattern is not None and allowed_pattern.search(sdk_name):
                continue
            violations.append(f"{relative} imports {sdk_name} directly")

    assert not violations, "Vector/embeddings SDK imported outside its designated module:\n" + "\n".join(violations)


def test_every_designated_implementation_module_exists() -> None:
    """The inverse check — catches the guardrail test itself going stale
    (e.g. a rename) by confirming each allow-listed path is real."""
    for relative_path in _ALLOWED_SDK_IMPORTS:
        assert (REPO_ROOT / relative_path).is_file(), f"{relative_path} does not exist"
