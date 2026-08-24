"""Google News RSS lookups for the Watchlist's per-company news feed.

Fetches only feed metadata (headline/link/source/time) from Google's own
public RSS endpoint and links out to the original article — never the
article content itself, same as a Google Alerts email. Not scraping: this
is a published feed, not a page we're parsing HTML out of, and it's an
outbound convenience link, not data this app ingests or stores.

A short in-memory cache keeps repeat toggles (or repeat page loads) from
re-hitting Google on every click.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

CACHE_TTL_SECONDS = 900
FETCH_TIMEOUT_SECONDS = 4.0
MAX_ITEMS = 5
_USER_AGENT = "Mozilla/5.0 (compatible; IndianEquityResearchAssistant/1.0)"

_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}


def google_news_last_24h_url(query: str, window_days: int = 1) -> str:
    """The human-facing search-results link (used as a fallback if the feed fetch fails)."""
    encoded_query = urllib.parse.quote(f"{query} when:{window_days}d")
    return f"https://news.google.com/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"


def _rss_url(query: str, window_days: int) -> str:
    encoded_query = urllib.parse.quote(f"{query} when:{window_days}d")
    return f"https://news.google.com/rss/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"


def _relative_time(published: datetime) -> str:
    delta = datetime.now(timezone.utc) - published
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return published.strftime("%b %d")


def fetch_company_news(query: str, window_days: int = 1) -> list[dict] | None:
    """Up to MAX_ITEMS headlines for `query` from the last `window_days` days, newest first.

    Returns None on any fetch/parse failure (network error, timeout,
    malformed feed) — this is best-effort against an external site we don't
    control, so callers fall back to a plain search link rather than erroring.
    """
    now = datetime.now(timezone.utc).timestamp()
    cache_key = (query, window_days)
    cached = _cache.get(cache_key)
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        req = urllib.request.Request(_rss_url(query, window_days), headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw = response.read()
        root = ET.fromstring(raw)
    except Exception:
        return None

    items = []
    for item_el in root.findall("./channel/item")[:MAX_ITEMS]:
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        source = (item_el.findtext("source") or "").strip()
        pub_date_raw = item_el.findtext("pubDate")
        if not title or not link:
            continue

        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()

        published = None
        if pub_date_raw:
            try:
                published = parsedate_to_datetime(pub_date_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None

        items.append(
            {
                "title": title,
                "link": link,
                "source": source or None,
                "published": _relative_time(published) if published else None,
            }
        )

    _cache[cache_key] = (now, items)
    return items
