"""config/settings.py's to_repo_relative/from_repo_relative — every path
persisted to the database (uploaded documents, note attachments, the
Ingest queue) must be stored relative to BASE_DIR, not baked in as an
absolute path tied to the repo folder's current name/location."""

from __future__ import annotations

from pathlib import Path

from config.settings import BASE_DIR, to_repo_relative, from_repo_relative


def test_to_repo_relative_strips_base_dir() -> None:
    absolute = BASE_DIR / "data" / "documents" / "HDFCBANK" / "file.pdf"
    assert to_repo_relative(absolute) == "data/documents/HDFCBANK/file.pdf"


def test_from_repo_relative_resolves_against_base_dir() -> None:
    assert from_repo_relative("data/documents/HDFCBANK/file.pdf") == BASE_DIR / "data" / "documents" / "HDFCBANK" / "file.pdf"


def test_round_trip() -> None:
    absolute = BASE_DIR / "data" / "raw" / "HDFCBANK" / "screener" / "x.xlsx"
    assert from_repo_relative(to_repo_relative(absolute)) == absolute.resolve()


def test_from_repo_relative_passes_through_an_already_absolute_path() -> None:
    """An old row stored absolute (pre-fix, or a path genuinely outside the
    repo) must still resolve to something usable, not be silently rejoined
    under BASE_DIR a second time."""
    outside = Path("/tmp/some/other/place.pdf")
    assert from_repo_relative(str(outside)) == outside


def test_to_repo_relative_falls_back_for_a_path_outside_the_repo() -> None:
    outside = Path("/tmp/genuinely/outside/the/repo.pdf")
    assert to_repo_relative(outside) == str(outside.resolve())  # /tmp is a symlink on macOS
