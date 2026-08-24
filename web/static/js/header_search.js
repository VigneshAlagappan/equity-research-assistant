// Header company search (web/templates/_header.html) — a small typeahead
// against /companies/search.json (companies/registry.py's search_companies,
// substring match on id/display name/legal name/NSE symbol). Selecting a
// result or pressing Enter navigates straight to that company's page.
(function () {
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function init() {
    const root = document.getElementById("site-search");
    const input = document.getElementById("site-search-input");
    const results = document.getElementById("site-search-results");
    if (!root || !input || !results) return;

    let items = [];
    let activeIndex = -1;
    let debounceTimer = null;
    let requestId = 0;

    function close() {
      results.hidden = true;
      results.innerHTML = "";
      items = [];
      activeIndex = -1;
    }

    function goTo(companyId) {
      window.location.href = "/companies/" + encodeURIComponent(companyId);
    }

    function render() {
      if (items.length === 0) {
        results.innerHTML = '<div class="site-search-empty">No matching companies.</div>';
      } else {
        results.innerHTML = items
          .map(
            (c, i) => `
            <a class="site-search-result${i === activeIndex ? " is-active" : ""}" href="/companies/${encodeURIComponent(c.company_id)}" data-index="${i}">
              <div class="site-search-result-name">${escapeHtml(c.display_name)}</div>
              <div class="site-search-result-meta">${escapeHtml(c.company_id)}${c.sector ? " · " + escapeHtml(c.sector) : ""}</div>
            </a>`
          )
          .join("");
      }
      results.hidden = false;
    }

    async function search(query) {
      const thisRequest = ++requestId;
      try {
        const response = await fetch("/companies/search.json?q=" + encodeURIComponent(query));
        const data = await response.json();
        if (thisRequest !== requestId) return; // a newer keystroke already superseded this request
        items = data.results || [];
        activeIndex = -1;
        render();
      } catch (err) {
        // Network hiccup on a typeahead isn't worth surfacing — just leave
        // the dropdown as it was.
      }
    }

    input.addEventListener("input", () => {
      const query = input.value.trim();
      clearTimeout(debounceTimer);
      if (!query) {
        close();
        return;
      }
      debounceTimer = setTimeout(() => search(query), 150);
    });

    input.addEventListener("keydown", (e) => {
      if (results.hidden || items.length === 0) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        activeIndex = Math.min(activeIndex + 1, items.length - 1);
        render();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        activeIndex = Math.max(activeIndex - 1, 0);
        render();
      } else if (e.key === "Enter") {
        e.preventDefault();
        const chosen = items[activeIndex >= 0 ? activeIndex : 0];
        if (chosen) goTo(chosen.company_id);
      } else if (e.key === "Escape") {
        close();
      }
    });

    document.addEventListener("click", (e) => {
      if (!root.contains(e.target)) close();
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
