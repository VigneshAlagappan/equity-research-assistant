"""Paths, source trust order, and LLM configuration.

No source adapters exist yet (see README: Implementation Sequence, step 1).
This module only defines where things live and how they're prioritized.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
NORMALIZED_DIR = DATA_DIR / "normalized"
DOCUMENTS_DIR = DATA_DIR / "documents"
CHARTS_DIR = DATA_DIR / "charts"
LOG_DIR = BASE_DIR / "logs"

SCHEMA_PATH = BASE_DIR / "schemas" / "sqlite_schema.sql"
DB_PATH = DATA_DIR / "equity_research.db"

# Daily OHLCV price history lives in its own db file, deliberately separate
# from DB_PATH: cheaply regenerable from yfinance at any time (unlike
# DB_PATH's LLM-extracted knowledge graph), gitignored via the existing
# blanket "*.db" rule, and never git-shard-committed (scripts/db_shard.py
# stays equity_research.db-only) -- regenerate on a fresh clone via
# scripts/backfill_price_history.py instead.
PRICE_SCHEMA_PATH = BASE_DIR / "schemas" / "price_schema.sql"
PRICE_DB_PATH = DATA_DIR / "price_history.db"

# ------------------------------------------------------------------
# Source trust order (default reconciliation priority)
#
# For STRUCTURED FINANCIAL FACTS specifically, NSE XBRL is now the target
# source of truth (trust_rank 0, ahead of everything else) — once a
# reporting period has a validated NSE observation on file, storage/
# repositories.py's reconcile() both prefers it over every other source for
# metrics NSE reported AND refuses to backfill metrics NSE didn't report
# for that same period from legacy data (blank instead of mixed). This
# supersedes the older "official company filing -> NSE/BSE filing ->
# licensed data provider -> secondary financial source" default order
# below investor_relations/proprietary — "proprietary" is a hand-curated
# spreadsheet of numbers, i.e. exactly the "Existing Manual/Legacy data"
# this policy demotes, not a genuine official filing; investor_relations
# itself feeds no financial_observations today (no adapter uses that
# source_id — it's narrative documents only), so this reordering has no
# other practical effect yet.
#
# NSE and BSE sit at the same trust_rank: filings submitted to both
# exchanges are often identical, so they're a confirming cross-check
# rather than competing canonical candidates (README: Open Decisions).
# ------------------------------------------------------------------

DEFAULT_SOURCES: list[dict[str, object]] = [
    {
        "source_id": "investor_relations",
        "name": "Investor Relations (annual reports, presentations, transcripts)",
        "trust_rank": 1,
        "description": "Official company filing — highest trust.",
    },
    {
        "source_id": "nse",
        "name": "National Stock Exchange",
        "trust_rank": 0,
        "description": (
            "Exchange XBRL filing — target source of truth for structured financial facts, "
            "ahead of every other source once a period is validated. Tied with BSE — see Open Decisions."
        ),
    },
    {
        "source_id": "bse",
        "name": "Bombay Stock Exchange",
        "trust_rank": 0,
        "description": "Exchange filing. Tied with NSE — see Open Decisions.",
    },
    {
        "source_id": "screener",
        "name": "Screener.in",
        "trust_rank": 3,
        "description": "Licensed/secondary data provider export.",
    },
    {
        "source_id": "proprietary",
        "name": "Proprietary (hand-prepared/verified workbook)",
        "trust_rank": 1,
        "description": "Own curated numbers, same trust tier as an official filing — tied with Investor Relations rather than ranked below it. Ranked below NSE XBRL (trust_rank 0) once a period is validated there.",
    },
    {
        "source_id": "yfinance",
        "name": "Yahoo Finance (via yfinance)",
        "trust_rank": 3,
        "description": "Secondary data provider API — same trust tier as Screener. Non-Indian companies only; live-fetched, not an uploaded file.",
    },
    # Macro/regulatory sources — each its own row (trust-rankable and
    # describable individually) rather than one generic "macro" placeholder,
    # the same way NSE/BSE/Screener each get their own row above rather than
    # sharing a generic "company data" one. Not reconciled against company
    # financials (trust_rank=None) — there's no cross-source conflict to
    # reconcile yet, since each series has exactly one provider today.
    {
        "source_id": "rbi",
        "name": "Reserve Bank of India",
        "trust_rank": None,
        "description": "Repo rate, monetary policy, banking-sector regulatory data.",
    },
    {
        "source_id": "imd",
        "name": "India Meteorological Department",
        "trust_rank": None,
        "description": "Rainfall data.",
    },
    {
        "source_id": "iitm",
        "name": "Indian Institute of Tropical Meteorology",
        "trust_rank": None,
        "description": (
            "Long-period regional and subdivisional rainfall series (Parthasarathy-style, "
            "1813/1871 onward). Same monsoon/rainfall domain as IMD, published separately by "
            "IITM Pune — kept as its own source row rather than folded into 'imd' since it's a "
            "distinct publisher with its own regionalization and parser (sources/iitm_rainfall.py)."
        ),
    },
    {
        "source_id": "mospi",
        "name": "Ministry of Statistics and Programme Implementation",
        "trust_rank": None,
        "description": "GDP, inflation, and other national statistics.",
    },
    {
        "source_id": "irda",
        "name": "Insurance Regulatory and Development Authority",
        "trust_rank": None,
        "description": "Insurance-sector regulatory data. Unused until an insurer is in scope.",
    },
    {
        "source_id": "mfin",
        "name": "Microfinance Institutions Network",
        "trust_rank": None,
        "description": (
            "Microfinance-sector industry body — best-practice guidance, sector studies, "
            "MicroScape bulletins. Reference PDFs archived under data/raw/_macro/mfin/, not "
            "numeric time-series — sources/macro.py's MacroDataAdapter (period,value,unit CSV) "
            "doesn't apply to this source, so it produces no macro_observations rows."
        ),
    },
    {
        "source_id": "fred",
        "name": "Federal Reserve Economic Data (FRED)",
        "trust_rank": None,
        "description": (
            "US macro/regulatory data — the Fed funds rate, Treasury yields, CPI, unemployment, "
            "GDP, and other economy-wide indicators. The US counterpart to rbi/imd/iitm/mospi "
            "above; live-fetched per series (sources/fred.py), not an uploaded file."
        ),
    },
]

# ------------------------------------------------------------------
# Default index-tag vocabulary (storage/database.py's _seed_index_definitions
# seeds the index_definitions table with this list on first run; after that,
# the table itself — editable via the Admin "Sectors, Industries & Tags"
# panel — is the source of truth, not this constant). Same pattern as
# DEFAULT_SOURCES above: seed data, not runtime-read configuration.
# ------------------------------------------------------------------

INDEX_NAMES: list[str] = [
    "BSE 100",
    "BSE 100 ESG Index (INR)",
    "BSE 1000",
    "BSE 150 MidCap Index",
    "BSE 200",
    "BSE 200 Equal Weight",
    "BSE 250 LargeMidCap Index",
    "BSE 400 MidSmallCap Index",
    "BSE 500",
    "BSE BANKEX",
    "BSE Dollex 100",
    "BSE Dollex 200",
    "BSE Financial Services",
    "BSE Focused Midcap",
    "BSE India 150",
    "BSE MIDSML PB QLT TLT",
    "BSE MidCap Select Index",
    "BSE Midsmall Private Banks",
    "BSE Private Banks Index",
    "BSE SENSEX Next 50",
    "Nifty 50",
    "Nifty 100",
    "Nifty 200",
    "Nifty 500",
    "Nifty 500 Multicap 50:25:25",
    "Nifty Bank",
    "Nifty Financial Services",
    "Nifty India FPI 150",
    "Nifty LargeMidcap 250",
    "Nifty Midcap 50",
    "Nifty Midcap 100",
    "Nifty Midcap 150",
    "Nifty Midcap Liquid 15",
    "Nifty Midcap Select",
    "Nifty MidSmall Financial Services",
    "Nifty MidSmallcap 400",
    "Nifty MidSmallcap400 50:50",
    "NIFTY Housing",
    "Nifty Next 50",
    "Nifty Private Bank",
    "Nifty Smallcap 50",
    "Nifty Smallcap 100",
    "Nifty Smallcap 250",
    "Nifty Total Market",
    "Nifty500 Equal Weight",
    "Nifty500 LargeMidSmall Equal-Cap Weighted",
    "Sensex",
    # US indices — for non-Indian (country="US") companies, see
    # companies.registry.register_company's country/currency support.
    "S&P 500",
    "Nasdaq 100",
    "Dow Jones Industrial Average",
]

# ------------------------------------------------------------------
# LLM configuration
#
# The research assistant (README: Deterministic Calculation Layer,
# Research Assistant) is a later phase — these are just the settings
# it will read once it exists. No API calls happen in this module.
# ------------------------------------------------------------------

# Unset by default (None) so research/assistant.py's per-question auto-routing
# (_select_model) picks the model tier itself. Set this env var only to pin
# every LLM call to one fixed model, bypassing auto-routing everywhere —
# research/insights.py and research/signals_report.py, which don't auto-route,
# fall back to DEFAULT_ANTHROPIC_MODEL when this is unset.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL")
# Sonnet, not Opus: operator cost-control policy (llm/capability_registry.py
# disables Opus entirely) — this is what research/insights.py and
# research/signals_report.py pin to when ANTHROPIC_MODEL is unset.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
ANTHROPIC_API_KEY_SET = bool(os.environ.get("ANTHROPIC_API_KEY"))

# ------------------------------------------------------------------
# Model tiering policy — llm/hardness.py's classify() sorts each question
# into a Tier (quick/standard/deep); llm/router.py and
# llm/capability_registry.py read the three settings below to decide which
# model actually runs it. Edit these directly to change routing — no env
# var, no runtime toggle, just this file (and a restart).
# ------------------------------------------------------------------

# Models that must never be used, at any tier, for any call — not as a
# tier's preferred model, not as a fallback candidate, and not even via an
# explicit model= argument or the ANTHROPIC_MODEL pin above.
# llm/capability_registry.py marks each matching ModelSpec disabled.
DISABLED_MODELS: set[str] = {"claude-opus-5"}

# Tier -> which model is tried first for that class of question:
#   quick     short factual lookups, little evidence
#   standard  everything else (the default)
#   deep      peer comparisons, "why"/causal reasoning, or 40+ evidence lines
# llm/router.py's fallback chain starts here, then falls through other
# enabled models (strongest reasoning_strength first) if this one fails.
#TIER_PREFERRED_MODEL: dict[str, str] = {
#    "quick": "claude-haiku-4-5",
#    "standard": "claude-sonnet-5",
#    "deep": "claude-sonnet-5",
#}

TIER_PREFERRED_MODEL: dict[str, str] = {
    "quick": "claude-haiku-4-5",
    "standard": "claude-haiku-4-5",
    "deep": "claude-haiku-4-5",
}

# Tier -> minimum ModelSpec.reasoning_strength (llm/capability_registry.py,
# 1-5 scale) a model must have to be offered this tier's work at all, even
# as a last-resort fallback — keeps a hard question from silently landing
# on a model too weak for it. To let a weaker model (e.g. Haiku,
# reasoning_strength=2) handle "deep" questions too, lower its entry here
# AND change TIER_PREFERRED_MODEL["deep"] above — changing only one of the
# two leaves Haiku still excluded as a fallback candidate, or still
# preferred-but-then-immediately-rejected.
TIER_MIN_REASONING_STRENGTH: dict[str, int] = {
    "quick": 1,
    "standard": 2,
    "deep": 4,
}

# ------------------------------------------------------------------
# Local model fallback (llm/providers/local_provider.py, llm/capability_registry.py)
#
# A locally running Ollama server is the last resort in the fallback chain
# llm/router.py builds — tried only once every configured Anthropic model has
# failed. Not started/stopped by this app (README §20, local-first
# experimentation: start it yourself when you want the fallback available).
# LOCAL_MODEL_ENABLED lets it be turned off entirely (e.g. no Ollama
# installed) without touching code.
# ------------------------------------------------------------------

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
LOCAL_MODEL_ENABLED = os.environ.get("LOCAL_MODEL_ENABLED", "true").lower() != "false"
LOCAL_MODEL_ID = os.environ.get("LOCAL_MODEL_ID", "llama3.1:8b")

# research/knowledge_builder.py truncates a document's text to this many
# characters before sending it to the model — env-configurable because
# local-model inference speed is roughly linear in input length, and a slow
# local model benefits from a smaller cap far more than a cloud model does
# (Anthropic handles the full 40k comfortably). Lower this per-run (not the
# default) when running a local-model bulk ingestion and speed matters more
# than the extra document coverage the last ~20k characters would add.
KNOWLEDGE_EXTRACTION_MAX_CHARS = int(os.environ.get("KNOWLEDGE_EXTRACTION_MAX_CHARS", "40000"))

# ------------------------------------------------------------------
# Knowledge graph backend (context/graph.py, context/graph_neo4j.py)
#
# GRAPH_BACKEND="sqlite" (default) is context/graph.py's own pure-Python
# traversal over data already in SQLite — no extra service required.
# GRAPH_BACKEND="neo4j" opts into the Neo4j-backed traversal instead; a
# local Neo4j server isn't started/stopped by this app (README §20,
# local-first: start it yourself, same as Ollama) — see
# context/graph_neo4j.py's module docstring for the docker run command.
# If GRAPH_BACKEND=neo4j but the server isn't reachable, context/graph.py
# falls back to the sqlite traversal rather than failing the request.
# ------------------------------------------------------------------

GRAPH_BACKEND = os.environ.get("GRAPH_BACKEND", "sqlite")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

# ------------------------------------------------------------------
# Web session secret (signs the login cookie — web/app.py)
#
# Self-use local app (README: no deployment target yet), so there's no
# secrets store to read from — SECRET_KEY env var wins if set, otherwise a
# random key is generated once and persisted to a gitignored file so
# sessions survive process restarts instead of logging everyone out.
# ------------------------------------------------------------------

_SECRET_KEY_PATH = DATA_DIR / ".flask_secret_key"


def _load_or_create_secret_key() -> str:
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    if _SECRET_KEY_PATH.exists():
        return _SECRET_KEY_PATH.read_text().strip()
    import secrets

    key = secrets.token_hex(32)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SECRET_KEY_PATH.write_text(key)
    return key


SECRET_KEY = _load_or_create_secret_key()

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger: console + rotating file handler under LOG_DIR."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=3
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [console_handler, file_handler]


def ensure_data_dirs() -> None:
    """Create the raw/normalized/documents/charts/logs directories if missing."""
    for path in (RAW_DIR, NORMALIZED_DIR, DOCUMENTS_DIR, CHARTS_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------
# Repo-relative path storage — any path persisted to the database (an
# uploaded document, a note attachment, a discovered ingestion-queue file)
# must be stored relative to BASE_DIR, not as an absolute path. An absolute
# path bakes in the repo folder's current name/location; renaming or moving
# the repo (as this one already has, "indian-equity-research-assistant" ->
# "equity-research-assistant") silently breaks every previously-stored
# absolute reference, since BASE_DIR itself is derived fresh from
# Path(__file__) on every process start (see BASE_DIR above) and no longer
# matches what was baked into the database.
# ------------------------------------------------------------------


def to_repo_relative(path: Path | str) -> str:
    """Convert an absolute (or already-relative) path into one relative to
    BASE_DIR, for storage. Falls back to the absolute string if the path
    genuinely isn't under BASE_DIR (shouldn't normally happen for anything
    this app itself writes, but never raises over it)."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(BASE_DIR))
    except ValueError:
        return str(resolved)


def from_repo_relative(path: str) -> Path:
    """Inverse of to_repo_relative() — resolve a stored path against the
    CURRENT BASE_DIR. A path that's already absolute (an old, pre-fix row,
    or the to_repo_relative() fallback above) is returned as-is."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else BASE_DIR / candidate
