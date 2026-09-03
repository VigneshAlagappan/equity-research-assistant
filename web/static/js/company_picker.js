// Generic company search-and-select widget for a plain HTML form field —
// same /companies/search.json typeahead as header_search.js
// (web/static/js/header_search.js), but fills a hidden form field instead
// of navigating. Wire up any element with [data-company-picker] containing
// a text input [data-company-picker-input], a hidden input
// [data-company-picker-value], and a results container
// [data-company-picker-results] (reuses the .site-search-result* classes
// from web/static/classical/styles.css, so it looks like the header search).
(function () {
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function initOne(root) {
    const input = root.querySelector("[data-company-picker-input]");
    const hidden = root.querySelector("[data-company-picker-value]");
    const results = root.querySelector("[data-company-picker-results]");
    if (!input || !hidden || !results) return;

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

    function choose(item) {
      hidden.value = item.company_id;
      input.value = item.display_name + " (" + item.company_id + ")";
      close();
    }

    function render() {
      if (items.length === 0) {
        results.innerHTML = '<div class="site-search-empty">No matching companies.</div>';
      } else {
        results.innerHTML = items
          .map(
            (c, i) => `
            <button type="button" class="site-search-result${i === activeIndex ? " is-active" : ""}" data-index="${i}">
              <div class="site-search-result-name">${escapeHtml(c.display_name)}</div>
              <div class="site-search-result-meta">${escapeHtml(c.company_id)}${c.sector ? " &middot; " + escapeHtml(c.sector) : ""}</div>
            </button>`
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
        // Network hiccup on a typeahead isn't worth surfacing.
      }
    }

    input.addEventListener("input", () => {
      hidden.value = ""; // typing invalidates a previous selection until a new one is made
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
        if (chosen) choose(chosen);
      } else if (e.key === "Escape") {
        close();
      }
    });

    results.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-index]");
      if (!btn) return;
      const item = items[Number(btn.dataset.index)];
      if (item) choose(item);
    });

    document.addEventListener("click", (e) => {
      if (!root.contains(e.target)) close();
    });
  }

  function init() {
    document.querySelectorAll("[data-company-picker]").forEach(initOne);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
