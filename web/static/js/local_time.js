// Every timestamp the server sends down is UTC (storage/database.py's
// utcnow_iso) — this rewrites each [data-utc] element's text to the
// viewer's own local timezone, since the server has no way to know that
// itself. Runs once at load; nothing on this site rewrites a timestamp
// after the page renders, so no MutationObserver is needed.
(function () {
  function localize(el) {
    const raw = el.dataset.utc;
    if (!raw) return;
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return;
    el.textContent = el.dataset.utcFormat === "date"
      ? parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
      : parsed.toLocaleString(undefined, {
          year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
        });
  }
  document.querySelectorAll("[data-utc]").forEach(localize);
})();
