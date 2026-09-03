-- Daily OHLCV price history for NSE 500 companies, kept in its own db file
-- (config/settings.py's PRICE_DB_PATH), separate from the main
-- equity_research.db. Cheaply regenerable from yfinance at any time, so
-- this db is gitignored (covered by the existing blanket "*.db" rule) and
-- never git-shard-committed like the main db -- scripts/db_shard.py stays
-- equity_research.db-only; regenerate this one via scripts/backfill_price_
-- history.py instead.
--
-- No foreign key to companies(company_id) -- that table lives in the other
-- db file, and SQLite can't enforce a cross-database FK anyway. Referential
-- integrity is procedural: storage/price_repository.py's writers only ever
-- receive company_ids the caller already read out of the main db's
-- company_index_membership.

CREATE TABLE IF NOT EXISTS daily_prices (
  company_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,          -- ISO date, e.g. "2026-08-27"
  open       REAL,
  high       REAL,
  low        REAL,
  close      REAL NOT NULL,
  volume     INTEGER,
  source     TEXT NOT NULL DEFAULT 'yfinance',
  fetched_at TEXT NOT NULL,          -- storage/database.py's utcnow_iso()
  PRIMARY KEY (company_id, trade_date)
);
