"""Example-investigation fixture data for the Research screens.

Ported verbatim from the claude.ai/design wireframe's own mock data
(threadData()/examples in "Screens Wireframes copy.dc.html") — not real
computed answers. Used until real question-answering + persistence lands
(README: Resumable build checklist, step 3 for the result view, step 12 for
real persisted threads replacing this file). Every field here is illustrative
content, not a FACT/CALCULATION grounded in ingested data.
"""

from __future__ import annotations

EXAMPLES = [
    # Swapped from the hand-written "Does monsoon rainfall affect tractor
    # sales?" fixture, then swapped again from the SBFC Finance Deep Dive
    # (/investigate/2ff8929f146f) to this real Quick Answer thread instead
    # -- a /research/thread/<id> case (like every entry originally routed
    # to), just a real generated_reports row instead of the wireframe
    # fixture branch, so it still needs the explicit href: the template's
    # fallback (url_for('research_thread', thread_id=ex.thread_id)) is the
    # same URL shape either way, but relying on it here would make this
    # entry indistinguishable in source from the old fixture-backed ones.
    {
        "kicker": "Macro / credit",
        "title": "RBI Bank Credit Growth Analysis",
        "href": "/research/thread/0ee7a134d9b6",
    },
    # Swapped from the hand-written "Kalyan Bank ROA across rate cycles"
    # fixture to a real, already-run Deep Dive investigation -- href set
    # explicitly since this routes to /investigate/<id> (investigate_view),
    # not /research/thread/<thread_id> (the fixture-rendering branch every
    # other entry here still uses); research.html's example-card loop
    # checks for this key before falling back to the old route.
    {
        "kicker": "Bank profitability",
        "title": "Is IDFC FIRST Bank's growth translating into sustainable profitability?",
        "href": "/investigate/b7ebadb572bb",
    },
    # Swapped from the hand-written "How sensitive are paint margins to
    # crude oil?" fixture, same reasoning as above.
    {
        "kicker": "Interest rates",
        "title": "How do macro rate/credit conditions relate to HDFC Bank and peers' margins and asset quality?",
        "href": "/investigate/720016d2c143",
    },
]

# All 3 EXAMPLES entries above now point at real, already-run Deep Dive
# investigations (href) rather than a hand-written wireframe fixture --
# nothing references a THREADS entry anymore, but the name/shape is left
# in place (empty) rather than ripped out here, since web/app.py's
# research_thread/watchlist routes still call THREADS.get(...) defensively
# and a real replacement fixture could reasonably land here again later.
THREADS = {}
