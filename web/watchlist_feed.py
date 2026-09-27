"""Builds the Watchlist tab's merged activity feed (News + Investigations
today; Announcements & Financial filing updates render their honest empty
state until the on-demand NSE ingestion pipeline in docs/pendingList.md is
built — this module never fabricates data for those two).

One `ActivityItem` list, shaped into Grid (one panel per type) and Feed
(single newest-first list, grouped by day) view-models -- same "one
activity model, two views over it" design the source mockup specifies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from storage.db_types import DBConnection
from storage.repositories import list_company_news, list_research_cases_for_audit

NEWS_WINDOW_DAYS = 7

TYPE_DEFS = {
    "news": {"label": "News", "tag": "NEWS", "scope": f"Last {NEWS_WINDOW_DAYS} days · Google News"},
    "announcement": {"label": "Announcements & disclosures", "tag": "ANNOUNCEMENT", "scope": "Not yet available"},
    "financial": {"label": "Financial & filing updates", "tag": "FINANCIAL", "scope": "Not yet available"},
    "investigation": {"label": "Investigations", "tag": "INVESTIGATION", "scope": "Signals research"},
}

_CASE_STATUS_LABEL = {"in_progress": "In progress", "completed": "Completed", "failed": "Failed"}


@dataclass(frozen=True)
class ActivityItem:
    type: str  # 'news' | 'announcement' | 'financial' | 'investigation'
    company_id: str
    company_name: str
    title: str
    context: str
    source_label: str
    occurred_at: datetime
    url: str
    status: str | None = None


def list_watchlist_activity(conn: DBConnection, companies: dict[str, str]) -> list[ActivityItem]:
    """`companies` is {company_id: display_name} for exactly the companies
    to include (the caller's watchlist) -- an empty dict returns no items
    (never silently reinterpreted as "all companies"), same contract
    list_company_news() itself already follows."""
    if not companies:
        return []
    company_ids = list(companies)
    items: list[ActivityItem] = []
    items.extend(_news_items(conn, companies, company_ids))
    items.extend(_investigation_items(conn, companies, company_ids))
    items.sort(key=lambda it: it.occurred_at, reverse=True)
    return items


def _news_items(conn: DBConnection, companies: dict[str, str], company_ids: list[str]) -> list[ActivityItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_WINDOW_DAYS)
    items = []
    for row in list_company_news(conn, company_ids=company_ids):
        occurred_at = _parse_dt(row["published_at"] or row["fetched_at"])
        if occurred_at is None or occurred_at < cutoff:
            continue
        items.append(ActivityItem(
            type="news", company_id=row["company_id"], company_name=companies.get(row["company_id"], row["company_id"]),
            title=row["title"], context="", source_label=row["source"] or "News",
            occurred_at=occurred_at, url=row["link"],
        ))
    return items


def _investigation_items(conn: DBConnection, companies: dict[str, str], company_ids: list[str]) -> list[ActivityItem]:
    """Reuses list_research_cases_for_audit() (the Audit Log's "every case,
    every status" query) rather than the investigations/investigation_
    companies join -- that join only has rows once a case FINISHES
    (save_investigation() is what inserts investigation_companies), so an
    in_progress/failed case would never show up there. research_cases.
    company_ids is a JSON array column, not a join-friendly relational one,
    so the company match is done in Python after the fetch."""
    company_id_set = set(company_ids)
    items = []
    for case in list_research_cases_for_audit(conn, kind="investigation", limit=200):
        status = case["status"]
        label = _CASE_STATUS_LABEL.get(status)
        if label is None:  # cancelled, or any future status this view doesn't model
            continue
        try:
            case_company_ids = set(json.loads(case["company_ids"]))
        except (TypeError, ValueError):
            continue
        matched = case_company_ids & company_id_set
        if not matched:
            continue
        occurred_at = _parse_dt(case["completed_at"] or case["started_at"])
        if occurred_at is None:
            continue
        investigation_id = case["investigation_id"]
        url = f"/investigate/{investigation_id}" if investigation_id else f"/cases/{case['case_id']}"
        for company_id in matched:
            items.append(ActivityItem(
                type="investigation", company_id=company_id, company_name=companies.get(company_id, company_id),
                title=case["question"], context=case["current_activity"] or "",
                source_label="Signals", occurred_at=occurred_at, url=url, status=label,
            ))
    return items


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------
# View-model: shapes list_watchlist_activity()'s output for the template,
# same role Signals.dc.html's watchlistVals() plays in the source mockup.
# ------------------------------------------------------------------

_TYPE_ORDER = ["news", "announcement", "financial", "investigation"]


def _relative_time(occurred_at: datetime, now: datetime) -> dict:
    delta_hours = (now - occurred_at).total_seconds() / 3600
    local = occurred_at.astimezone(now.tzinfo)
    hh = local.strftime("%H:%M")
    if local.date() == now.date():
        day = "today"
    elif local.date() == (now - timedelta(days=1)).date():
        day = "yesterday"
    else:
        day = "earlier"
    if delta_hours < 1:
        rel = "Just now"
    elif delta_hours < 12:
        rel = f"{round(delta_hours)}h ago"
    elif day == "yesterday":
        rel = f"Yesterday {hh}"
    elif day == "today":
        rel = f"Today {hh}"
    else:
        rel = local.strftime("%-d %b %H:%M")
    return {"rel": rel, "hh": hh, "day": day}


GRID_PANEL_MAX = 4


def build_watchlist_view(
    activity: list[ActivityItem], companies: dict[str, str], *,
    selected_company: str | None, now: datetime | None = None,
) -> dict:
    """Everything watchlist.html needs to render, for the selected company
    scope only -- Grid/Feed switching and type-filter chips are client-side
    (JS shows/hides by `data-type`/`data-view` attributes, matching how the
    source mockup's own Grid/Feed toggle works with no server round trip),
    so this always returns the FULL panel and feed data, never narrowed by
    a server-side view/filter param. Mirrors Signals.dc.html's
    watchlistVals()."""
    now = now or datetime.now(timezone.utc)
    scoped = [a for a in activity if selected_company is None or a.company_id == selected_company]
    company_name = companies.get(selected_company) if selected_company else None

    def to_item(a: ActivityItem) -> dict:
        w = _relative_time(a.occurred_at, now)
        return {
            "type": a.type, "company": a.company_name, "title": a.title, "context": a.context,
            "source": a.source_label, "when": w["rel"], "time": w["hh"], "day": w["day"],
            "type_label": TYPE_DEFS[a.type]["tag"], "is_investigation": a.type == "investigation",
            "has_status": bool(a.status), "status": a.status or "", "url": a.url,
        }

    panels = []
    for t in _TYPE_ORDER:
        rows = [a for a in scoped if a.type == t]
        if t == "news":
            scope = f" for {company_name}" if company_name else ""
            empty = f"No headlines{scope} in the last {NEWS_WINDOW_DAYS} days."
        elif t == "investigation":
            empty = f"No investigations mention {company_name} yet." if company_name else "No investigations touch Watchlist companies yet."
        else:
            empty = "Announcement tracking is not available yet." if t == "announcement" else "Financial filing tracking is not available yet."
        panels.append({
            "key": t, "label": TYPE_DEFS[t]["label"], "scope": TYPE_DEFS[t]["scope"],
            "count": len(rows), "entries": [to_item(a) for a in rows[:GRID_PANEL_MAX]],
            "is_empty": not rows, "empty": empty, "has_more": len(rows) > GRID_PANEL_MAX,
        })

    filters = [{"key": t, "label": TYPE_DEFS[t]["label"].split(" ")[0].rstrip("s") if t != "news" else "News",
                "count": len([a for a in scoped if a.type == t])} for t in _TYPE_ORDER]
    filters.insert(0, {"key": "all", "label": "All", "count": len(scoped)})

    feed_items = [to_item(a) for a in sorted(scoped, key=lambda a: a.occurred_at, reverse=True)]
    groups_order = [("today", "Today"), ("yesterday", "Yesterday"), ("earlier", "Earlier")]
    feed_groups = [{"key": key, "label": label, "entries": [it for it in feed_items if it["day"] == key]}
                   for key, label in groups_order if any(it["day"] == key for it in feed_items)]

    return {
        "companies": companies, "selected_company": selected_company, "company_name": company_name,
        "context_label": (
            f"Activity across all {len(companies)} Watchlist companies" if selected_company is None
            else f"Activity for {company_name}"
        ),
        "panels": panels, "filters": filters, "feed_groups": feed_groups,
        "counts_by_company": {cid: len([a for a in activity if a.company_id == cid]) for cid in companies},
        "total_count": len(activity),
        "feed_empty": not feed_groups,
    }
