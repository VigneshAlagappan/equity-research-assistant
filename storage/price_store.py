"""PriceStore -- the price-history capability interface, same DI shape as
storage/fact_store.py's FactStore (architecture guardrail #3: storage must
be replaceable behind a repository/data-access interface, business logic
must never depend on SQLite-specific behavior directly). Every field is a
plain callable matching a real storage/price_repository.py function's
signature exactly -- no wrapper classes needed, same pattern FactStore
already established.

default_price_store() is the only place that imports the concrete SQLite-
backed functions directly. Every consumer (scripts/fetch_daily_prices.py,
scripts/backfill_price_history.py, web/app.py's price-feed route) takes an
optional `price_store` parameter defaulting to it -- so swapping SQLite for
a different backend later means supplying a different PriceStore, not
editing every call site.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PriceStore:
    upsert_daily_bars: Callable[..., int]
    get_price_history: Callable[..., list[sqlite3.Row]]
    get_latest_close: Callable[..., sqlite3.Row | None]


def default_price_store() -> PriceStore:
    """The only place that imports the real SQLite-backed implementations
    directly. Everywhere else routes through an injected/default PriceStore."""
    from storage.price_repository import get_latest_close, get_price_history, upsert_daily_bars

    return PriceStore(
        upsert_daily_bars=upsert_daily_bars,
        get_price_history=get_price_history,
        get_latest_close=get_latest_close,
    )
